"""An odd part of an HWP/HWPX document does not get the whole document rejected: deep record chains, a broken section stream,
parts outside the body (header.xml, content.hpf, manifest, preview), very long numeric attributes, spans past the table.

HWP record streams are put into the OLE container of `_fake_hwp` of `test_hwp.py`.
"""

from io import BytesIO
import struct
import tracemalloc
import zipfile
import zlib

import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, DocumentTooLargeError, convert
from tests import builder
from tests.test_hwp import (
    TAG_BIN_DATA,
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_CHAR_SHAPE,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    TAG_SHAPE_PICTURE,
    TAG_STYLE,
    TAG_TABLE,
    _fake_hwp,
)
from tests.timing import allowed, fastest


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


class TestBrokenDocInfo:
    """DocInfo holds styles, numbering and the picture list, not text. Its broken compression rejected the whole document even
    though the sections were intact (HWPX keeps the body when header.xml is broken)."""

    def test_broken_doc_info_keeps_the_body_with_a_warning(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, sections=[_paragraph("본문")], extra={"DocInfo": b"\xff\xff\xff garbage" * 10})

        result = convert(data, "a.hwp")

        assert result.text == "본문"
        assert result.warnings == ["The document style information (DocInfo) could not be read, so the document was converted "
                                   "without headings, numbering, character formatting and pictures."]

    def test_broken_doc_info_and_broken_sections_are_still_a_corrupted_document(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, extra={"DocInfo": b"\xff garbage" * 10, "BodyText/Section0": b"\xff garbage" * 10})

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.hwp")


class TestBrokenDocInfoRecordsKeepTheirPlace:
    """Style and BinItem IDs are positions in DocInfo. A STYLE or BIN_DATA record shorter than 2 bytes was skipped, so every later
    style took the name of the next one and every later picture the stream of the next one. It keeps its place, empty."""

    def test_broken_style_record_does_not_shift_later_styles(self, monkeypatch):
        # _fake_hwp gives styles 0-2; 3 is broken and 4 is an outline style.
        docinfo = builder.hwp_record(TAG_STYLE, 0, b"\x01") + builder.hwp_record(TAG_STYLE, 0, builder.hwp_style("개요 1"))
        section = (builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header(style_id=4))
                   + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text("Chapter one")))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        assert convert(data, "a.hwp").text == "# Chapter one"

    def test_broken_bin_data_record_does_not_shift_later_pictures(self, monkeypatch):
        record = builder.hwp_record
        docinfo = record(TAG_BIN_DATA, 0, b"\x01") + record(TAG_BIN_DATA, 0, struct.pack("<HHH", 0x1, 2, 3) + "png".encode("utf-16-le"))
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("앞", (11, "gso ")))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso "))
                   + record(TAG_SHAPE_PICTURE, 2, b"\x00" * 71 + struct.pack("<H", 2) + b"\x00" * 18))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo, extra={"BinData/BIN0002.png": builder.png_bytes()})

        result = convert(data, "a.hwp")

        assert len(result.images) == 1


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

    def test_many_tables_of_huge_spans_share_one_budget(self, monkeypatch):
        """The span budget was per table: 50 small tables whose 50 empty cells each span 100×100 filled 500,000 cells apiece
        (a 5.6 KB file: 13 s, no text, no warning). The tables of one conversion share one budget, and a table after them is
        still drawn."""
        from opendockit.markdown import _CONVERSION_SPAN_BUDGET, TableGrid

        filled: list[int] = []
        place = TableGrid.place

        def counting(self, *args, **kwargs):
            spans = place(self, *args, **kwargs)
            filled.append(spans[0] * spans[1])
            return spans

        monkeypatch.setattr(TableGrid, "place", counting)
        cells = "".join(builder._hwpx_cell(0, col * 100, "", row_span=100, col_span=100) for col in range(50))
        table = f'<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="5000"><hp:tr>{cells}</hp:tr></hp:tbl></hp:run></hp:p>'
        last = ('<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "끝")
                + builder._hwpx_cell(0, 1, "값") + "</hp:tr></hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes("<hp:p><hp:run><hp:t>title</hp:t></hp:run></hp:p>" + table * 50 + last), "a.hwpx")

        assert result.text == "title\n\n| 끝 | 값 |\n| --- | --- |"
        assert sum(filled) <= _CONVERSION_SPAN_BUDGET + len(filled)  # per table it was 50 × 500,000 cells


    @pytest.mark.timing
    @pytest.mark.parametrize("binary", [False, True])
    def test_far_apart_spans_of_many_tables_take_little_time(self, monkeypatch, binary):
        """One cell at column 65,536 spanning 10,000 rows and one at row 65,536 spanning 1,000 columns, each within the span
        limits, made a grid of 10,000 × 1,001 cells per table: a 400-byte table took 6 s, ten of them 63 s (HWP the same,
        with 65,535 rows and columns declared). The grid is built from the rows and columns that hold text."""
        record = builder.hwp_record
        if binary:
            cells = [(0, 1000, "A", 10_000, 1), (65_534, 0, "B", 1, 1000)]
            table = b"".join([
                record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
                record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
                record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
                record(TAG_TABLE, 2, struct.pack("<IHH", 0, 65_535, 65_535) + b"\x00" * 10),
                *(part for row, col, text, row_span, col_span in cells
                  for part in (record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, col, row_span=row_span, col_span=col_span)),
                               _paragraph(text, 2))),
            ])
            data, name = _fake_hwp(monkeypatch, sections=[table * 3]), "a.hwp"
        else:
            table = ("<hp:p><hp:run><hp:tbl><hp:tr>" + builder._hwpx_cell(0, 65_536, "A", row_span=10_000)
                     + builder._hwpx_cell(65_536, 0, "B", col_span=1_000) + "</hp:tr></hp:tbl></hp:run></hp:p>")
            data, name = builder.hwpx_bytes(table * 3), "a.hwpx"

        seconds = fastest(lambda: convert(data, name))
        result = convert(data, name)

        assert seconds < allowed(1.0)
        assert result.warnings == []
        assert result.text.count("| B | |") == 3

class TestNotesCountTowardTheLimit:
    """Footnotes are written after the body, and their text was not counted while the body was read: a document whose text is
    in its notes was read to its end whatever `max_chars` said (9.6 million characters of notes took 3.7 s, the same text in
    the body 0.2 s). The notes a paragraph adds count with it."""

    _NOTE = "각주 내용 " * 20  # 80 characters that count

    def test_hwpx_stops_reading_at_the_limit(self):
        paragraph = ('<hp:p><hp:run><hp:t>a</hp:t><hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>' + self._NOTE
                     + "</hp:t></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(paragraph * 200), "a.hwpx", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.text.count("[^") < 2 * 40  # the references and definitions of about 25 notes, not of 200

    def test_hwp_stops_reading_at_the_limit(self, monkeypatch):
        record = builder.hwp_record
        paragraph = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("a", (17, "fn  "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("fn  ")),
            record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
            _paragraph(self._NOTE, 2),
        ])
        data = _fake_hwp(monkeypatch, sections=[paragraph * 200])

        result = convert(data, "a.hwp", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.text.count("[^") < 2 * 40

    def test_notes_are_not_measured_without_a_limit(self, monkeypatch):
        """Measuring every note made a document of 60,000 notes 30% slower; without a limit nothing is compared, so nothing is
        measured."""
        from opendockit.extractors import hwp, hwpx

        record = builder.hwp_record
        paragraph = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("a", (17, "fn  "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("fn  ")),
            record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
            _paragraph(self._NOTE, 2),
        ])
        hwp_data = _fake_hwp(monkeypatch, sections=[paragraph * 20])
        hwpx_data = builder.hwpx_bytes(('<hp:p><hp:run><hp:t>a</hp:t><hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>'
                                        + self._NOTE + "</hp:t></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl></hp:run></hp:p>") * 20)
        expected = {name: convert(data, name, ConvertOptions(max_chars=None)).text for name, data in (("a.hwp", hwp_data), ("a.hwpx", hwpx_data))}
        measured = []
        for module in (hwp, hwpx):
            monkeypatch.setattr(module, "solid_length", lambda text: measured.append(text) or len(text))

        for name, data in (("a.hwp", hwp_data), ("a.hwpx", hwpx_data)):
            assert convert(data, name, ConvertOptions(max_chars=None)).text == expected[name]
        assert measured == []


class TestManyMembersAndReferences:
    """Each chart and picture a section refers to was looked up in the list of the archive's members: 40,000 references to as many
    members (3.7 MB) took 5 s. The members are looked up in constant time."""

    @staticmethod
    def _hwpx(count: int) -> bytes:
        body = ("<hp:p><hp:run>" + "".join(f'<hp:chart chartIDRef="Chart/c{index}.xml"/>' for index in range(count))
                + "</hp:run><hp:run><hp:t>끝</hp:t></hp:run></hp:p>")
        output = BytesIO()
        with zipfile.ZipFile(BytesIO(builder.hwpx_bytes(body))) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            for index in range(count):
                target.writestr(zipfile.ZipInfo(f"x/{index}"), b"")
        return output.getvalue()

    @pytest.mark.timing
    def test_references_to_many_members_take_linear_time(self):
        small, large = self._hwpx(5_000), self._hwpx(20_000)

        assert fastest(lambda: convert(large, "a.hwpx")) < 8 * fastest(lambda: convert(small, "a.hwpx")) + 0.05
        assert convert(large, "a.hwpx").text.endswith("끝")


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

    @pytest.mark.parametrize(("unit", "text"), [(10, "\n"), (24, "-"), (30, " "), (31, " ")])
    def test_paragraph_of_char_controls_is_decoded_in_one_go(self, unit, text):
        """A char control of one WCHAR that stands for a character (line break, hyphen, fixed-width space) was taken one at a time,
        making a bytes object per character again: 63 times the paragraph, and 60 MB of line breaks in a 57 KB file took 13 s
        and 4.6 GB. They are decoded with the text around them."""
        import tracemalloc

        from opendockit.extractors.hwp import decode_para_text

        payload = struct.pack("<H", unit) * 2_000_000
        tracemalloc.start()

        try:
            parts = decode_para_text(payload)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert parts == [text * 2_000_000]
        assert peak < 4 * len(payload)

    def test_char_controls_read_the_same_inside_text(self):
        """Replaced in the decoded text, the char controls give what they gave one at a time: a hyphen, a space, a line break,
        or nothing (paragraph end, null, reserved), and a dropped one between two parts of a marked run does not split the run."""
        from opendockit.extractors.hwp import decode_para_text

        def units(*values):
            return b"".join(value.encode("utf-16-le") if isinstance(value, str) else struct.pack("<H", value) for value in values)

        superscript = ("^", False, False, False)

        assert decode_para_text(units("a", 24, "b", 30, "c", 31, "d", 10, "e", 0, 13, 25, 29, "f", 13)) == ["a-b c d\nef"]
        assert decode_para_text(units(13)) == []
        assert (decode_para_text(units("ab", 0, "cd"), [(0, superscript), (2, (None, False, False, False)), (3, superscript)])
                == decode_para_text(units("abcd"), [(0, superscript)]))

    @pytest.mark.timing
    @pytest.mark.parametrize("payload", [struct.pack("<H", 13) * 2_000_000, "Ā".encode("utf-16-le") * 2_000_000],
                             ids=["dropped controls", "bytes like a control across two characters"])
    def test_paragraph_without_text_or_with_control_like_bytes_takes_as_long_as_text(self, payload):
        """A paragraph of char controls that are dropped (paragraph end, null) went round the loop once per character: 4 MB took
        0.44 s, a 190 KB file of 200 MB took 24 s, and nothing of it counts toward the character limit. Text whose bytes look
        like a control across two characters ('Ā' is 00 01, so 'ĀĀ' holds 01 00 at an odd byte) did the same, 4.5 s for
        4 MB. Both take as long as ordinary text now."""
        from opendockit.extractors.hwp import decode_para_text

        text = "가".encode("utf-16-le") * 2_000_000
        assert fastest(lambda: decode_para_text(payload)) < 5 * fastest(lambda: decode_para_text(text)) + 0.01

    def test_unordered_shape_positions_past_the_text_are_not_sorted(self):
        """Character shape positions that do not increase were sorted as tuples before those past the text were dropped: 10
        million pairs in a 114 KB section past a one-character text took 3 s and 1 GB. Only positions inside the text are kept
        before ordering; at one position the larger shape ID still wins."""
        import tracemalloc

        from opendockit import ConvertOptions
        from opendockit.extractors.hwp import TAG_PARA_CHAR_SHAPE, HwpRenderer, Record

        payload = b"".join(struct.pack("<II", position, 1) for position in range(1_000_000, 0, -1))
        paragraph = Record(TAG_PARA_HEADER, 0, b"", [Record(TAG_PARA_CHAR_SHAPE, 1, payload)])
        renderer = HwpRenderer([], ConvertOptions(), scripts={1: "^"})
        tracemalloc.start()

        try:
            runs = renderer._shape_runs(paragraph, 1)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert runs == []
        assert peak < 4 * len(payload)  # the record read as an array and split in two; sorted tuples took 13 times
        unordered = Record(TAG_PARA_HEADER, 0, b"", [Record(TAG_PARA_CHAR_SHAPE, 1, struct.pack("<6I", 2, 0, 0, 0, 0, 1))])
        assert HwpRenderer([], ConvertOptions(), scripts={1: "^"})._shape_runs(unordered, 5) == [(0, ("^", False, False, False)), (2, (None, False, False, False))]

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


def _unreadable(data: bytes, name: str, *, encrypted: bool) -> bytes:
    """An HWPX whose member `name` cannot be read at all: its compression method is one zipfile does not know
    (NotImplementedError), or its ZIP encryption flag is set (RuntimeError)."""
    packed = bytearray(_rezipped(data))
    with zipfile.ZipFile(BytesIO(bytes(packed))) as archive:
        info = archive.getinfo(name)
    for offset, flag_at, method_at in [(info.header_offset, 6, 8)] + [
            (start, 8, 10) for start in _central_entries(packed) if packed[start + 46:start + 46 + len(name)] == name.encode()]:
        if encrypted:
            struct.pack_into("<H", packed, offset + flag_at, 1)
        else:
            struct.pack_into("<H", packed, offset + method_at, 99)
    return bytes(packed)


def _central_entries(packed: bytearray) -> list[int]:
    starts, at = [], packed.find(b"PK\x01\x02")
    while at != -1:
        starts.append(at)
        at = packed.find(b"PK\x01\x02", at + 4)
    return starts


class TestHwpxUnreadableMembers:
    """A member with an unknown compression method (NotImplementedError) or the ZIP encryption flag (RuntimeError) rejected the
    whole document: only bad compression and checksums were caught. Like those, only that member is skipped."""

    @pytest.mark.parametrize("encrypted", [False, True])
    def test_unreadable_second_section_keeps_the_first_with_a_warning(self, encrypted):
        data = _unreadable(_two_section_hwpx(TestBrokenHwpxSection._SECOND), "Contents/section1.xml", encrypted=encrypted)

        result = convert(data, "a.hwpx")

        assert result.text == "첫 구역 본문"
        assert result.warnings == ["Section 2: corrupted and could not be read."]

    @pytest.mark.parametrize("encrypted", [False, True])
    def test_unreadable_header_xml_keeps_the_body(self, encrypted):
        data = _unreadable(builder.hwpx_bytes(), "Contents/header.xml", encrypted=encrypted)

        assert "본문 탭과 각주[^1]입니다." in convert(data, "a.hwpx").text

    def test_unreadable_picture_skips_only_that_picture(self):
        data = _unreadable(builder.hwpx_with_images(), "BinData/image1.png", encrypted=False)

        result = convert(data, "a.hwpx")

        assert result.text.count("[Image") == 1


def _deflate(data: bytes) -> bytes:
    return zlib.compress(data)[2:-4]


def _peak_memory(action) -> int:
    tracemalloc.start()
    try:
        action()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class TestStreamsLargerThanTheFile:
    """olefile reads a stream whole when it is opened, as many sectors as its directory entry declares, and only notes a defect
    when the sector chain loops: a 26.5 KB HWP whose Section0 chain pointed at itself with a declared size of 2 GB made 5.4 GB in
    memory and converted 'successfully'. A stream cannot hold more than the file, so it is read no further than that."""

    _STREAMS = {"FileHeader": builder.hwp_file_header(1), "DocInfo": _deflate(b""), "PrvText": "미리보기".encode("utf-16-le"),
                "\x05HwpSummaryInformation": b"", "BodyText/Section0": _deflate(_paragraph("본문"))}

    @pytest.mark.parametrize("looped", ["FileHeader", "DocInfo", "PrvText", "\x05HwpSummaryInformation", "BodyText/Section0"])
    def test_stream_that_loops_on_itself_is_read_no_further_than_the_file(self, looped):
        # every stream takes regular sectors (4,096 bytes or more), the only ones whose declared size olefile reads in full
        streams = {name: body.ljust(4096, b"\x00") for name, body in self._STREAMS.items()}
        data = builder.ole_file(streams, loops=(looped,), sizes={looped: 200_000_000})
        results = []

        peak = _peak_memory(lambda: results.append(convert(data, "a.hwp")))

        assert results[0].text == "본문"
        assert peak < 20_000_000

    def test_mini_stream_that_loops_on_itself_is_read_no_further_than_the_file(self):
        """The streams under 4,096 bytes are read from the mini stream, which olefile reads first, as large as the root entry
        says."""
        data = builder.ole_file(self._STREAMS, loops=("Root Entry",), sizes={"Root Entry": 200_000_000})
        results = []

        peak = _peak_memory(lambda: results.append(convert(data, "a.hwp")))

        assert results[0].text == "본문"
        assert peak < 20_000_000

    def test_compressed_stream_counts_as_stored_toward_the_limit(self):
        """Only what a compressed stream inflates to was counted: 1,000 section entries on one 4 MB stream holding a short deflate
        stream and then zeros read 4 GB in 5 s under a limit of 10 MB, without an error."""
        streams = {"FileHeader": builder.hwp_file_header(1), "DocInfo": _deflate(b""),
                   "BodyText/Section0": _deflate(_paragraph("본문")) + bytes(1_000_000)}
        shared = {f"BodyText/Section{index}": "BodyText/Section0" for index in range(1, 20)}
        data = builder.ole_file(streams, shared=shared)

        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=5_000_000))
        assert convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=25_000_000)).text == "\n\n".join(["본문"] * 20)


class TestManyDirectoryEntries:
    """olefile finds a stream by walking the children of each storage, so opening each of n sections took the square of n
    (8,000 sections: 3.4 s, 16,000: 27 s). The names are looked up once, in any case as olefile does."""

    @staticmethod
    def _document(count: int) -> bytes:
        streams = {"FileHeader": builder.hwp_file_header(1), "DocInfo": _deflate(b"")}
        return builder.ole_file(streams | {f"BodyText/Section{index}": _deflate(_paragraph("가")) for index in range(count)})

    def test_stream_names_are_found_in_any_case(self):
        streams = {"FileHeader": builder.hwp_file_header(1), "DOCINFO": _deflate(builder.hwp_record(TAG_STYLE, 0, builder.hwp_style("개요 1"))),
                   "BodyText/Section0": _deflate(_paragraph("본문"))}

        assert convert(builder.ole_file(streams), "a.hwp").text == "# 본문"  # its first style, read from DocInfo, is an outline

    @pytest.mark.timing
    def test_many_sections_take_linear_time(self):
        small, large = self._document(2_000), self._document(8_000)

        assert convert(large, "a.hwp").text.count("가") == 8_000
        assert fastest(lambda: convert(large, "a.hwp")) < 8 * fastest(lambda: convert(small, "a.hwp"))
