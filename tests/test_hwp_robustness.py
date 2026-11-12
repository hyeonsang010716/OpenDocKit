"""An odd part of an HWP/HWPX document does not get the whole document rejected: deep record chains, a broken section stream,
parts outside the body (header.xml, content.hpf, manifest, preview), very long numeric attributes, spans past the table.

HWP record streams are built with the fake OLE container (`_fake_hwp`) of `test_hwp.py`.
"""

from io import BytesIO
import struct
import zipfile
import zlib

import pytest

from opendockit import CorruptedDocumentError, convert
from tests import builder
from tests.test_hwp import (
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_CHAR_SHAPE,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    TAG_TABLE,
    _fake_hwp,
)


_HUGE = "9" * 5000  # more digits than int() accepts (over 4,300)


def _paragraph(text: str, level: int = 0) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, level, builder.hwp_para_header())
            + builder.hwp_record(TAG_PARA_TEXT, level + 1, builder.hwp_text(text)))


def _nested_tables(depth: int) -> bytes:
    """A body of tables nested `depth` deep, each cell holding a table. The text of the innermost cell is '가장 안'."""
    record = builder.hwp_record
    inner = _paragraph("가장 안", 2 * depth)
    for index in range(depth - 1, -1, -1):
        level = 2 * index
        inner = b"".join([
            record(TAG_PARA_HEADER, level, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, level + 1, builder.hwp_text((11, "tbl "))),
            record(TAG_CTRL_HEADER, level + 1, builder.hwp_ctrl("tbl ")),
            record(TAG_TABLE, level + 2, struct.pack("<IHH", 0, 1, 1) + b"\x00" * 10),
            record(TAG_LIST_HEADER, level + 2, builder.hwp_cell(0, 0)),
            _paragraph(f"칸{index}", level + 2),
            inner,
        ])
    return inner


def _rezipped(data: bytes, *, corrupt: str | None = None) -> bytes:
    """An HWPX with its parts stored again without compression. The `corrupt` part has one content byte changed so that its CRC
    does not match."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    packed = bytearray(buffer.getvalue())
    if corrupt is not None:
        start = packed.index(corrupt.encode()) + len(corrupt)  # first content byte after the local header
        packed[start + 2] ^= 0x20
    return bytes(packed)


class TestDeepRecordChains:
    """The record tree was flattened (`_flatten`) and descended (`HwpRenderer.render`) recursively, so a chain 1,000 levels deep
    gave RecursionError → the whole document was rejected."""

    def test_deep_chain_of_unknown_records_in_the_body_keeps_the_paragraphs_around_it(self, monkeypatch):
        chain = b"".join(builder.hwp_record(0x99, level, b"") for level in range(1000))
        data = _fake_hwp(monkeypatch, sections=[_paragraph("앞") + chain + _paragraph("뒤")])

        result = convert(data, "a.hwp")

        assert result.text == "앞\n\n뒤"

    def test_deep_chain_in_doc_info_does_not_reject_the_document(self, monkeypatch):
        chain = b"".join(builder.hwp_record(0x30, level, b"") for level in range(1023))
        data = _fake_hwp(monkeypatch, sections=[_paragraph("본문")], docinfo_extra=chain)

        result = convert(data, "a.hwp")

        assert result.text == "본문"

    def test_deep_chain_under_a_drawing_object_keeps_the_paragraph(self, monkeypatch):
        record = builder.hwp_record
        drawing = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("그림 앞", (11, "gso "), "뒤"))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso "))
                   + b"".join(record(0x4C, level, b"") for level in range(2, 1023)))
        data = _fake_hwp(monkeypatch, sections=[_paragraph("처음") + drawing])

        result = convert(data, "a.hwp")

        assert result.text == "처음\n\n그림 앞뒤"

    def test_tables_nested_too_deeply_keep_the_outer_levels_with_a_warning(self, monkeypatch):
        """Tables nested 400 deep. The outer levels are kept, and a warning says the inner part that is too deep was not read."""
        data = _fake_hwp(monkeypatch, sections=[_nested_tables(400) + _paragraph("표 뒤 문단")])

        result = convert(data, "a.hwp")

        assert "칸0" in result.text
        assert "가장 안" not in result.text
        assert result.text.endswith("표 뒤 문단")
        assert any("nested too deeply" in warning for warning in result.warnings)

    def test_tables_nested_within_the_limit_are_read_whole(self, monkeypatch):
        data = _fake_hwp(monkeypatch, sections=[_nested_tables(120)])

        result = convert(data, "a.hwp")

        assert "칸119" in result.text
        assert "가장 안" in result.text
        assert result.warnings == []


class TestBrokenSectionStream:
    """When the compression of one section stream was broken (zlib.error), the whole document was rejected even though Section0
    was intact."""

    def test_broken_second_section_keeps_the_first_with_a_warning(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, sections=[_paragraph("첫 구역")],
                         extra={"BodyText/Section1": b"\xff\xff\xff garbage" * 10})

        result = convert(data, "a.hwp")

        assert result.text == "첫 구역"
        assert result.warnings == ["Section 2: the compressed data is broken and could not be read."]

    def test_truncated_section_keeps_what_was_read_with_a_warning(self, monkeypatch):
        section = b"".join(_paragraph(f"문단 {index}") for index in range(50))
        truncated = zlib.compress(section)[2:-4][:60]
        data = _fake_hwp(monkeypatch, flags=1, sections=[_paragraph("다른 구역")], extra={"BodyText/Section1": truncated})

        result = convert(data, "a.hwp")

        assert result.text.startswith("다른 구역\n\n문단 0")
        assert result.warnings == ["Section 2: the stream is truncated, so only the part that could be read was converted."]

    def test_intact_compressed_sections_give_no_warning(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, sections=[_paragraph("가"), _paragraph("나")])

        result = convert(data, "a.hwp")

        assert result.text == "가\n\n나"
        assert result.warnings == []


class TestHwpxPartsOutsideTheBody:
    """With an intact body (section), an XML error in header.xml (styles) or a CRC error in content.hpf, the manifest or the
    preview got the whole document rejected."""

    def test_broken_header_xml_reads_the_body_without_styles(self):
        data = builder.zip_with(builder.hwpx_bytes(), edit={"Contents/header.xml": lambda xml: xml[:-20]})

        result = convert(data, "a.hwpx")

        assert "본문 탭과 각주[^1]입니다." in result.text
        assert "| 일정 | 3월 |" in result.text
        assert result.warnings == ["The document style information (header.xml) could not be read, so the document was converted "
                                   "without headings, numbering and character formatting."]

    @pytest.mark.parametrize("part", ["Contents/header.xml", "Contents/content.hpf", "META-INF/manifest.xml"])
    def test_part_with_a_bad_checksum_does_not_reject_the_document(self, part):
        data = _rezipped(builder.hwpx_bytes(), corrupt=part)

        result = convert(data, "a.hwpx")

        assert "본문 탭과 각주[^1]입니다." in result.text

    def test_preview_with_a_bad_checksum_is_not_needed_when_the_body_is_read(self):
        source = builder.zip_with(builder.hwpx_bytes(), add={"Preview/PrvText.txt": "미리보기 글"})
        data = _rezipped(source, corrupt="Preview/PrvText.txt")

        result = convert(data, "a.hwpx")

        assert "본문 탭과 각주[^1]입니다." in result.text
        assert result.warnings == []


class TestHugeNumbersInHangulDocuments:
    """A number of more than 4,300 digits that passed `isdecimal()` made `int()` raise ValueError, so the whole document was
    rejected. It is treated as an odd value and the default is used."""

    def test_hwpx_cell_address_with_too_many_digits_is_the_default(self):
        cell = builder._hwpx_cell(0, 0, "칸").replace('rowAddr="0"', f'rowAddr="{_HUGE}"')
        body = f'<hp:p><hp:run><hp:t>앞</hp:t><hp:tbl rowCnt="1" colCnt="1"><hp:tr>{cell}</hp:tr></hp:tbl></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "앞\n\n칸"  # a one-cell table is a box, so it is unwrapped into paragraphs

    def test_hwpx_auto_number_with_too_many_digits_is_left_out(self):
        body = f'<hp:p><hp:run><hp:t>표 </hp:t><hp:ctrl><hp:autoNum num="{_HUGE}" numType="TABLE"/></hp:ctrl><hp:t>현황</hp:t></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "표 현황"


class TestSpansPastTheTable:
    """When a span written in the file was larger than the table, that many fake rows and columns appeared (`markdown.TableGrid`
    clips it to the real extent)."""

    def test_hwpx_row_span_past_the_last_row_adds_no_rows(self):
        body = ('<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "구분")
                + builder._hwpx_cell(0, 1, "값") + "</hp:tr><hp:tr>" + builder._hwpx_cell(1, 0, "매출", row_span=5000)
                + builder._hwpx_cell(1, 1, "10") + "</hp:tr></hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "| 구분 | 값 |\n| --- | --- |\n| 매출 | 10 |"

    def test_hwpx_column_span_past_the_last_column_keeps_every_row(self):
        rows = "".join(f"<hp:tr>{builder._hwpx_cell(row, 0, f'행{row}')}{builder._hwpx_cell(row, 1, str(row))}</hp:tr>"
                       for row in range(1, 601))
        body = (f'<hp:p><hp:run><hp:tbl rowCnt="601" colCnt="2"><hp:tr>{builder._hwpx_cell(0, 0, "제목", col_span=10_000)}'
                f"</hp:tr>{rows}</hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text.count("<tr>") == 601  # heading row (colspan 2) + 600 rows
        assert "<tr><td>행600</td><td>600</td></tr>" in result.text
        assert result.warnings == []

    def test_hwpx_rows_left_out_by_the_grid_limit_are_reported(self, monkeypatch):
        """A table whose cell addresses are scattered along a diagonal has its bottom rows dropped by the grid cell limit.
        Formerly there was no warning. The limit (5 million cells) is lowered to 100 cells, seen as 20 rows × 20 columns."""
        monkeypatch.setattr("opendockit.markdown._MAX_GRID_CELLS", 100)
        cells = "".join(f"<hp:tr>{builder._hwpx_cell(index, index, 'x')}</hp:tr>" for index in range(20))
        body = f"<hp:p><hp:run><hp:tbl>{cells}</hp:tbl></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.warnings == ["The table is too large; the bottom 15 row(s) could not be converted."]


class TestCellsOnAnOccupiedAddress:
    """When a cell address pointed at a place taken by an earlier cell (two cells with the same address, or an earlier cell's
    span larger than the table), the later cell's text vanished without a warning. Like a cell that comes without an address
    (`TableGrid.place_next`), it goes to the next free place in that row (`TableGrid.place_or_next`)."""

    def test_hwpx_cell_with_a_duplicate_address_keeps_its_text(self):
        cells = ("<hp:tr>" + builder._hwpx_cell(0, 0, "가1") + builder._hwpx_cell(0, 0, "가2") + "</hp:tr><hp:tr>"
                 + builder._hwpx_cell(1, 0, "나1") + builder._hwpx_cell(1, 1, "나2") + "</hp:tr>")
        body = f'<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="2">{cells}</hp:tbl></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "| 가1 | 가2 |\n| --- | --- |\n| 나1 | 나2 |"

    def test_hwpx_cells_covered_by_a_span_past_the_table_keep_their_text(self):
        cells = ("<hp:tr>" + builder._hwpx_cell(0, 0, "가1", col_span=9) + builder._hwpx_cell(0, 1, "가2")
                 + builder._hwpx_cell(0, 2, "가3") + "</hp:tr><hp:tr>" + builder._hwpx_cell(1, 0, "나1")
                 + builder._hwpx_cell(1, 1, "나2") + builder._hwpx_cell(1, 2, "나3") + "</hp:tr>")
        body = f'<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="3">{cells}</hp:tbl></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert all(text in result.text for text in ("가1", "가2", "가3", "나1", "나2", "나3"))

    def test_hwp_cell_with_a_duplicate_address_keeps_its_text(self, monkeypatch):
        def cell_text(text):
            return [builder.hwp_record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
                    builder.hwp_record(TAG_PARA_TEXT, 3, builder.hwp_text(text))]

        record = builder.hwp_record
        cells = [(0, 0, "가1"), (0, 0, "가2"), (1, 0, "나1"), (1, 1, "나2")]
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
            record(TAG_TABLE, 2, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10),
            *(part for row, col, text in cells
              for part in (record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, col)), *cell_text(text))),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "| 가1 | 가2 |\n| --- | --- |\n| 나1 | 나2 |"


def _two_section_hwpx(second: str) -> bytes:
    """An HWPX with 2 sections. The content of the second section (section1.xml) is `second` as given."""
    return builder.zip_with(builder.hwpx_bytes("<hp:p><hp:run><hp:t>첫 구역 본문</hp:t></hp:run></hp:p>"), edit={
        "Contents/content.hpf": lambda xml: xml.replace(
            "</opf:manifest>", '<opf:item id="section1" href="Contents/section1.xml"/></opf:manifest>').replace(
            "</opf:spine>", '<opf:itemref idref="section1"/></opf:spine>'),
    }, add={"Contents/section1.xml": second})


class TestBrokenHwpxSection:
    """When the XML of one section (section*.xml) was broken or its CRC did not match, the whole document was rejected even
    though the other sections were intact. Like the section isolation of HWP (`TestBrokenSectionStream`), only that section is
    skipped, with a warning. If every section is broken it is a corrupted document, as before."""

    _SECOND = ('<?xml version="1.0"?><hs:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
               'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"><hp:p><hp:run><hp:t>둘째 구역</hp:t></hp:run></hp:p></hs:sec>')

    def test_broken_xml_in_the_second_section_keeps_the_first_with_a_warning(self):
        data = _two_section_hwpx(self._SECOND.replace("둘째 구역", "둘째 &amp 구역"))

        result = convert(data, "a.hwpx")

        assert result.text == "첫 구역 본문"
        assert result.warnings == ["Section 2: corrupted and could not be read."]

    def test_section_with_a_bad_checksum_keeps_the_others_with_a_warning(self):
        packed = bytearray(_rezipped(_two_section_hwpx(self._SECOND)))
        with zipfile.ZipFile(BytesIO(bytes(packed))) as archive:
            info = archive.getinfo("Contents/section1.xml")
        packed[info.header_offset + 30 + len(info.filename) + len(info.extra) + 5] ^= 0x20  # one content byte → CRC mismatch
        data = bytes(packed)

        result = convert(data, "a.hwpx")

        assert result.text == "첫 구역 본문"
        assert result.warnings == ["Section 2: corrupted and could not be read."]

    def test_intact_sections_are_read_in_order_without_a_warning(self):
        result = convert(_two_section_hwpx(self._SECOND), "a.hwpx")

        assert result.text == "첫 구역 본문\n\n둘째 구역"
        assert result.warnings == []

    def test_document_whose_every_section_is_broken_is_still_rejected(self):
        data = builder.zip_with(builder.hwpx_bytes(), edit={"Contents/section0.xml": lambda xml: xml[:-30]})

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.hwpx")


class TestHugeParagraphText:
    """Paragraph text (PARA_TEXT) was unpacked into a Python integer per character (tuple, list), and the character limit was
    only checked after the whole paragraph was unpacked. One paragraph of 24 million characters used 1.9GB. The spans between
    control characters are decoded as bytes."""

    def test_huge_paragraph_is_decoded_without_an_object_per_character(self):
        import tracemalloc

        from opendockit.extractors.hwp import decode_para_text

        payload = "가".encode("utf-16-le") * 2_000_000
        tracemalloc.start()

        try:
            parts = decode_para_text(payload)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert parts == ["가" * 2_000_000]
        assert peak < 4 * len(payload)  # an integer per character would take more than 20 times

    def test_bytes_that_look_like_a_control_across_two_characters_are_text(self):
        """Bytes spanning two characters, such as 'A' (41 00) followed by '가' (00 AC), may look like a control character (1f or
        below + 00), but they are text unless they sit on a character boundary."""
        from opendockit.extractors.hwp import decode_para_text

        tab = struct.pack("<H", 9) + b"\x00" * 12 + struct.pack("<H", 9)
        payload = ("A가Ā가".encode("utf-16-le") + tab + "B".encode("utf-16-le") + struct.pack("<H", 24)
                   + "\U0001F600".encode("utf-16-le") + builder.hwp_text((11, "tbl "))[:-2] + "끝".encode("utf-16-le"))

        parts = decode_para_text(payload)

        assert parts == ["A가Ā가\tB-\U0001F600", 11, "끝"]

    def test_character_shapes_still_mark_runs_that_span_a_tab(self, monkeypatch):
        """A character shape span that continues across a tab is marked as one span, and it is not broken at a boundary to
        another character shape that gives the same mark."""
        from tests.test_hwp import TAG_CHAR_SHAPE

        def char_shape(attribute):
            return builder.hwp_record(TAG_CHAR_SHAPE, 0, b"\x00" * 42 + struct.pack("<iI", 1000, attribute) + b"\x00" * 18)

        tab = struct.pack("<H", 9) + b"\x00" * 12 + struct.pack("<H", 9)
        text = "x".encode("utf-16-le") + "ab".encode("utf-16-le") + tab + "cd".encode("utf-16-le") + "y".encode("utf-16-le")
        positions = [(0, 0), (1, 1), (4, 2), (11, 1), (13, 0)]  # 'ab' + tab: superscript 1, after the tab: superscript 2 (same mark), 'cd': 1
        section = b"".join([
            builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            builder.hwp_record(TAG_PARA_TEXT, 1, text),
            builder.hwp_record(TAG_PARA_CHAR_SHAPE, 1, b"".join(struct.pack("<II", *pair) for pair in positions)),
        ])
        docinfo = char_shape(0) + char_shape(1 << 15) + char_shape(1 << 15)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "x^{ab cd}y"  # clean_text turns the tab into a space
