"""HWP/HWPX: a table that is not treated as a character and is drawn under the lines of its paragraph.

Hangul keeps the control of such a table at the start of the paragraph and places the table by an offset from the top of the
paragraph. Read where the control stands, the table came before the line that introduces it.
"""

import struct

import pytest

from opendockit import convert
from opendockit.extractors import _hangul
from tests import builder
from tests.test_hwp import (
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_SHAPE,
    TAG_PARA_TEXT,
    TAG_TABLE,
    _fake_hwp,
    _para_header,
)


_TAG_PARA_LINE_SEG = 16 + 53
_TABLE = "| 구분 | 내용 |\n| --- | --- |\n| 기간 | 1년 |"
_OTHER = "| 항목 | 값 |\n| --- | --- |\n| 예산 | 3억 |"
_LABEL = "□ 수상작 목록"

# What the file says of a table: (treated as a character, measured from, side, how text passes), as HWPX writes it and as
# the bits of the common attributes of HWP ([HWP 5.0] table 70)
_UNDER = (False, "PARA", "TOP", "TOP_AND_BOTTOM")
_FROM = {"PAPER": 0, "PAGE": 1, "PARA": 2}
_SIDE = {"TOP": 0, "CENTER": 1, "BOTTOM": 2}
_WRAP = {"SQUARE": 0, "TOP_AND_BOTTOM": 1, "BEHIND_TEXT": 2, "IN_FRONT_OF_TEXT": 3}


def _hwpx_table(offset: int | str, placed: tuple = _UNDER, texts: tuple[str, ...] = ("구분", "내용", "기간", "1년")) -> str:
    as_character, measured_from, side, wrap = placed
    cells = [builder._hwpx_cell(index // 2, index % 2, text) for index, text in enumerate(texts)]
    return (f'<hp:tbl rowCnt="2" colCnt="2" textWrap="{wrap}"><hp:pos treatAsChar="{int(as_character)}" vertRelTo="{measured_from}" '
            f'vertAlign="{side}" vertOffset="{offset}"/><hp:tr>{cells[0]}{cells[1]}</hp:tr><hp:tr>{cells[2]}{cells[3]}</hp:tr></hp:tbl>')


def _hwpx_lines(*lines: tuple[int | str, int | str]) -> str:
    return "<hp:linesegarray>" + "".join(f'<hp:lineseg textpos="0" vertpos="{position}" vertsize="{height}"/>'
                                         for position, height in lines) + "</hp:linesegarray>"


_HC = 'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"'


def _hwpx_margin(above: int | str, unit: str = "HWPUNIT") -> str:
    return f'<hh:margin><hc:prev {_HC} value="{above}" unit="{unit}"/></hh:margin>'


def _hwpx_shape(identifier: str, case: int | str, default: int | str) -> str:
    """A paragraph shape as Hangul writes it: the margin in both branches of a switch, the value HWP stores in the second."""
    return (f'<hh:paraPr id="{identifier}"><hp:switch><hp:case>{_hwpx_margin(case)}</hp:case>'
            f"<hp:default>{_hwpx_margin(default)}</hp:default></hp:switch></hh:paraPr>")


# Paragraph shapes by id: no spacing above; 1,500 above (3,000 in the branch that holds the value of HWP); written without a
# switch; in the second branch only; in another unit; as no number; without a margin
_HWPX_SHAPES = ("<hh:paraProperties>" + _hwpx_shape("0", 0, 0) + _hwpx_shape("1", 1500, 3000)
                + f'<hh:paraPr id="2">{_hwpx_margin(700)}</hh:paraPr>'
                + f'<hh:paraPr id="3"><hp:switch><hp:default>{_hwpx_margin(900)}</hp:default></hp:switch></hh:paraPr>'
                + f'<hh:paraPr id="4"><hp:switch><hp:case>{_hwpx_margin(0, "CHAR")}</hp:case>'
                  f"<hp:default>{_hwpx_margin(0)}</hp:default></hp:switch></hh:paraPr>"
                + f'<hh:paraPr id="5">{_hwpx_margin("x")}</hh:paraPr>' + '<hh:paraPr id="6"/>' + _hwpx_shape("7", -400, -800)
                + "</hh:paraProperties>")


def _hwpx_document(body: str) -> str:
    data = builder.zip_with(builder.hwpx_bytes(body), edit={
        "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", _HWPX_SHAPES + "</hh:refList>")})
    return convert(data, "a.hwpx").text


def _hwpx(*pieces: str, lines: str = "", shape: str | None = "0") -> str:
    """One paragraph: its tables and texts in the order of the file, and the line segments Hangul stored."""
    reference = "" if shape is None else f' paraPrIDRef="{shape}"'
    return _hwpx_document(f"<hp:p{reference}><hp:run>{''.join(pieces)}</hp:run>{lines}</hp:p>")


def _text(text: str) -> str:
    return f"<hp:t>{text}</hp:t>"


def _hwp_table(level: int, offset: int, placed: tuple = _UNDER, texts: tuple[str, ...] = ("구분", "내용", "기간", "1년")) -> bytes:
    as_character, measured_from, side, wrap = placed
    attributes = int(as_character) | _FROM[measured_from] << 3 | _SIDE[side] << 5 | _WRAP[wrap] << 21
    record = builder.hwp_record
    cells = b"".join(record(TAG_LIST_HEADER, level + 1, builder.hwp_cell(index // 2, index % 2))
                     + record(TAG_PARA_HEADER, level + 1, builder.hwp_para_header())
                     + record(TAG_PARA_TEXT, level + 2, builder.hwp_text(text)) for index, text in enumerate(texts))
    header = "tbl "[::-1].encode("latin-1") + struct.pack("<IiiII", attributes, offset, 0, 40000, 20000)
    return record(TAG_CTRL_HEADER, level, header) + record(TAG_TABLE, level + 1, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10) + cells


def _hwp_lines(*lines: tuple[int, int], level: int = 1) -> bytes:
    """PARA_LINE_SEG: 36 bytes a line; the height of the line at byte 8 is not the height of its text at byte 12."""
    return builder.hwp_record(_TAG_PARA_LINE_SEG, level, b"".join(struct.pack("<iiiiiiiiI", 0, position, height, 99999, 0, 0, 0, 0, 0)
                                                               for position, height in lines))


def _hwp_shape(stored_above: int, size: int = 54) -> bytes:
    """PARA_SHAPE: attribute 1, then six INT32s - the margins, the indent, the spacing above (stored doubled) and below, the
    line spacing."""
    return (struct.pack("<I6i", 0, 0, 0, 0, stored_above, 0, 0) + b"\x00" * 26)[:size]


# Paragraph shapes by id: no spacing above; 1,500 above, stored as 3,000; a record cut before its spacing; a spacing below nothing
_HWP_SHAPES = b"".join(builder.hwp_record(TAG_PARA_SHAPE, 0, shape) for shape in (_hwp_shape(0), _hwp_shape(3000), _hwp_shape(0, 16),
                                                                                  _hwp_shape(-800)))


def _hwp_document(monkeypatch, section: bytes) -> str:
    return convert(_fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_SHAPES), "a.hwp").text


def _hwp(monkeypatch, text: tuple, *controls: bytes, lines: bytes = b"", shape: int = 0) -> str:
    """One paragraph: its text with a table control for each `...`, the line segments, and the tables."""
    parts = [(11, "tbl ") if part is ... else part for part in text]
    section = (builder.hwp_record(TAG_PARA_HEADER, 0, _para_header(shape, 0))
               + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(*parts)) + lines + b"".join(controls))
    return _hwp_document(monkeypatch, section)


class TestTableSetUnderTheLinesOfItsParagraph:
    """A press release wrote `□ 대상 및 최우수·우수상 수상작(3편)` and the table of the works under it. The table is anchored in
    that line and set 2,548 units under its top, the line is 1,500 high: Hangul draws the line, then the table. The control
    stands before the first character, so the table of two pages came first and the line that introduces it last."""

    def test_hwpx_table_under_the_one_line_is_written_after_it(self):
        assert _hwpx(_hwpx_table(2548), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_LABEL}\n\n{_TABLE}"

    def test_hwp_table_under_the_one_line_is_written_after_it(self, monkeypatch):
        text = _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 1465), lines=_hwp_lines((7000, 1000)))

        assert text == f"{_LABEL}\n\n{_TABLE}"

    @pytest.mark.parametrize(("offset", "after"), [(1500, True), (1499, False), (0, False), (-62, False)],
                             ids=["at-the-bottom-of-the-line", "one-unit-into-the-line", "at-the-top", "above-the-top"])
    def test_table_that_starts_inside_the_line_stays_first(self, monkeypatch, offset, after):
        expected = f"{_LABEL}\n\n{_TABLE}" if after else f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(offset % (1 << 32)), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, offset), lines=_hwp_lines((3930, 1500))) == expected

    @pytest.mark.parametrize(("offset", "after"), [(5000, True), (4660, True), (4659, False), (2000, False)],
                             ids=["under-both", "at-the-bottom-of-the-second", "inside-the-second", "between-the-two"])
    def test_table_is_under_the_text_only_when_it_is_under_every_line(self, monkeypatch, offset, after):
        """Two lines, the second 3,160 under the first and 1,500 high. A table that starts between them stays where its
        control is: the text is not cut at the line."""
        lines = ((20000, 1500), (23160, 1500))
        expected = f"{_LABEL}\n\n{_TABLE}" if after else f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(offset), _text(_LABEL), lines=_hwpx_lines(*lines)) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, offset), lines=_hwp_lines(*lines)) == expected

    @pytest.mark.parametrize("placed", [
        (True, "PARA", "TOP", "TOP_AND_BOTTOM"), (False, "PAGE", "TOP", "TOP_AND_BOTTOM"), (False, "PAPER", "TOP", "TOP_AND_BOTTOM"),
        (False, "PARA", "BOTTOM", "TOP_AND_BOTTOM"), (False, "PARA", "CENTER", "TOP_AND_BOTTOM"), (False, "PARA", "TOP", "SQUARE"),
        (False, "PARA", "TOP", "BEHIND_TEXT"), (False, "PARA", "TOP", "IN_FRONT_OF_TEXT"),
    ], ids=["treated-as-a-character", "measured-from-the-page", "measured-from-the-paper", "measured-from-the-bottom",
            "centred", "text-beside-it", "behind-the-text", "in-front-of-the-text"])
    def test_table_placed_any_other_way_stays_where_its_control_is(self, monkeypatch, placed):
        expected = f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(2548, placed), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 2548, placed), lines=_hwp_lines((3930, 1500))) == expected

    def test_without_the_lines_of_the_paragraph_the_table_stays_first(self, monkeypatch):
        """A file another program wrote has no line segments: nothing says where the text ends."""
        expected = f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(2548), _text(_LABEL)) == expected
        assert _hwpx(_hwpx_table(2548), _text(_LABEL), lines=_hwpx_lines()) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 2548)) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 2548), lines=_hwp_lines()) == expected

    def test_lines_that_start_again_higher_up_keep_the_table_first(self, monkeypatch):
        """The paragraph goes on in the next column: the positions start again, and the offset says nothing about that column."""
        lines = ((60000, 1500), (2000, 1500))
        expected = f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(9000), _text(_LABEL), lines=_hwpx_lines(*lines)) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 9000), lines=_hwp_lines(*lines)) == expected

    @pytest.mark.parametrize("lines", [(("x", 1500),), ((3930, "x"),), ((3930, ""),)], ids=["position", "height", "empty-height"])
    def test_hwpx_lines_that_are_no_numbers_keep_the_table_first(self, lines):
        assert _hwpx(_hwpx_table(2548), _text(_LABEL), lines=_hwpx_lines(*lines)) == f"{_TABLE}\n\n{_LABEL}"

    @pytest.mark.parametrize("offset", ["x", "", "1e3"])
    def test_hwpx_offset_that_is_no_number_keeps_the_table_first(self, offset):
        assert _hwpx(_hwpx_table(offset), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"

    def test_hwpx_table_without_its_place_stays_first(self):
        table = _hwpx_table(2548).replace('<hp:pos treatAsChar="0" vertRelTo="PARA" vertAlign="TOP" vertOffset="2548"/>', "")

        assert _hwpx(table, _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"

    def test_line_of_a_height_below_nothing_keeps_the_table_first(self, monkeypatch):
        expected = f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(2548), _text(_LABEL), lines=_hwpx_lines((3930, -9000))) == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 2548), lines=_hwp_lines((3930, -9000))) == expected

    def test_hwp_line_segments_cut_short_are_read_as_far_as_they_go(self, monkeypatch):
        lines = _hwp_lines((3930, 1500), (7090, 1500))

        short = builder.hwp_record(_TAG_PARA_LINE_SEG, 1, lines[4:4 + 36 + 20])  # the second line without its end

        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, 2548), lines=short) == f"{_LABEL}\n\n{_TABLE}"

    def test_text_on_both_sides_of_the_control_is_one_text_above_the_table(self, monkeypatch):
        """The control stands in the middle of the line: the line is drawn whole, above the table."""
        expected = f"수상작 목록은 아래와 같다\n\n{_TABLE}"

        assert _hwpx(_text("수상작 목록은 "), _hwpx_table(2548), _text("아래와 같다"), lines=_hwpx_lines((3930, 1500))) == expected
        assert _hwp(monkeypatch, ("수상작 목록은 ", ..., "아래와 같다"), _hwp_table(1, 2548), lines=_hwp_lines((3930, 1500))) == expected

    def test_two_tables_under_the_text_keep_their_order(self, monkeypatch):
        other = ("항목", "값", "예산", "3억")
        expected = f"{_LABEL}\n\n{_TABLE}\n\n{_OTHER}"

        assert _hwpx(_hwpx_table(2548), _hwpx_table(30000, texts=other), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == expected
        assert _hwp(monkeypatch, (..., ..., _LABEL), _hwp_table(1, 2548), _hwp_table(1, 30000, texts=other),
                    lines=_hwp_lines((3930, 1500))) == expected

    def test_table_treated_as_a_character_keeps_its_place_beside_one_under_the_text(self, monkeypatch):
        other = ("항목", "값", "예산", "3억")
        inline = (True, "PARA", "TOP", "TOP_AND_BOTTOM")
        expected = f"{_OTHER}\n\n{_LABEL}\n\n{_TABLE}"

        assert _hwpx(_hwpx_table(2548), _hwpx_table(0, inline, other), _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == expected
        assert _hwp(monkeypatch, (..., ..., _LABEL), _hwp_table(1, 2548), _hwp_table(1, 0, inline, other),
                    lines=_hwp_lines((3930, 1500))) == expected

    def test_heading_of_the_paragraph_goes_on_its_text_not_on_a_line_before_the_table(self):
        """An outline paragraph: its mark is for the text, which now comes first."""
        body = (f'<hp:p paraPrIDRef="0" styleIDRef="2"><hp:run>{_hwpx_table(2548)}{_text("수상작 목록")}</hp:run>{_hwpx_lines((3930, 1500))}</hp:p>'
                "<hp:p><hp:run><hp:t>다음 문단</hp:t></hp:run></hp:p>")

        assert _hwpx_document(body) == f"# 수상작 목록\n\n{_TABLE}\n\n다음 문단"

    def test_note_of_the_text_takes_its_number_before_a_note_in_the_table(self):
        """Notes are numbered in the order they are written."""
        note = "<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>{}</hp:t></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl>"
        table = _hwpx_table(2548).replace("<hp:t>1년</hp:t>", "<hp:t>1년</hp:t>" + note.format("표의 주"))

        text = _hwpx(table, _text(_LABEL), note.format("글의 주"), lines=_hwpx_lines((3930, 1500)))

        assert text.index(_LABEL + "[^1]") < text.index("1년[^2]")
        assert text.index("[^1]: 글의 주") < text.index("[^2]: 표의 주")

    @pytest.mark.parametrize(("offset", "after"), [(3000, True), (2999, False), (1500, False)],
                             ids=["under-the-spacing-and-the-line", "one-unit-into-the-line", "inside-the-spacing"])
    def test_spacing_above_the_paragraph_stands_between_its_top_and_its_line(self, monkeypatch, offset, after):
        """A label with 1,500 above it: the offset of the table counts from the top of the paragraph, the line is stored
        1,500 under that top and is 1,500 high. A table 2,000 under the top reaches into the line, and Hangul puts the line
        under it. HWPX names the spacing in the first branch of a switch and twice that in the second; HWP stores it doubled."""
        expected = f"{_LABEL}\n\n{_TABLE}" if after else f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(offset), _text(_LABEL), lines=_hwpx_lines((49680, 1500)), shape="1") == expected
        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, offset), lines=_hwp_lines((49680, 1500)), shape=1) == expected

    @pytest.mark.parametrize(("shape", "offset", "after"), [
        ("2", 2200, True), ("2", 2199, False), ("3", 2400, True), ("3", 2399, False), ("6", 1500, True), ("7", 1500, True),
        ("7", 1499, False), ("4", 9000, False), ("5", 9000, False), ("99", 9000, False), (None, 9000, False),
    ], ids=["without-a-switch", "without-a-switch-into-the-line", "second-branch-only", "second-branch-only-into-the-line",
            "no-margin", "below-nothing", "below-nothing-into-the-line", "another-unit", "no-number", "no-such-shape", "no-shape-named"])
    def test_hwpx_spacing_above_is_read_where_the_shape_of_the_paragraph_gives_it(self, shape, offset, after):
        """Without a spacing that can be read, nothing is said of where the table stands."""
        expected = f"{_LABEL}\n\n{_TABLE}" if after else f"{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(offset), _text(_LABEL), lines=_hwpx_lines((3930, 1500)), shape=shape) == expected

    @pytest.mark.parametrize(("shape", "offset", "after"), [(2, 9000, False), (9, 9000, False), (3, 1500, True), (3, 1499, False)],
                             ids=["record-cut-before-the-spacing", "no-such-shape", "below-nothing", "below-nothing-into-the-line"])
    def test_hwp_spacing_above_is_read_where_the_shape_of_the_paragraph_gives_it(self, monkeypatch, shape, offset, after):
        expected = f"{_LABEL}\n\n{_TABLE}" if after else f"{_TABLE}\n\n{_LABEL}"

        assert _hwp(monkeypatch, (..., _LABEL), _hwp_table(1, offset), lines=_hwp_lines((3930, 1500)), shape=shape) == expected

    def test_other_table_that_starts_higher_keeps_every_table_where_its_control_is(self, monkeypatch):
        """A table at the top of the paragraph pushes the line under itself, and the stored position of that line is then not
        the top the offsets are measured from: a second table 5,600 under the top stands above the line, not under it."""
        other = ("항목", "값", "예산", "3억")
        expected = f"{_OTHER}\n\n{_TABLE}\n\n{_LABEL}"

        assert _hwpx(_hwpx_table(0, texts=other), _hwpx_table(5600), _text(_LABEL), lines=_hwpx_lines((9496, 1000))) == expected
        assert _hwp(monkeypatch, (..., ..., _LABEL), _hwp_table(1, 0, texts=other), _hwp_table(1, 5600),
                    lines=_hwp_lines((9496, 1000))) == expected

    @pytest.mark.parametrize("wrap", ["TOP_AND_BOTTOM", "SQUARE", "IN_FRONT_OF_TEXT"])
    def test_hwpx_picture_or_shape_placed_in_the_paragraph_keeps_the_table_where_its_control_is(self, wrap):
        for name in ("pic", "rect", "container"):
            other = (f'<hp:{name} textWrap="{wrap}"><hp:pos treatAsChar="0" vertRelTo="PARA" vertAlign="TOP" vertOffset="0"/>'
                     f"</hp:{name}>")

            text = _hwpx(other, _hwpx_table(5200), _text(_LABEL), lines=_hwpx_lines((9496, 1000)))

            assert text.index("구분") < text.index(_LABEL), name

    def test_hwpx_picture_treated_as_a_character_does_not_keep_the_table(self):
        picture = '<hp:pic><hp:pos treatAsChar="1" vertRelTo="PARA" vertAlign="TOP" vertOffset="0"/></hp:pic>'

        text = _hwpx(_hwpx_table(2548), picture, _text(_LABEL), lines=_hwpx_lines((3930, 1500)))

        assert text.index(_LABEL) < text.index("구분")

    def test_hwpx_switch_in_the_paragraph_keeps_the_table_where_its_control_is(self):
        """What a switch holds is not looked at: it may be an object that pushes the line."""
        switch = "<hp:switch><hp:case><hp:t>가</hp:t></hp:case></hp:switch>"

        assert _hwpx(_hwpx_table(2548), switch, _text(_LABEL), lines=_hwpx_lines((3930, 1500))).startswith(_TABLE)

    def test_hwp_drawing_object_placed_in_the_paragraph_keeps_the_table_where_its_control_is(self, monkeypatch):
        floating = builder.hwp_record(TAG_CTRL_HEADER, 1, "gso "[::-1].encode("latin-1") + struct.pack("<Ii", 2 << 3 | 1 << 21, 0))
        as_character = builder.hwp_record(TAG_CTRL_HEADER, 1, "gso "[::-1].encode("latin-1") + struct.pack("<Ii", 1, 0))
        short = builder.hwp_record(TAG_CTRL_HEADER, 1, "gso "[::-1].encode("latin-1"))
        text = ((11, "gso "), ..., _LABEL)

        assert _hwp(monkeypatch, text, floating, _hwp_table(1, 2548), lines=_hwp_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"
        assert _hwp(monkeypatch, text, short, _hwp_table(1, 2548), lines=_hwp_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"
        assert _hwp(monkeypatch, text, as_character, _hwp_table(1, 2548), lines=_hwp_lines((3930, 1500))) == f"{_LABEL}\n\n{_TABLE}"

    def test_hwpx_table_without_the_word_on_being_a_character_stays_first(self):
        table = _hwpx_table(2548).replace('treatAsChar="0" ', "")

        assert _hwpx(table, _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"

    def test_hwp_control_of_just_the_attributes_and_the_offset_is_read(self, monkeypatch):
        table = _hwp_table(1, 2548)
        size = struct.unpack_from("<I", table)[0] >> 20
        short = builder.hwp_record(TAG_CTRL_HEADER, 1, table[4:16]) + table[4 + size:]

        assert _hwp(monkeypatch, (..., _LABEL), short, lines=_hwp_lines((3930, 1500))) == f"{_LABEL}\n\n{_TABLE}"

    def test_lines_of_a_cell_are_not_the_lines_of_the_paragraph(self):
        """The cells of a table carry line segments of their own: the paragraph's are the ones that are its own child."""
        table = _hwpx_table(2548).replace("</hp:run></hp:p></hp:subList>", "</hp:run>" + _hwpx_lines((0, 100)) + "</hp:p></hp:subList>")

        assert _hwpx(table, _text(_LABEL)) == f"{_TABLE}\n\n{_LABEL}"
        assert _hwpx(table, _text(_LABEL), lines=_hwpx_lines((3930, 9000))) == f"{_TABLE}\n\n{_LABEL}"
        assert _hwpx(table, _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_LABEL}\n\n{_TABLE}"

    def test_hwp_table_under_the_text_of_a_cell_is_written_after_it_there_too(self, monkeypatch):
        """The request for proposals is an HWP file: the paragraph, its lines and its table are a level deeper."""
        record = builder.hwp_record
        cell = (record(TAG_LIST_HEADER, 2, builder.hwp_cell(1, 1)) + record(TAG_PARA_HEADER, 2, builder.hwp_para_header())
                + record(TAG_PARA_TEXT, 3, builder.hwp_text((11, "tbl "), "ㅇ 기본 산출물 목록")) + _hwp_lines((7000, 1000), level=3)
                + _hwp_table(3, 1465))
        plain = b"".join(record(TAG_LIST_HEADER, 2, builder.hwp_cell(index // 2, index % 2))
                         + record(TAG_PARA_HEADER, 2, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 3, builder.hwp_text(text))
                         for index, text in enumerate(("번호", "내용", "1")))
        outer = (record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10)
                 + plain + cell)
        section = (record(TAG_PARA_HEADER, 0, _para_header(0, 0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))) + outer)

        text = _hwp_document(monkeypatch, section)

        assert text.index("ㅇ 기본 산출물 목록") < text.index("구분") < text.index("1년")

    def test_comment_on_the_text_no_longer_takes_in_the_table(self):
        """The range of a comment is the text written between its two ends: the table is written after them now."""
        memo = ('<hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>확인</hp:t></hp:run></hp:p></hp:subList>'
                '</hp:fieldBegin></hp:ctrl>{}<hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>')

        text = _hwpx(memo.format(_hwpx_table(2548) + _text(_LABEL)), lines=_hwpx_lines((3930, 1500)))

        assert f'"{_LABEL}"' in text and "구분 내용" not in text.split(_TABLE)[1]

    def test_hwpx_table_inside_a_switch_is_read_where_it_stands(self):
        """Only a table that is a child of a run of the paragraph is kept for its end. One inside an hp:switch is read with
        its branch - also when the switch carries line segments and the name of a shape of its own, which would have had it
        kept for a paragraph that never comes and written nowhere."""
        switch = f"<hp:switch paraPrIDRef=\"0\"><hp:case>{_hwpx_table(2548)}</hp:case>{_hwpx_lines((3930, 1500))}</hp:switch>"

        assert _hwpx(switch, _text(_LABEL), lines=_hwpx_lines((3930, 1500))) == f"{_TABLE}\n\n{_LABEL}"

    def test_table_under_the_text_of_a_cell_is_written_after_it_there_too(self):
        """A request for proposals: the list of deliverables is a table inside a cell, under the line that names it."""
        inner = f"<hp:p paraPrIDRef=\"0\"><hp:run>{_hwpx_table(1465)}{_text('ㅇ 기본 산출물 목록')}</hp:run>{_hwpx_lines((7000, 1000))}</hp:p>"
        cell = builder._hwpx_cell(1, 1, "X").replace("<hp:p><hp:run><hp:t>X</hp:t></hp:run></hp:p>", inner)
        body = ('<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "번호") + builder._hwpx_cell(0, 1, "내용")
                + "</hp:tr><hp:tr>" + builder._hwpx_cell(1, 0, "1") + cell + "</hp:tr></hp:tbl></hp:run></hp:p>")

        text = _hwpx_document(body)

        assert text.index("ㅇ 기본 산출물 목록") < text.index("구분") < text.index("1년")


class TestBottomOfTheLines:
    def test_bottom_is_where_the_last_line_ends_under_the_top_of_the_first(self):
        assert _hangul.bottom_of_the_lines([(3930, 1500)]) == 1500
        assert _hangul.bottom_of_the_lines([(20000, 1500), (23160, 1400)]) == 4560
        assert _hangul.bottom_of_the_lines([(0, 1000), (0, 1000)]) == 1000

    def test_no_line_and_lines_out_of_order_give_none(self):
        assert _hangul.bottom_of_the_lines([]) is None
        assert _hangul.bottom_of_the_lines([(60000, 1500), (2000, 1500)]) is None
        assert _hangul.bottom_of_the_lines([(3930, -1)]) is None
        assert _hangul.bottom_of_the_lines([(100, 10), (300, 10), (200, 10)]) is None   # lower than the one before, not than the first

    def test_line_of_no_height_is_a_line(self):
        assert _hangul.bottom_of_the_lines([(3930, 0)]) == 0

    def test_lines_are_read_once_for_all_the_tables_of_a_paragraph(self, monkeypatch):
        """Many tables in one paragraph of many lines: the lines are not read again for each table."""
        calls = []
        real = _hangul.bottom_of_the_lines
        from opendockit.extractors import hwpx

        monkeypatch.setattr(hwpx, "bottom_of_the_lines", lambda lines: calls.append(1) or real(lines))

        _hwpx(*[_hwpx_table(2548)] * 5, _text(_LABEL), lines=_hwpx_lines((3930, 1500)))

        assert calls == [1]

    def test_hwp_lines_are_read_once_for_all_the_tables_of_a_paragraph(self, monkeypatch):
        calls = []
        real = _hangul.bottom_of_the_lines
        from opendockit.extractors import hwp

        monkeypatch.setattr(hwp, "bottom_of_the_lines", lambda lines: calls.append(1) or real(lines))

        text = _hwp(monkeypatch, (..., ..., ..., _LABEL), *[_hwp_table(1, 2548)] * 3, lines=_hwp_lines((3930, 1500)))

        assert calls == [1] and text == "\n\n".join([_LABEL, _TABLE, _TABLE, _TABLE])
