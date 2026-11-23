"""The caption of a table, a picture or another object of a Hangul document stands where the document sets it — above for a
top or left caption, under for a bottom or right one — and is written the same from HWP and from HWPX.

Hangul's own default is a caption under the object: of the table captions of 2,033 HWPX files 1,547 are set under their
table and 282 above it. Both readers wrote the caption of a table above it whatever its side, the HWP reader wrote the
caption of a picture under it whatever its side, and the HWPX reader set the caption of a picture inside the paragraph the
picture hangs on, where it ran into the sentence before it ('… 순서였음. 그림 32. EU 국가의 …').
"""

import struct

import pytest

from opendockit import convert
from opendockit.extractors.hwp import (
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    TAG_SHAPE_PICTURE,
    TAG_TABLE,
)
from tests import builder
from tests.test_hwp import TAG_BIN_DATA, _fake_hwp
from tests.test_hwp_heads_and_shapes import _HWP_PICTURE, _hwpx_picture


_SIDES = {"TOP": 2, "BOTTOM": 3, "LEFT": 0, "RIGHT": 1}   # the side of an HWPX caption and the direction of an HWP one
_TABLE = "| 항목 | 금액 |\n| --- | --- |\n| 인건비 | 100 |"


def _hwpx_caption(text: str, side: str | None) -> str:
    paragraphs = "".join(f"<hp:p><hp:run><hp:t>{line}</hp:t></hp:run></hp:p>" for line in text.split("\n"))
    return f"<hp:caption{'' if side is None else f' side=\"{side}\"'}><hp:subList>{paragraphs}</hp:subList></hp:caption>"


def _hwpx_table(caption: str, side: str | None) -> bytes:
    cells = "".join(f"<hp:tr>{builder._hwpx_cell(row, 0, left)}{builder._hwpx_cell(row, 1, right)}</hp:tr>"
                    for row, (left, right) in enumerate((("항목", "금액"), ("인건비", "100"))))
    body = ("<hp:p><hp:run><hp:t>앞 문단.</hp:t></hp:run></hp:p>"
            f'<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="2">{_hwpx_caption(caption, side)}{cells}</hp:tbl></hp:run></hp:p>'
            "<hp:p><hp:run><hp:t>뒤 문단.</hp:t></hp:run></hp:p>")
    return builder.hwpx_bytes(body)


def _hwp_paragraph(level: int, text) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, level, builder.hwp_para_header())
            + builder.hwp_record(TAG_PARA_TEXT, level + 1, builder.hwp_text(text)))


def _hwp_caption(text: str, direction: int | None) -> bytes:
    """The LIST_HEADER of a caption (its direction in bits 0-1 of the attribute 8 bytes in) and its paragraphs."""
    header = b"\x01\x00" + b"\x00" * 6 + (b"" if direction is None else struct.pack("<I", direction))
    return builder.hwp_record(TAG_LIST_HEADER, 2, header) + b"".join(_hwp_paragraph(2, line) for line in text.split("\n"))


def _hwp_table(monkeypatch, caption: str, direction: int | None) -> bytes:
    record = builder.hwp_record
    cells = b"".join(record(TAG_LIST_HEADER, 2, builder.hwp_cell(index // 2, index % 2)) + _hwp_paragraph(2, text)
                     for index, text in enumerate(("항목", "금액", "인건비", "100")))
    section = (_hwp_paragraph(0, "앞 문단.") + _hwp_paragraph(0, (11, "tbl ")) + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl "))
               + _hwp_caption(caption, direction) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10) + cells
               + _hwp_paragraph(0, "뒤 문단."))
    return _fake_hwp(monkeypatch, sections=[section])


def _hwpx_pictured(caption: str, side: str | None, text: str = "조직은 다음과 같다.") -> bytes:
    picture = _hwpx_picture().replace("</hp:pic>", _hwpx_caption(caption, side) + "</hp:pic>")
    body = (f"<hp:p><hp:run><hp:t>{text}</hp:t>{picture}</hp:run></hp:p>"
            "<hp:p><hp:run><hp:t>뒤 문단.</hp:t></hp:run></hp:p>")
    return builder.hwpx_with_picture(body)


def _hwp_pictured(monkeypatch, caption: str, direction: int | None, text: str = "조직은 다음과 같다.") -> bytes:
    """A paragraph with a picture (a drawing object, gso): the caption's LIST_HEADER and paragraphs stand right under the
    control, before the shape."""
    record = builder.hwp_record
    picture = b"\x00" * 71 + struct.pack("<H", 1) + b"\x00" * 18
    section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text(text, (11, "gso ")))
               + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")) + _hwp_caption(caption, direction)
               + record(TAG_SHAPE_PICTURE, 2, picture) + _hwp_paragraph(0, "뒤 문단."))
    return _fake_hwp(monkeypatch, sections=[section], docinfo_extra=record(TAG_BIN_DATA, 0, _HWP_PICTURE),
                     extra={"BinData/BIN0001.png": builder.png_bytes(seed=11)})


class TestTableCaptions:
    @pytest.mark.parametrize("side", ["BOTTOM", "RIGHT"])
    def test_hwpx_caption_set_under_the_table_is_written_under_it(self, side):
        text = convert(_hwpx_table("자료: 통계청", side), "a.hwpx").text

        assert text == f"앞 문단.\n\n{_TABLE}\n\n자료: 통계청\n\n뒤 문단."

    @pytest.mark.parametrize("side", ["TOP", "LEFT", None])
    def test_hwpx_caption_set_above_the_table_or_nowhere_is_written_above_it(self, side):
        text = convert(_hwpx_table("표 1. 예산", side), "a.hwpx").text

        assert text == f"앞 문단.\n\n표 1. 예산\n\n{_TABLE}\n\n뒤 문단."

    @pytest.mark.parametrize("side", ["BOTTOM", "RIGHT"])
    def test_hwp_caption_set_under_the_table_is_written_under_it(self, monkeypatch, side):
        text = convert(_hwp_table(monkeypatch, "자료: 통계청", _SIDES[side]), "a.hwp").text

        assert text == f"앞 문단.\n\n{_TABLE}\n\n자료: 통계청\n\n뒤 문단."

    @pytest.mark.parametrize("direction", [_SIDES["TOP"], _SIDES["LEFT"], None])
    def test_hwp_caption_set_above_the_table_or_nowhere_is_written_above_it(self, monkeypatch, direction):
        text = convert(_hwp_table(monkeypatch, "표 1. 예산", direction), "a.hwp").text

        assert text == f"앞 문단.\n\n표 1. 예산\n\n{_TABLE}\n\n뒤 문단."

    def test_a_note_in_a_caption_under_the_table_is_numbered_after_the_notes_of_the_cells(self):
        note = ("<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>{}</hp:t></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl>")
        data = builder.zip_with(_hwpx_table("자료: 통계청", "BOTTOM"), edit={"Contents/section0.xml": lambda xml: xml.replace(
            "<hp:t>자료: 통계청</hp:t>", "<hp:t>자료: 통계청</hp:t>" + note.format("캡션의 주")).replace(
            "<hp:t>인건비</hp:t>", "<hp:t>인건비</hp:t>" + note.format("칸의 주"))})

        text = convert(data, "a.hwpx").text

        assert "| 인건비[^1] | 100 |" in text and "자료: 통계청[^2]" in text
        assert text.index("[^1]: 칸의 주") < text.index("[^2]: 캡션의 주")


class TestPictureCaptions:
    def test_hwpx_caption_under_a_picture_is_a_block_of_its_own_under_it(self):
        text = convert(_hwpx_pictured("그림 1. 조직도", "BOTTOM"), "a.hwpx").text

        assert text == "조직은 다음과 같다.\n\n[Image]\n\n그림 1. 조직도\n\n뒤 문단."

    def test_hwpx_caption_above_a_picture_is_a_block_of_its_own_above_it(self):
        text = convert(_hwpx_pictured("그림 1. 조직도", "TOP"), "a.hwpx").text

        assert text == "조직은 다음과 같다.\n\n그림 1. 조직도\n\n[Image]\n\n뒤 문단."

    def test_hwp_caption_under_a_picture(self, monkeypatch):
        text = convert(_hwp_pictured(monkeypatch, "그림 1. 조직도", _SIDES["BOTTOM"]), "a.hwp").text

        assert text == "조직은 다음과 같다.\n\n[Image]\n\n그림 1. 조직도\n\n뒤 문단."

    def test_hwp_caption_above_a_picture(self, monkeypatch):
        text = convert(_hwp_pictured(monkeypatch, "그림 1. 조직도", _SIDES["TOP"]), "a.hwp").text

        assert text == "조직은 다음과 같다.\n\n그림 1. 조직도\n\n[Image]\n\n뒤 문단."

    @pytest.mark.parametrize("side", ["TOP", "BOTTOM", "LEFT", "RIGHT"])
    def test_hwp_and_hwpx_write_a_captioned_picture_alike(self, monkeypatch, side):
        caption = "그림 1. 조직도\n출처: 인사혁신처"

        assert convert(_hwp_pictured(monkeypatch, caption, _SIDES[side]), "a.hwp").text \
            == convert(_hwpx_pictured(caption, side), "a.hwpx").text

    def test_picture_without_a_caption_stays_in_its_paragraph(self):
        body = f"<hp:p><hp:run><hp:t>조직은 다음과 같다.</hp:t>{_hwpx_picture()}<hp:t>이상이다.</hp:t></hp:run></hp:p>"

        assert convert(builder.hwpx_with_picture(body), "a.hwpx").text == "조직은 다음과 같다. 이상이다.\n\n[Image]"
