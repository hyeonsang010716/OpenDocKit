"""HWP/HWPX paragraph heads (numbers, outline headings) in paragraphs mixed with pictures, tables and text boxes; lower-level
numbers; a chart inside a cell; a shape filled with a picture.

HWP record streams are built with the fake OLE container (`_fake_hwp`) and record helpers of `test_hwp.py`.
"""

import struct

import pytest

from opendockit import convert
from tests import builder
from tests.test_hwp import (
    _CHART_XML,
    TAG_BIN_DATA,
    TAG_BULLET,
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_NUMBERING,
    TAG_PARA_HEADER,
    TAG_PARA_SHAPE,
    TAG_PARA_TEXT,
    TAG_SHAPE_PICTURE,
    TAG_TABLE,
    _bullet,
    _fake_hwp,
    _numbering,
    _para_header,
    _para_shape,
)


_HC = 'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"'
# Paragraph shapes 1-3 are levels 0-2 of the same numbering definition (1. / 가. / 1)); 0 has no head
_HWPX_HEADS = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">'
               '<hh:paraHead start="1" level="1" numFormat="DIGIT">^1.</hh:paraHead>'
               '<hh:paraHead start="1" level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead>'
               '<hh:paraHead start="1" level="3" numFormat="DIGIT">^3)</hh:paraHead>'
               '</hh:numbering></hh:numberings>'
               '<hh:paraProperties itemCnt="4">'
               '<hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
               '<hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>'
               '<hh:paraPr id="2"><hh:heading type="NUMBER" idRef="1" level="1"/></hh:paraPr>'
               '<hh:paraPr id="3"><hh:heading type="NUMBER" idRef="1" level="2"/></hh:paraPr>'
               "</hh:paraProperties>")
_HWP_HEADS = (builder.hwp_record(TAG_NUMBERING, 0, _numbering(("^1.", 0), ("^2.", 8), ("^3)", 0)))
              + b"".join(builder.hwp_record(TAG_PARA_SHAPE, 0, shape)
                         for shape in (_para_shape(), _para_shape(2, 0, 1), _para_shape(2, 1, 1), _para_shape(2, 2, 1))))
_HWP_PICTURE = struct.pack("<HHH", 0x1, 1, 3) + "png".encode("utf-16-le")


def _hwpx_paragraph(text: str, shape: int = 0, style: int = 0) -> str:
    return f'<hp:p paraPrIDRef="{shape}" styleIDRef="{style}"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>'


def _hwpx_picture() -> str:
    return f'<hp:pic><hc:img {_HC} binaryItemIDRef="image1"/></hp:pic>'


def _hwp_paragraph(text: str, shape: int = 0, style: int = 0) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, 0, _para_header(shape, style))
            + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(text)))


def _hwp_picture_control(level: int) -> bytes:
    """A drawing object (gso) CTRL_HEADER holding one picture + SHAPE_COMPONENT_PICTURE (BinItem 1)."""
    picture = b"\x00" * 71 + struct.pack("<H", 1) + b"\x00" * 18
    return (builder.hwp_record(TAG_CTRL_HEADER, level, builder.hwp_ctrl("gso "))
            + builder.hwp_record(TAG_SHAPE_PICTURE, level + 1, picture))


def _hwp_table_control(level: int, *texts: str) -> bytes:
    """The CTRL_HEADER of a one-row table and its cells (`level` is the level of the control)."""
    record = builder.hwp_record
    cells = b"".join(record(TAG_LIST_HEADER, level + 1, builder.hwp_cell(0, col))
                     + record(TAG_PARA_HEADER, level + 1, builder.hwp_para_header())
                     + record(TAG_PARA_TEXT, level + 2, builder.hwp_text(text)) for col, text in enumerate(texts))
    return (record(TAG_CTRL_HEADER, level, builder.hwp_ctrl("tbl "))
            + record(TAG_TABLE, level + 1, struct.pack("<IHH", 0, 1, len(texts)) + b"\x00" * 10) + cells)


class TestHeadOfAParagraphThatStartsWithAPicture:
    """In a numbered or outline paragraph whose picture comes before a table or text box, the head went to the first piece, which
    held only the picture placeholder, so '2.' was left on its own and the real text '추진 목표' lost its number (an outline heading
    paragraph was left with only `#`). A piece holding only a placeholder is not text that takes the head."""

    def test_hwpx_number_goes_to_the_text_after_a_picture_and_a_table(self):
        body = (_hwpx_paragraph("추진 배경", 1)
                + f'<hp:p paraPrIDRef="1"><hp:run>{_hwpx_picture()}<hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
                + builder._hwpx_cell(0, 0, "목표") + builder._hwpx_cell(0, 1, "20%")
                + "</hp:tr></hp:tbl><hp:t>추진 목표</hp:t></hp:run></hp:p>"
                + _hwpx_paragraph("추진 일정", 1))

        result = convert(builder.hwpx_with_picture(body, header_extra=_HWPX_HEADS), "a.hwpx")

        assert result.text == ("1. 추진 배경\n\n[Image]\n\n| 목표 | 20% |\n| --- | --- |\n\n"
                               "2. 추진 목표\n3. 추진 일정")

    def test_hwpx_heading_mark_goes_to_the_text_after_a_picture_and_a_text_box(self):
        body = (_hwpx_paragraph("앞 절 본문")
                + f'<hp:p styleIDRef="2"><hp:run>{_hwpx_picture()}<hp:rect><hp:drawText><hp:subList>'
                + _hwpx_paragraph("핵심 요약") + "</hp:subList></hp:drawText></hp:rect>"
                "<hp:t>사업 개요</hp:t></hp:run></hp:p>"
                + _hwpx_paragraph("본 사업은"))

        result = convert(builder.hwpx_with_picture(body), "a.hwpx")

        assert result.text == "앞 절 본문\n\n[Image]\n\n핵심 요약\n\n# 사업 개요\n\n본 사업은"

    def test_hwpx_paragraph_of_only_a_picture_keeps_its_number_but_gets_no_heading_mark(self):
        """A numbered paragraph without text still counts its number — otherwise the following numbers move up. Only the picture
        of an outline heading paragraph gets no `#`."""
        body = (_hwpx_paragraph("추진 배경", 1)
                + f'<hp:p paraPrIDRef="1"><hp:run>{_hwpx_picture()}</hp:run></hp:p>'
                + _hwpx_paragraph("추진 일정", 1)
                + f'<hp:p styleIDRef="2"><hp:run>{_hwpx_picture()}</hp:run></hp:p>')

        result = convert(builder.hwpx_with_picture(body, header_extra=_HWPX_HEADS), "a.hwpx")

        assert result.text == "1. 추진 배경\n2.\n\n[Image]\n\n3. 추진 일정\n\n[Image]"
        assert "#" not in result.text

    def test_hwp_number_goes_to_the_text_after_a_picture_and_a_table(self, monkeypatch):
        record = builder.hwp_record
        section = (_hwp_paragraph("추진 배경", 1)
                   + record(TAG_PARA_HEADER, 0, _para_header(1))
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "gso "), (11, "tbl "), "추진 목표"))
                   + _hwp_picture_control(1) + _hwp_table_control(1, "목표", "20%")
                   + _hwp_paragraph("추진 일정", 1))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_HEADS + record(TAG_BIN_DATA, 0, _HWP_PICTURE),
                         extra={"BinData/BIN0001.png": builder.png_bytes()})

        result = convert(data, "a.hwp")

        assert result.text == ("1. 추진 배경\n\n[Image]\n\n| 목표 | 20% |\n| --- | --- |\n\n"
                               "2. 추진 목표\n3. 추진 일정")

    def test_hwp_heading_mark_goes_to_the_text_after_a_picture(self, monkeypatch):
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, _para_header(0, 2))
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "gso "), (11, "tbl "), "사업 개요"))
                   + _hwp_picture_control(1) + _hwp_table_control(1, "구분", "내용"))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=record(TAG_BIN_DATA, 0, _HWP_PICTURE),
                         extra={"BinData/BIN0001.png": builder.png_bytes()})

        result = convert(data, "a.hwp")

        assert result.text == "[Image]\n\n| 구분 | 내용 |\n| --- | --- |\n\n# 사업 개요"


class TestOutlineParagraphSplitByATable:
    """The piece after a table in the middle of an outline-style paragraph became a heading too, giving '# 은 아래와 같다'. The
    heading mark goes on the first piece only."""

    def test_hwpx(self):
        body = ('<hp:p styleIDRef="2"><hp:run><hp:t>사업 개요</hp:t><hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
                + builder._hwpx_cell(0, 0, "구분") + builder._hwpx_cell(0, 1, "내용")
                + "</hp:tr></hp:tbl><hp:t>은 아래와 같다</hp:t></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "# 사업 개요\n\n| 구분 | 내용 |\n| --- | --- |\n\n은 아래와 같다"

    def test_hwp(self, monkeypatch):
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, _para_header(0, 2))
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("사업 개요", (11, "tbl "), "은 아래와 같다"))
                   + _hwp_table_control(1, "구분", "내용"))
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "# 사업 개요\n\n| 구분 | 내용 |\n| --- | --- |\n\n은 아래와 같다"


class TestLowerLevelNumbers:
    """Lower-level numbers such as '가.' or '1)' had their leading spaces removed by `clean_text` (list lines are only `-`, `*`,
    `1.`) and got a blank line per item, so there was no telling they were sub-items. They take the same `_shared.list_item_prefix`
    form as DOCX and PPTX (`  - 가. `). The top level stays as it is."""

    _EXPECTED = ("1. 추진 목적\n  - 가. 현황 분석\n    - 1) 매출 감소 10%\n    - 2) 원가 상승 5%\n  - 나. 개선 방향\n"
                 "    - 1) 공정 자동화\n2. 추진 일정")
    _ITEMS = [(1, "추진 목적"), (2, "현황 분석"), (3, "매출 감소 10%"), (3, "원가 상승 5%"), (2, "개선 방향"), (3, "공정 자동화"),
              (1, "추진 일정")]

    def test_hwpx(self):
        body = "".join(_hwpx_paragraph(text, shape) for shape, text in self._ITEMS)
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", _HWPX_HEADS + "</hh:refList>")})

        result = convert(data, "a.hwpx")

        assert result.text == self._EXPECTED

    def test_hwp(self, monkeypatch):
        section = b"".join(_hwp_paragraph(text, shape) for shape, text in self._ITEMS)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_HEADS)

        result = convert(data, "a.hwp")

        assert result.text == self._EXPECTED

    def test_hwp_outline_heading_at_a_lower_level_gets_no_list_mark(self, monkeypatch):
        """The number of an outline paragraph that becomes a heading gets no list bullet (so it does not become
        '# - 가. 세부')."""
        record = builder.hwp_record
        heads = _HWP_HEADS + record(TAG_PARA_SHAPE, 0, _para_shape(1, 1))  # paragraph shape 4: outline level 1
        section = _hwp_paragraph("세부 계획", 4, 2)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=heads)

        result = convert(data, "a.hwp")

        assert result.text == "# 가. 세부 계획"


class TestChartInsideATableCell:
    """A chart inside a cell was drawn as Markdown table text and put in the cell, leaving a mess of `\\|` and `<br>`. Like a table
    inside a table, it is handed to the outer table (`markdown.TableGrid.embed`). A one-column box is a table between paragraphs,
    as before."""

    _CHART = ('<hp:p><hp:run><hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/ooxmlchart">'
              '<hp:chart chartIDRef="Chart/chart1.xml"/></hp:case></hp:switch></hp:run></hp:p>')

    def _table(self, *rows: list[str]) -> bytes:
        def cell(row: int, col: int, inner: str) -> str:
            return (f'<hp:tc><hp:subList>{inner}</hp:subList><hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
                    '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')

        body = ("<hp:p><hp:run><hp:tbl>"
                + "".join("<hp:tr>" + "".join(cell(r, c, inner) for c, inner in enumerate(row)) + "</hp:tr>"
                          for r, row in enumerate(rows))
                + "</hp:tbl></hp:run></hp:p>")
        return builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": _CHART_XML})

    def test_hwpx_chart_keeps_its_rows_and_columns_inside_the_cell(self):
        data = self._table([_hwpx_paragraph("그래프"), _hwpx_paragraph("설명")],
                           [self._CHART, _hwpx_paragraph("판매량 증가")])

        result = convert(data, "a.hwpx")

        assert result.text == ("<table>\n<tr><th>그래프</th><th>설명</th></tr>\n"
                               "<tr><td>[Chart] 판매<br><table><tr><th>Category</th><th>판매량</th></tr><tr><td>1월</td><td>5</td>"
                               "</tr><tr><td>2월</td><td>7</td></tr></table></td><td>판매량 증가</td></tr>\n</table>")
        assert "\\|" not in result.text

    def test_hwpx_chart_in_a_one_column_box_stays_a_table_between_paragraphs(self):
        data = self._table([_hwpx_paragraph("그래프")], [self._CHART])

        result = convert(data, "a.hwpx")

        assert result.text == "그래프\n\n[Chart] 판매\n\n| Category | 판매량 |\n| --- | --- |\n| 1월 | 5 |\n| 2월 | 7 |"


class TestShapeFilledWithAPicture:
    """A shape filled with a picture (hc:fillBrush/hc:imgBrush/hc:img) is not a picture object, so it was dropped without a
    trace. Like a PPTX fill picture it is registered as a picture (before the shape text); a tiled fill (TILE…) is a texture and
    is not registered."""

    @staticmethod
    def _shape(mode: str) -> str:
        return (f'<hp:p><hp:run><hp:rect><hc:fillBrush {_HC}><hc:imgBrush mode="{mode}"><hc:img binaryItemIDRef="image1"/>'
                "</hc:imgBrush></hc:fillBrush><hp:drawText><hp:subList>" + _hwpx_paragraph("사진 설명 상자")
                + "</hp:subList></hp:drawText></hp:rect></hp:run></hp:p>")

    def test_hwpx_picture_fill_is_registered_before_the_shape_text(self):
        result = convert(builder.hwpx_with_picture(self._shape("TOTAL")), "a.hwpx")

        assert result.text == "[Image]\n\n사진 설명 상자"
        assert len(result.images) == 1

    def test_hwpx_tiled_picture_fill_is_a_texture_and_is_not_registered(self):
        result = convert(builder.hwpx_with_picture(self._shape("TILE_HORZ_TOP")), "a.hwpx")

        assert result.text == "사진 설명 상자"
        assert result.images == []


# The object element (SHAPE_COMPONENT) of a drawing object and the text art record under it (HWPTAG_BEGIN + 60, 74)
_TAG_SHAPE_COMPONENT = 0x10 + 60
_TAG_SHAPE_TEXTART = 0x10 + 74


def _text_art(text: str) -> bytes:
    """SHAPE_COMPONENT_TEXTART (hwplib ForControlTextArt): vertex coordinates INT32×8 + text (WORD length + UTF-16) + font name
    + font style + shape info INT32×9 …"""
    return (struct.pack("<8i", 0, 0, 100, 0, 100, 50, 0, 50) + struct.pack("<H", len(text)) + text.encode("utf-16-le")
            + struct.pack("<H", 3) + "궁서체".encode("utf-16-le") + struct.pack("<H", 0) + struct.pack("<9i", *[0] * 9)
            + struct.pack("<Ii", 0, 0))


class TestHwpTextArt:
    """The text of HWP text art (SHAPE_COMPONENT_TEXTART under a drawing object gso) was dropped entirely. Like the hp:textart
    text of HWPX it is a block of its own (test_hwp.py::TestHwpxObjects::test_text_art_is_kept). If the record is broken, only
    that text art is skipped."""

    @staticmethod
    def _section(text_art: bytes) -> bytes:
        record = builder.hwp_record
        return b"".join([
            _hwp_paragraph("머리 문단"),
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "gso "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ") + b"\x00" * 40),
            record(_TAG_SHAPE_COMPONENT, 2, b"tat$tat$" + b"\x00" * 200),
            record(_TAG_SHAPE_TEXTART, 3, text_art),
            _hwp_paragraph("본문 계속"),
        ])

    def test_hwp_text_art_text_is_kept_as_its_own_block(self, monkeypatch):
        data = _fake_hwp(monkeypatch, sections=[self._section(_text_art("2024 가정통신문"))])

        result = convert(data, "a.hwp")

        assert result.text == "머리 문단\n\n2024 가정통신문\n\n본문 계속"

    def test_hwp_text_art_line_breaks_become_new_lines(self, monkeypatch):
        data = _fake_hwp(monkeypatch, sections=[self._section(_text_art("첫 줄\r\n둘째 줄"))])

        result = convert(data, "a.hwp")

        assert result.text == "머리 문단\n\n첫 줄\n둘째 줄\n\n본문 계속"

    @pytest.mark.parametrize("payload", [b"", b"\x00" * 31, b"\x00" * 32 + b"\xff\x7f" + "글".encode("utf-16-le")])
    def test_hwp_broken_text_art_record_is_skipped(self, monkeypatch, payload):
        data = _fake_hwp(monkeypatch, sections=[self._section(payload)])

        result = convert(data, "a.hwp")

        assert result.text == "머리 문단\n\n본문 계속"


# The bullets □, ○ and - are all level 0 and differ only in the paragraph's left margin (Hangul report formatting). Paragraph
# shape 0 has no head (margin 0), 4 is an indented paragraph without a head (margin 2000).
_BULLET_MARGINS = {1: 0, 2: 2000, 3: 4000}
_HWPX_BULLETS = ('<hh:bullets itemCnt="3"><hh:bullet id="1" char="□"/><hh:bullet id="2" char="○"/><hh:bullet id="3" char="-"/>'
                 '</hh:bullets><hh:paraProperties itemCnt="5">'
                 '<hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                 + "".join(f'<hh:paraPr id="{shape}"><hh:heading type="BULLET" idRef="{shape}" level="0"/>'
                           f'<hh:margin><hc:intent value="-1000"/><hc:left value="{margin}"/></hh:margin></hh:paraPr>'
                           for shape, margin in _BULLET_MARGINS.items())
                 + '<hh:paraPr id="4"><hh:heading type="NONE" idRef="0" level="0"/><hh:margin><hc:left value="2000"/>'
                 "</hh:margin></hh:paraPr></hh:paraProperties>")


def _hwp_bullet_shape(bullet_id: int, margin: int) -> bytes:
    """PARA_SHAPE: attribute 1 (head type 3 = bullet, level 0) + left margin, right margin, indent … + bullet ID."""
    return struct.pack("<I6iHH", (3 << 23) if bullet_id else 0, margin, 0, 0, 0, 0, 0, 0, bullet_id) + b"\x00" * 22


_HWP_BULLETS = (b"".join(builder.hwp_record(TAG_BULLET, 0, _bullet(char)) for char in "□○-")
                + builder.hwp_record(TAG_PARA_SHAPE, 0, _hwp_bullet_shape(0, 0))
                + b"".join(builder.hwp_record(TAG_PARA_SHAPE, 0, _hwp_bullet_shape(shape, margin))
                           for shape, margin in _BULLET_MARGINS.items())
                + builder.hwp_record(TAG_PARA_SHAPE, 0, _hwp_bullet_shape(0, 2000)))


class TestBulletNestingByMargin:
    """When the □, ○ and - bullets are all level 0 and the hierarchy is shown only by the paragraph's left margin, the result was a
    flat `- ` list and there was no telling which item each one belonged under. An item goes one level down only when the margin
    of consecutive bullet paragraphs grows (same direction as the DOCX indentation levels, conservative)."""

    _ITEMS = [(1, "추진 배경"), (2, "디지털 전환 가속"), (3, "민원 처리 시간 30% 단축 필요"), (1, "추진 방향"), (2, "단계적 도입")]
    _EXPECTED = "- 추진 배경\n  - 디지털 전환 가속\n    - 민원 처리 시간 30% 단축 필요\n- 추진 방향\n  - 단계적 도입"

    @staticmethod
    def _hwpx(items: list[tuple[int, str]]) -> bytes:
        body = "".join(_hwpx_paragraph(text, shape) for shape, text in items)
        return builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", _HWPX_BULLETS + "</hh:refList>").replace(
                "xmlns:hh=", f"{_HC} xmlns:hh=")})

    @staticmethod
    def _hwp(monkeypatch, items: list[tuple[int, str]]) -> bytes:
        section = b"".join(_hwp_paragraph(text, shape) for shape, text in items)
        return _fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_BULLETS)

    def test_hwpx_bullets_nest_by_their_left_margin(self):
        result = convert(self._hwpx(self._ITEMS), "a.hwpx")

        assert result.text == self._EXPECTED

    def test_hwp_bullets_nest_by_their_left_margin(self, monkeypatch):
        result = convert(self._hwp(monkeypatch, self._ITEMS), "a.hwp")

        assert result.text == self._EXPECTED

    def test_hwpx_bullets_with_the_same_margin_stay_flat(self):
        result = convert(self._hwpx([(2, "가"), (2, "나"), (2, "다")]), "a.hwpx")

        assert result.text == "- 가\n- 나\n- 다"

    def test_hwpx_margin_paragraph_between_bullets_ends_the_nesting(self):
        """A body paragraph with margin 0 in between closes the items before it — the indented bullet after it is not a
        sub-item."""
        result = convert(self._hwpx([(1, "추진 배경"), (0, "본문 설명"), (2, "세부 항목")]), "a.hwpx")

        assert result.text == "- 추진 배경\n\n본문 설명\n\n- 세부 항목"

    def test_hwpx_indented_paragraph_under_an_item_keeps_the_nesting(self):
        """A body paragraph indented to line up with the item text (margin 2000) does not close the □ item."""
        result = convert(self._hwpx([(1, "추진 배경"), (4, "설명 문단"), (2, "세부 항목")]), "a.hwpx")

        assert result.text == "- 추진 배경\n\n설명 문단\n\n  - 세부 항목"

    def test_hwpx_bullets_inside_a_table_cell_nest_with_cell_marks(self):
        cell = "".join(_hwpx_paragraph(text, shape) for shape, text in [(1, "목표"), (2, "세부"), (1, "방향")])
        body = (f'<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList>{_hwpx_paragraph("구분")}</hp:subList>'
                '<hp:cellAddr colAddr="0" rowAddr="0"/></hp:tc>'
                f'<hp:tc><hp:subList>{cell}</hp:subList><hp:cellAddr colAddr="1" rowAddr="0"/></hp:tc></hp:tr></hp:tbl>'
                "</hp:run></hp:p>")
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", _HWPX_BULLETS + "</hh:refList>").replace(
                "xmlns:hh=", f"{_HC} xmlns:hh=")})

        result = convert(data, "a.hwpx")

        assert result.text == "| 구분 | - 목표<br>└ - 세부<br>- 방향 |\n| --- | --- |"


class TestHwpxCheckBullets:
    """A check bullet (checkedChar of hh:bullet) puts ☑ or ☐ according to the check state of the paragraph shape (hh:paraPr
    checked). They all came out as just '- ', so the done/not done of a checklist was lost."""

    _HEADS = ('<hh:bullets itemCnt="2"><hh:bullet id="1" char="☐" checkedChar="☑" useImage="0"/>'
              '<hh:bullet id="2" char="●" useImage="0"/></hh:bullets>'
              '<hh:paraProperties itemCnt="3">'
              '<hh:paraPr id="1" checked="1"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>'
              '<hh:paraPr id="2" checked="0"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>'
              '<hh:paraPr id="3" checked="1"><hh:heading type="BULLET" idRef="2" level="0"/></hh:paraPr>'
              '</hh:paraProperties>')

    def _convert(self, *paragraphs: tuple[int, str], heads: str | None = None) -> str:
        body = "".join(f'<hp:p paraPrIDRef="{shape}"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>' for shape, text in paragraphs)
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", (heads or self._HEADS) + "</hh:refList>")})
        return convert(data, "a.hwpx").text

    def test_checked_and_unchecked_items_keep_their_state(self):
        text = self._convert((1, "안전모 착용 확인"), (2, "소화기 위치 확인"), (1, "비상구 개방 확인"))

        assert text == "- ☑ 안전모 착용 확인\n- ☐ 소화기 위치 확인\n- ☑ 비상구 개방 확인"

    def test_plain_bullet_ignores_the_checked_attribute(self):
        text = self._convert((3, "일반 항목"))

        assert text == "- 일반 항목"

    def test_check_character_in_the_private_use_area_is_left_out(self):
        """Private use area characters of the Hancom symbol font are removed anyway, so only '- ' is left (the former output)."""
        heads = self._HEADS.replace('char="☐" checkedChar="☑"', 'char="" checkedChar=""')

        text = self._convert((1, "안전모 착용 확인"), (2, "소화기 위치 확인"), heads=heads)

        assert text == "- 안전모 착용 확인\n- 소화기 위치 확인"
