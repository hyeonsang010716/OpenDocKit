"""HWPX (XML) and HWP 5.x (binary) conversion.

No Python library can build an OLE compound document, so for HWP only the OLE container is faked, while the record streams
inside it are built in the real format. Results checked against real Hangul (HWP) files are in docs/design.md.
"""

from io import BytesIO
import struct
import time
import zlib

import olefile
import pytest

from opendockit import (
    ConvertOptions,
    DocumentFormat,
    DocumentTooLargeError,
    EncryptedDocumentError,
    convert,
)
from opendockit.extractors.hwp import (
    TAG_BIN_DATA,
    TAG_BULLET,
    TAG_CHAR_SHAPE,
    TAG_CTRL_HEADER,
    TAG_EQEDIT,
    TAG_LIST_HEADER,
    TAG_NUMBERING,
    TAG_PARA_CHAR_SHAPE,
    TAG_PARA_HEADER,
    TAG_PARA_SHAPE,
    TAG_PARA_TEXT,
    TAG_SHAPE_OLE,
    TAG_SHAPE_PICTURE,
    TAG_STYLE,
    TAG_TABLE,
    decode_para_text,
    parse_records,
)
from tests import builder


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class TestHwpx:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        return convert(builder.hwpx_bytes(), "계획서.hwpx")

    def test_outline_style_becomes_heading(self, result):
        assert result.text.startswith("# 1. 사업 개요")

    def test_keeps_footnote_as_markdown_footnote(self, result):
        assert "본문 탭과 각주[^1]입니다." in result.text
        assert "[^1]: 각주 내용" in result.text

    def test_table_uses_cell_addresses_and_repeats_vertical_merge(self, result):
        assert "| 구분 | 내용 |\n| --- | --- |\n| 일정 | 3월 |\n| 일정 | 4월 |" in result.text

    def test_includes_text_box_and_skips_picture_comment(self, result):
        """A picture's shapeComment is the noise Hangul inserts automatically: '그림입니다. 원본 그림의 이름…'."""
        assert "글상자 안 글" in result.text
        assert "그림입니다" not in result.text

    def test_reads_title_metadata(self, result):
        assert result.metadata["title"] == "한글 문서"
        assert result.format is DocumentFormat.HWPX

    def test_encrypted_hwpx_raises(self):
        with pytest.raises(EncryptedDocumentError):
            convert(builder.hwpx_bytes(encrypted=True), "암호.hwpx")


class TestHwpxObjects:
    def test_chart_data_is_kept(self):
        """hp:chart points to an OOXML chart (Chart/chart1.xml). It was treated as a skipped object, so its values were dropped."""
        body = ("<hp:p><hp:run><hp:t>판매 현황:</hp:t><hp:switch><hp:case hp:required-namespace="
                '"http://www.hancom.co.kr/hwpml/2016/ooxmlchart"><hp:chart chartIDRef="Chart/chart1.xml"/></hp:case>'
                '<hp:default><hp:ole binaryItemIDRef="ole1"/></hp:default></hp:switch></hp:run></hp:p>')
        data = builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": _CHART_XML})

        result = convert(data, "a.hwpx")

        assert result.text == "판매 현황:\n\n[Chart] 판매\n\n| Category | 판매량 |\n| --- | --- |\n| 1월 | 5 |\n| 2월 | 7 |"

    def test_picture_and_equation_captions_are_kept(self):
        """Only table captions were read, so the captions (hp:caption) of pictures and equations were dropped."""
        body = ('<hp:p><hp:run><hp:pic><hp:shapeComment>그림입니다.</hp:shapeComment><hp:caption side="BOTTOM">'
                "<hp:subList><hp:p><hp:run><hp:t>그림 3. 조직도</hp:t></hp:run></hp:p></hp:subList></hp:caption>"
                "</hp:pic></hp:run></hp:p>"
                '<hp:p><hp:run><hp:equation><hp:caption side="TOP"><hp:subList><hp:p><hp:run><hp:t>(식 1) 수익률</hp:t>'
                "</hp:run></hp:p></hp:subList></hp:caption><hp:script>a over b</hp:script></hp:equation></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "그림 3. 조직도\n\n(식 1) 수익률 $\\frac{a}{b}$"

    def test_switch_branch_without_text_leaves_nothing_behind(self):
        """hp:case is read first and hp:default only when the case gives no text. A case with only an empty table counted as
        output, so the fallback text was lost; a case with only a comment let the fallback register the same comment again."""
        memo = ('<hp:ctrl><hp:fieldBegin id="5" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>check</hp:t></hp:run></hp:p>'
                "</hp:subList></hp:fieldBegin></hp:ctrl>")
        body = ("<hp:p><hp:run><hp:t>Before </hp:t><hp:switch><hp:case>" + memo + "<hp:tbl></hp:tbl></hp:case>"
                "<hp:default>" + memo + '<hp:t>fallback</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="5"/></hp:ctrl></hp:default>'
                "</hp:switch></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == 'Before\n\nfallback\n\n> Comment(on "fallback"): check'
        only_memo = convert(builder.hwpx_bytes(body.replace("<hp:tbl></hp:tbl>", "")), "a.hwpx")
        assert only_memo.text == 'Before fallback\n\n> Comment(on "fallback"): check'

    def test_text_art_is_kept(self):
        """The text of text art (hp:textart) is in the text attribute, not the element text, so it was dropped. ␍␊ is a line break."""
        body = '<hp:p><hp:run><hp:textart text="hwpx 라이브러리 테스트␍␊neolord"/></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "hwpx 라이브러리 테스트\nneolord"

    def test_paragraph_numbers_and_bullets_are_kept(self):
        """Paragraph numbers and bullets are a paragraph shape (hh:heading), not characters, so "1." "가." "•" were dropped. The
        lower-level '가.' takes the `list_item_prefix` form ('  - 가. ') — formerly the indentation was removed and blank lines
        appeared before and after it (test_hwp_heads_and_shapes.py::TestLowerLevelNumbers)."""
        heads = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">'
                 '<hh:paraHead start="1" level="1" numFormat="DIGIT">^1.</hh:paraHead>'
                 '<hh:paraHead start="1" level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead>'
                 '</hh:numbering></hh:numberings>'
                 '<hh:bullets itemCnt="1"><hh:bullet id="1" char="&#xF0A7;"><hh:paraHead level="0"/></hh:bullet></hh:bullets>'
                 '<hh:paraProperties itemCnt="5">'
                 '<hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                 '<hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr>'
                 '<hh:paraPr id="2"><hh:heading type="NUMBER" idRef="1" level="1"/></hh:paraPr>'
                 '<hh:paraPr id="3"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>'
                 '<hh:paraPr id="4"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
                 "</hh:paraProperties>")
        body = ('<hp:p paraPrIDRef="1"><hp:run><hp:secPr outlineShapeIDRef="1"/><hp:t>목적</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="1"><hp:run><hp:t>정의</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="2"><hp:run><hp:t>세부</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="1"><hp:run><hp:t>범위</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="3"><hp:run><hp:t>항목</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="4" styleIDRef="2"><hp:run><hp:t>추진 계획</hp:t></hp:run></hp:p>'
                '<hp:p paraPrIDRef="0"><hp:run><hp:t>본문</hp:t></hp:run></hp:p>')
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", heads + "</hh:refList>")})

        result = convert(data, "a.hwpx")

        assert result.text == "1. 목적\n2. 정의\n  - 가. 세부\n3. 범위\n- 항목\n\n# 1. 추진 계획\n\n본문"


_CHART_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r>'
    "<a:t>판매</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:barChart><c:ser><c:idx val=\"0\"/>"
    '<c:order val="0"/><c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>판매량</c:v></c:pt></c:strCache></c:strRef></c:tx>'
    '<c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>1월</c:v></c:pt><c:pt idx="1"><c:v>2월</c:v></c:pt></c:strCache>'
    '</c:strRef></c:cat><c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>5</c:v></c:pt><c:pt idx="1"><c:v>7</c:v></c:pt>'
    "</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"
)


class TestHwpRecords:
    def test_parse_records_builds_tree_by_level_and_reads_extended_size(self):
        big = b"x" * 5000
        stream = (builder.hwp_record(TAG_PARA_HEADER, 0, b"")
                  + builder.hwp_record(TAG_PARA_TEXT, 1, big)
                  + builder.hwp_record(TAG_PARA_HEADER, 0, b""))

        roots = parse_records(stream)

        assert [record.tag for record in roots] == [TAG_PARA_HEADER, TAG_PARA_HEADER]
        assert roots[0].children[0].payload == big

    def test_decode_para_text_handles_char_inline_and_extended_controls(self):
        tab = struct.pack("<H", 9) + b"\x00" * 12 + struct.pack("<H", 9)
        payload = ("가".encode("utf-16-le") + tab + "나".encode("utf-16-le")
                   + struct.pack("<H", 10) + builder.hwp_text((11, "tbl "))[:-2] + struct.pack("<H", 13))

        parts = decode_para_text(payload)

        assert parts == ["가\t나\n", 11]


def _fake_hwp(monkeypatch, *, flags=0, sections=None, preview=None, extra=None, docinfo_extra=b""):
    docinfo = b"".join(builder.hwp_record(TAG_STYLE, 0, builder.hwp_style(name))
                       for name in ("바탕글", "본문", "개요 1")) + docinfo_extra
    compress = (lambda data: zlib.compress(data)[2:-4]) if flags & 1 else (lambda data: data)
    streams = {"FileHeader": builder.hwp_file_header(flags), "DocInfo": compress(docinfo)}
    for index, section in enumerate(sections or []):
        streams[f"BodyText/Section{index}"] = compress(section)
    if preview is not None:
        streams["PrvText"] = preview.encode("utf-16-le")
    streams.update(extra or {})

    class FakeOle:
        def __init__(self, _source):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def listdir(self, streams=True, storages=False):
            return [name.split("/") for name in streams_map] + ([["BodyText"]] if storages else [])

        def exists(self, name):
            return name in streams_map

        def openstream(self, name):
            return BytesIO(streams_map[name])

    streams_map = streams
    monkeypatch.setattr(olefile, "OleFileIO", FakeOle)
    return _OLE_MAGIC + b"\x00" * 504


def _sample_section():
    record = builder.hwp_record
    return b"".join([
        record(TAG_PARA_HEADER, 0, builder.hwp_para_header(style_id=2)),
        record(TAG_PARA_TEXT, 1, builder.hwp_text("1. 개요")),
        record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 1, builder.hwp_text("본문", (17, "fn  "), "입니다")),
        record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("fn  ")),
        record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
        record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 3, builder.hwp_text("각주 내용")),
        record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
        record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
        record(TAG_TABLE, 2, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10),
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(0, 0, row_span=2)),
        record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 3, builder.hwp_text("분류")),
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(0, 1)),
        record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 3, builder.hwp_text("가")),
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(1, 1)),
        record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 3, builder.hwp_text("나")),
    ])


class TestHwp:
    @pytest.mark.parametrize("flags", [0, 1], ids=["uncompressed", "compressed"])
    def test_converts_heading_footnote_and_merged_table(self, monkeypatch, flags):
        data = _fake_hwp(monkeypatch, flags=flags, sections=[_sample_section()])

        result = convert(data, "문서.hwp")

        assert result.format is DocumentFormat.HWP
        assert result.text == (
            "# 1. 개요\n\n본문[^1]입니다\n\n| 분류 | 가 |\n| --- | --- |\n| 분류 | 나 |\n\n[^1]: 각주 내용"
        )

    def test_compressed_stream_is_bounded_by_uncompressed_limit(self, monkeypatch):
        """ZIP-based formats check the total uncompressed size first, but HWP streams were inflated without a cap (284KB → 300MB).
        The same limit applies."""
        record = builder.hwp_record
        bomb = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, b"\x00" * 3_000_000)
        data = _fake_hwp(monkeypatch, flags=1, sections=[bomb])

        with pytest.raises(DocumentTooLargeError):
            convert(data, "bomb.hwp", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_password_document_raises(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=0b10, sections=[_sample_section()])

        with pytest.raises(EncryptedDocumentError):
            convert(data, "암호.hwp")

    def test_distribution_document_falls_back_to_preview_with_warning(self, monkeypatch):
        """The body encryption of a distribution document is a copy-protection device, so it is not decrypted (deliberate)."""
        data = _fake_hwp(monkeypatch, flags=0b100, preview="미리보기 본문")

        result = convert(data, "배포.hwp")

        assert result.text == "미리보기 본문"
        assert any("distribution (copy-protected) document" in warning for warning in result.warnings)

    def test_distribution_document_without_preview_raises(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=0b100)

        with pytest.raises(EncryptedDocumentError):
            convert(data, "배포.hwp")


class TestProtectedDocuments:
    """FileHeader attribute bits 4 (DRM), 8 (public-certificate encryption) and 10 (public-certificate DRM) mean the body
    streams are encrypted. Read as usual, such a body came out as an empty result or as a 'corrupted' document."""

    @pytest.mark.parametrize("flag", [0x10, 0x100, 0x400])
    def test_drm_and_certificate_encrypted_documents_raise(self, monkeypatch, flag):
        data = _fake_hwp(monkeypatch, flags=1 | flag, extra={"BodyText/Section0": bytes(range(256)) * 4})

        with pytest.raises(EncryptedDocumentError, match="DRM or certificate encryption"):
            convert(data, "보안.hwp")

    def test_other_attribute_bits_do_not_stop_reading(self, monkeypatch):
        """Script (3), history (6), signature (7), CCL (11), change tracking (14) and the like say nothing about the body."""
        data = _fake_hwp(monkeypatch, flags=1 | 0x8 | 0x40 | 0x80 | 0x800 | 0x4000, sections=[_sample_section()])

        assert convert(data, "문서.hwp").text.startswith("# 1. 개요")


def _summary_stream(properties: list[tuple[int, int, bytes]]) -> bytes:
    """Property set stream (MS-OLEPS): 28-byte header + offset of the first section → section (size, count, (ID, offset) list,
    values)."""
    header = struct.pack("<HHI16sI", 0xFFFE, 0, 0, b"\x00" * 16, 1) + b"\x00" * 16 + struct.pack("<I", 48)
    table_size = 8 + 8 * len(properties)
    entries, values, offset = b"", b"", table_size
    for property_id, value_type, payload in properties:
        entries += struct.pack("<II", property_id, offset)
        value = struct.pack("<I", value_type) + payload
        values += value
        offset += len(value)
    return header + struct.pack("<II", table_size + len(values), len(properties)) + entries + values


def _wide(text: str) -> bytes:
    encoded = (text + "\x00").encode("utf-16-le")
    return struct.pack("<I", len(encoded) // 2) + encoded


class TestHwpSummaryInformation:
    def test_reads_title_and_author(self, monkeypatch):
        stream = _summary_stream([(2, 0x1F, _wide("사업 계획")), (4, 0x1F, _wide("홍길동"))])
        data = _fake_hwp(monkeypatch, sections=[_sample_section()], extra={"\x05HwpSummaryInformation": stream})

        result = convert(data, "a.hwp")

        assert (result.metadata["title"], result.metadata["author"]) == ("사업 계획", "홍길동")

    def test_property_with_absurd_vector_count_does_not_hang(self, monkeypatch):
        """olefile's property parsing trusts the vector count as is, so a 21KB file with 8 bytes changed to FF looped 4.3 billion
        times and hung. The summary information reads only the title and author strings directly and discards out-of-range values."""
        absurd = struct.pack("<I", 0xFFFFFFFF) + b"\x00" * 8
        stream = _summary_stream([(2, 0x1000 | 0x1F, absurd), (4, 0x1F, struct.pack("<I", 0xFFFFFFFF))])
        data = _fake_hwp(monkeypatch, sections=[_sample_section()], extra={"\x05HwpSummaryInformation": stream})

        result = convert(data, "a.hwp")

        assert "title" not in result.metadata and "author" not in result.metadata
        assert "본문" in result.text


class TestStopsAtTheCharacterLimit:
    """HWP and HWPX also stop reading the remaining paragraphs once the limit is exceeded. A 2-million-character document would be
    rejected anyway even if it were read to the end."""

    def test_hwp(self, monkeypatch):
        record = builder.hwp_record
        section = b"".join(record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                           + record(TAG_PARA_TEXT, 1, builder.hwp_text(f"{number}번째 문단입니다. " * 5))
                           for number in range(2_000))
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.char_count < 10_000, "reading everything would give more than 100,000 characters"

    def test_hwpx(self):
        body = "".join(f"<hp:p><hp:run><hp:t>{f'{number}번째 문단입니다. ' * 5}</hp:t></hp:run></hp:p>"
                       for number in range(2_000))

        result = convert(builder.hwpx_bytes(body), "a.hwpx", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.char_count < 10_000, "reading everything would give more than 100,000 characters"


def _para_header(shape_id: int, style_id: int = 0) -> bytes:
    """PARA_HEADER: character count (4) + control mask (4) + paragraph shape ID (2) + style ID (1) …"""
    return struct.pack("<IIHB", 0, 0, shape_id, style_id) + b"\x00" * 11


def _para_shape(head_type: int = 0, level: int = 0, numbering_id: int = 0) -> bytes:
    """PARA_SHAPE: attribute 1 (head type in bits 23-24, level in bits 25-27) + 6 INT32 margins/spacings + tab ID (2) +
    numbering/bullet ID (2) …"""
    return struct.pack("<I6iHH", (head_type << 23) | (level << 25), 0, 0, 0, 0, 0, 0, 0, numbering_id) + b"\x00" * 22


def _numbering(*levels: tuple[str, int]) -> bytes:
    """NUMBERING: for each of the 7 levels, paragraph head info (12; the number shape is attribute bits 5-8) + format string →
    start number (2) + per-level start (4×7)."""
    payload = b""
    for template, shape in [*levels, *[("^1.", 0)] * (7 - len(levels))]:
        payload += struct.pack("<IhhIH", shape << 5, 0, 0, 0, len(template)) + template.encode("utf-16-le")
    return payload + struct.pack("<H7I", 1, *[1] * 7)


def _bullet(char: str) -> bytes:
    return struct.pack("<IhhI", 0, 0, 0, 0) + char.encode("utf-16-le") + b"\x00" * 11


def _section_definition(outline_numbering_id: int) -> bytes:
    """Section definition (secd) CTRL_HEADER: control ID (4) + attributes (4) + column gap and line grids (2×3) + default tab (4)
    + outline numbering paragraph shape ID (2)."""
    return builder.hwp_ctrl("secd")[:4] + struct.pack("<IhHHIH", 0, 0, 0, 0, 8000, outline_numbering_id) + b"\x00" * 16


class TestParagraphNumbering:
    """Hangul paragraph numbers and bullets are a paragraph shape (PARA_SHAPE → NUMBERING/BULLET), not characters. They were not
    read, so "1." "가." were dropped. Lower-level numbers take the `list_item_prefix` form ('  - 가. ',
    test_hwp_heads_and_shapes.py::TestLowerLevelNumbers)."""

    def test_numbers_bullets_and_outline_numbers(self, monkeypatch):
        record = builder.hwp_record
        docinfo = (record(TAG_NUMBERING, 0, _numbering(("^1.", 0), ("^2.", 8)))
                   + record(TAG_BULLET, 0, _bullet("\uf0a7"))
                   + b"".join(record(TAG_PARA_SHAPE, 0, shape) for shape in (
                       _para_shape(), _para_shape(2, 0, 1), _para_shape(2, 1, 1), _para_shape(3, 0, 1), _para_shape(1, 0))))
        paragraphs = [(1, 0, "목적"), (1, 0, "정의"), (2, 0, "세부"), (1, 0, "범위"), (3, 0, "항목"), (4, 2, "추진 계획"),
                      (0, 0, "본문")]
        section = (record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((2, "secd")))
                   + record(TAG_CTRL_HEADER, 1, _section_definition(1))
                   + b"".join(record(TAG_PARA_HEADER, 0, _para_header(shape, style))
                              + record(TAG_PARA_TEXT, 1, builder.hwp_text(text)) for shape, style, text in paragraphs))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "1. 목적\n2. 정의\n  - 가. 세부\n3. 범위\n- 항목\n\n# 1. 추진 계획\n\n본문"

    def test_broken_numbering_records_fall_back_to_plain_paragraphs(self, monkeypatch):
        record = builder.hwp_record
        docinfo = record(TAG_NUMBERING, 0, b"\x01\x02") + record(TAG_PARA_SHAPE, 0, _para_shape(2, 0, 7))
        section = record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text("본문"))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "본문"


class TestHwpObjects:
    def test_table_caption_is_kept_before_the_table(self, monkeypatch):
        """The caption paragraph comes as a sibling after the empty LIST_HEADER that precedes TABLE. Only cell paragraphs were
        collected, so the caption was dropped (HWPX puts the caption before the table)."""
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
            record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("표 1. 예산")),
            record(TAG_TABLE, 2, struct.pack("<IHH", 0, 1, 2) + b"\x00" * 10),
            record(TAG_LIST_HEADER, 2, builder.hwp_cell(0, 0)),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("항목")),
            record(TAG_LIST_HEADER, 2, builder.hwp_cell(0, 1)),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("금액")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "표 1. 예산\n\n| 항목 | 금액 |\n| --- | --- |"

    def test_picture_inside_text_box_is_registered_once_where_it_is(self, monkeypatch):
        """The outer drawing object also scanned the paragraphs inside the text box and registered the picture twice, and the
        result put the picture at the outer paragraph's position."""
        record = builder.hwp_record
        picture = b"\x00" * 71 + struct.pack("<H", 1) + b"\x00" * 18
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("본문", (11, "gso "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")),
            record(TAG_LIST_HEADER, 2, b"\x00" * 8),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("로고:", (11, "gso "))),
            record(TAG_CTRL_HEADER, 3, builder.hwp_ctrl("gso ")),
            record(TAG_SHAPE_PICTURE, 4, picture),
        ])
        bin_data = record(TAG_BIN_DATA, 0, struct.pack("<HHH", 0x1, 1, 3) + "png".encode("utf-16-le"))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=bin_data,
                         extra={"BinData/BIN0001.png": builder.png_bytes()})

        result = convert(data, "a.hwp")

        assert result.text == "본문\n\n로고:\n\n[Image]"
        assert len(result.images) == 1


class TestDutmal:
    """With dutmal (a small annotation above or below characters) even the main text is inside the control, not in the paragraph
    text, so the main text was dropped along with it."""

    def test_hwp_main_text_and_its_annotation_are_kept(self, monkeypatch):
        record = builder.hwp_record
        control = "tdut"[::-1].encode("latin-1") + b"".join(
            struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in ("가나다라", "이것은 덧말이다.")) + b"\x00" * 10
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("앞", (23, "tdut"), "뒤")),
            record(TAG_CTRL_HEADER, 1, control),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "앞가나다라(이것은 덧말이다.)뒤"

    def test_hwpx_main_text_and_its_annotation_are_kept(self):
        body = ('<hp:p><hp:run><hp:t>앞</hp:t><hp:dutmal posType="TOP"><hp:mainText>가나다라</hp:mainText>'
                "<hp:subText>이것은 덧말이다.</hp:subText></hp:dutmal><hp:t>뒤</hp:t></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "앞가나다라(이것은 덧말이다.)뒤"


class TestComposedCharacters:
    """Composed characters (a number in a circle, etc.) have their characters inside a control or attribute, not in the paragraph
    text, so they were dropped entirely. The overlaid characters are written in order."""

    def test_hwp_composed_characters_are_kept(self, monkeypatch):
        """The `tcps` control has length + characters after the control ID, then the border type, character size and character
        shape IDs (hwplib sample '글자겹침.hwp')."""
        record = builder.hwp_record
        control = "tcps"[::-1].encode("latin-1") + struct.pack("<H", 2) + "40".encode("utf-16-le") + b"\x00\xfd\x00\x0a" \
            + b"\xff" * 40
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("제", (23, "tcps"), "조")),
            record(TAG_CTRL_HEADER, 1, control),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "제40조"

    def test_hwpx_composed_characters_are_kept(self):
        body = ('<hp:p><hp:run><hp:t>제</hp:t><hp:compose circleType="SHAPE_CIRCLE" charSz="-3" composeType="SPREAD" '
                'charPrCnt="10" composeText="12"><hp:charPr prIDRef="7"/><hp:charPr prIDRef="4294967295"/></hp:compose>'
                "<hp:t>조</hp:t></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "제12조"


class TestHwpxFormObjects:
    """HWPX form objects (check box, radio button, combo box, edit box) keep their values in element attributes and children, so
    they were dropped entirely. As with DOCX form fields and HTML forms, a check box is ☒/☐ + label, and combo and edit boxes are the
    value that was shown. Command buttons are left out like HTML `<button>`."""

    def test_check_boxes_radio_buttons_and_values_are_kept(self):
        def form(name, attributes, inner=""):
            return f'<hp:{name} {attributes} name="x"><hp:formCharPr charPrIDRef="0"/>{inner}<hp:sz width="1"/></hp:{name}>'

        body = ("<hp:p><hp:run><hp:t>동의:</hp:t>" + form("checkBtn", 'caption="개인정보 수집" value="CHECKED"')
                + form("checkBtn", 'caption="마케팅 수신" value="UNCHECKED"') + "</hp:run></hp:p>"
                + "<hp:p><hp:run><hp:t>성별:</hp:t>" + form("radioBtn", 'caption="남" value="UNCHECKED"')
                + form("radioBtn", 'caption="여" value="CHECKED"') + form("btn", 'caption="제출" value="UNCHECKED"')
                + "</hp:run></hp:p>"
                + "<hp:p><hp:run><hp:t>부서:</hp:t>" + form("comboBox", 'selectedValue="인사팀"',
                                                          '<hp:listItem displayText="인사팀" value="인사팀"/>')
                + "<hp:t> 사번:</hp:t>" + form("edit", 'passwordChar="X"', "<hp:text>1234</hp:text>")
                + form("comboBox", 'selectedValue=""', '<hp:listItem displayText="" value=""/>') + "</hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "동의: ☒ 개인정보 수집 ☐ 마케팅 수신\n\n성별: ☐ 남 ☒ 여\n\n부서: 인사팀 사번: 1234"


class TestHwpxTrackChanges:
    """Text deleted under change tracking (hp:deleteBegin ~ hp:deleteEnd) stayed as plain text, so '월 100,000원80,000원' read as
    two values. Only the spans whose pair is inside the same hp:t are left out (same direction as DOCX w:del), and inserted text is
    kept. The structure is from the hwpxlib sample 'ChangeTrack.hwpx'."""

    def test_deleted_text_is_left_out_and_inserted_text_is_kept(self):
        body = ('<hp:p><hp:run><hp:t>월 <hp:deleteBegin Id="2" TcId="2"/>100,000원<hp:deleteEnd Id="2" TcId="2" paraend="0"/>'
                '<hp:insertBegin Id="1" TcId="1"/>80,000원<hp:insertEnd Id="1" TcId="1" paraend="0"/>이다.</hp:t></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "월 80,000원이다."

    def test_deletion_without_its_end_in_the_same_text_is_kept(self):
        """If the pair (the deleteEnd with the same Id) is not inside the same hp:t, there is no telling how far the deletion goes,
        so the text is kept as is — no text is lost."""
        body = ('<hp:p><hp:run><hp:t>가<hp:deleteBegin Id="3" TcId="3"/>나<hp:tab/>다</hp:t></hp:run>'
                '<hp:run><hp:t>라<hp:deleteEnd Id="3" TcId="3" paraend="0"/>마</hp:t></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "가나 다라마"


class TestSuperscript:
    """Superscripts and subscripts in Hangul documents are a character shape attribute, so they were joined as plain text and 10⁶
    read as 106."""

    def test_hwpx_runs_with_superscript_or_subscript_character_shape_are_marked(self):
        def with_shapes(header):
            return header.replace("<hh:styles>", (
                '<hh:charProperties><hh:charPr id="0" height="1000"/><hh:charPr id="7" height="1000"><hh:supscript/>'
                '</hh:charPr><hh:charPr id="8" height="1000"><hh:subscript/></hh:charPr></hh:charProperties><hh:styles>'))

        body = ('<hp:p><hp:run charPrIDRef="0"><hp:t>면적 10</hp:t></hp:run><hp:run charPrIDRef="7"><hp:t>6</hp:t></hp:run>'
                '<hp:run charPrIDRef="0"><hp:t> m, H</hp:t></hp:run><hp:run charPrIDRef="8"><hp:t>2</hp:t></hp:run>'
                '<hp:run charPrIDRef="0"><hp:t>O, 13</hp:t></hp:run><hp:run charPrIDRef="7"><hp:t>th</hp:t></hp:run></hp:p>')
        data = builder.zip_with(builder.hwpx_bytes(body), edit={"Contents/header.xml": with_shapes})

        result = convert(data, "a.hwpx")

        assert result.text == "면적 10^6 m, H_2O, 13th"

    def test_hwp_characters_with_superscript_or_subscript_shape_are_marked(self, monkeypatch):
        """Bit 15 of the character shape (DocInfo CHAR_SHAPE) attribute is superscript and bit 16 is subscript, and the paragraph's
        PARA_CHAR_SHAPE records 'which character shape from which character on'."""
        def char_shape(attribute):
            return builder.hwp_record(TAG_CHAR_SHAPE, 0, b"\x00" * 42 + struct.pack("<iI", 1000, attribute) + b"\x00" * 18)

        record = builder.hwp_record
        text = "면적 106 m, H2O"
        positions = [(0, 0), (5, 1), (6, 0), (11, 2), (12, 0)]  # '6' is superscript, '2' is subscript
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text(text)),
            record(TAG_PARA_CHAR_SHAPE, 1, b"".join(struct.pack("<II", *pair) for pair in positions)),
        ])
        docinfo = char_shape(0) + char_shape(1 << 15) + char_shape(1 << 16)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "면적 10^6 m, H_2O"


def _hwpx_with_strikeouts(body: str) -> bytes:
    """Character shapes 0, 3 and 4 have no strikeout (the `3D` of 4 is the value found on every character shape of a real press
    release that has no strikeout); 1 and 2 have a strikeout."""
    shapes = "".join(f'<hh:charPr id="{index}"><hh:strikeout shape="{shape}" color="#000000"/></hh:charPr>'
                     for index, shape in enumerate(("NONE", "SOLID", "DOT", "", "3D")))
    return builder.zip_with(builder.hwpx_bytes(body), edit={
        "Contents/header.xml": lambda xml: xml.replace("<hh:styles>", f"<hh:charProperties>{shapes}</hh:charProperties><hh:styles>")})


def _hwpx_run(shape_id: int, text: str) -> str:
    return f'<hp:run charPrIDRef="{shape_id}"><hp:t>{text}</hp:t></hp:run>'


class TestStrikethrough:
    """A strikeout in Hangul documents is a character shape attribute, so the deleted value stayed as plain text and
    `월 100,000원 80,000원` read as two valid values. It is emitted as `~~text~~`, like HTML."""

    def test_hwpx_runs_with_a_strikeout_character_shape_are_marked(self):
        body = ("<hp:p>" + _hwpx_run(0, "수수료는 월 ") + _hwpx_run(1, "100,000원") + _hwpx_run(0, " 80,000원으로 한다.")
                + "</hp:p>")

        result = convert(_hwpx_with_strikeouts(body), "a.hwpx")

        assert result.text == "수수료는 월 ~~100,000원~~ 80,000원으로 한다."

    def test_hwpx_adjacent_struck_runs_are_marked_once_and_blank_runs_are_not_marked(self):
        """Consecutive struck runs (even with only whitespace between them) are wrapped once, and the marks sit against the
        characters. A struck run of only whitespace is not wrapped."""
        body = ("<hp:p>" + _hwpx_run(0, "담당:") + _hwpx_run(1, " 김") + _hwpx_run(2, "철수 ") + _hwpx_run(0, " ")
                + _hwpx_run(1, "과장 ") + _hwpx_run(0, "이영희") + _hwpx_run(1, "  ") + _hwpx_run(0, "부장")
                + "</hp:p>")

        result = convert(_hwpx_with_strikeouts(body), "a.hwpx")

        assert result.text == "담당: ~~김철수 과장~~ 이영희 부장"

    def test_hwpx_strikeout_shapes_that_are_not_a_line_are_not_marked(self):
        """`NONE`, no value, and `3D`, which is not among the line types of the specification, are not a strikeout."""
        body = "<hp:p>" + _hwpx_run(0, "가") + _hwpx_run(3, "나") + _hwpx_run(4, "다") + "</hp:p>"

        result = convert(_hwpx_with_strikeouts(body), "a.hwpx")

        assert result.text == "가나다"

    def test_hwpx_struck_superscript_keeps_both_marks(self):
        def with_shapes(header):
            return header.replace("<hh:styles>", (
                '<hh:charProperties><hh:charPr id="0"/><hh:charPr id="1"><hh:strikeout shape="SOLID"/></hh:charPr>'
                '<hh:charPr id="2"><hh:supscript/><hh:strikeout shape="SOLID"/></hh:charPr></hh:charProperties><hh:styles>'))

        body = "<hp:p>" + _hwpx_run(0, "면적 ") + _hwpx_run(1, "10") + _hwpx_run(2, "6") + _hwpx_run(0, " 삭제") + "</hp:p>"
        data = builder.zip_with(builder.hwpx_bytes(body), edit={"Contents/header.xml": with_shapes})

        result = convert(data, "a.hwpx")

        assert result.text == "면적 ~~10^6~~ 삭제"

    def test_hwpx_struck_superscript_split_over_runs_is_one_superscript_inside_the_strike(self):
        """Strikeouts are resolved before superscripts/subscripts (same order as DOCX and PPTX, `_inline.resolve_strikes`).
        Resolving them the other way round broke the struck superscript '-'·'3' split over two runs into `~~10^-^3~~`."""
        def with_shapes(header):
            return header.replace("<hh:styles>", (
                '<hh:charProperties><hh:charPr id="0"/><hh:charPr id="1"><hh:supscript/></hh:charPr>'
                '<hh:charPr id="2"><hh:strikeout shape="SOLID"/></hh:charPr>'
                '<hh:charPr id="3"><hh:supscript/><hh:strikeout shape="SOLID"/></hh:charPr></hh:charProperties><hh:styles>'))

        body = ("<hp:p>" + _hwpx_run(0, "허용치 ") + _hwpx_run(2, "10") + _hwpx_run(3, "-") + _hwpx_run(3, "3")
                + _hwpx_run(0, " → 10") + _hwpx_run(1, "-") + _hwpx_run(1, "6") + "</hp:p>")
        data = builder.zip_with(builder.hwpx_bytes(body), edit={"Contents/header.xml": with_shapes})

        result = convert(data, "a.hwpx")

        assert result.text == "허용치 ~~10^{-3}~~ → 10^{-6}"

    def test_hwp_struck_superscript_split_over_runs_is_one_superscript_inside_the_strike(self, monkeypatch):
        """Same as HWPX. The extended control (field start) between the struck superscript '-' and '3' splits the text piece in
        two."""
        def char_shape(attribute):
            return builder.hwp_record(TAG_CHAR_SHAPE, 0, b"\x00" * 42 + struct.pack("<iI", 1000, attribute) + b"\x00" * 18)

        record = builder.hwp_record
        text = builder.hwp_text("허용치 10-", (3, "%clk"), "3 → 10-6")  # an extended control takes 8 character positions
        positions = [(0, 0), (4, 1), (6, 2), (16, 0), (21, 3)]  # '10' struck, '-'·'3' struck + superscript, final '-6' superscript
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, text),
            record(TAG_PARA_CHAR_SHAPE, 1, b"".join(struct.pack("<II", *pair) for pair in positions)),
        ])
        docinfo = b"".join(char_shape(attribute) for attribute in (0, 1 << 18, 1 << 18 | 1 << 15, 1 << 15))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "허용치 ~~10^{-3}~~ → 10^{-6}"

    def test_hwp_characters_with_the_strikeout_attribute_are_marked(self, monkeypatch):
        """Bits 18-20 of the character shape (DocInfo CHAR_SHAPE) attribute say whether there is a strikeout. Having only bits
        26-29 (strikeout shape) is not a strikeout."""
        def char_shape(attribute):
            return builder.hwp_record(TAG_CHAR_SHAPE, 0, b"\x00" * 42 + struct.pack("<iI", 1000, attribute) + b"\x00" * 18)

        record = builder.hwp_record
        text = "월 100,000원 80,000원 x2"
        positions = [(0, 0), (2, 1), (10, 0), (11, 2), (18, 0), (20, 3)]  # '100,000원' is struck, the final '2' is struck + superscript
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text(text)),
            record(TAG_PARA_CHAR_SHAPE, 1, b"".join(struct.pack("<II", *pair) for pair in positions)),
        ])
        docinfo = char_shape(0) + char_shape(1 << 18) + char_shape(1 << 26) + char_shape(1 << 18 | 1 << 15)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "월 ~~100,000원~~ 80,000원 x~~^2~~"

    def test_hwp_strikeout_shapes_that_are_not_a_line_are_not_marked(self, monkeypatch):
        """Even with the strikeout bit set, a shape (bits 26-29) that is not among the HWPX line types (LineType2) — wave (11, 12)
        or 3D (13-15) — is not a strikeout. A real press release had body character shapes with bit 18 + shape 15 (3D), and the
        HWPX of the same document had `3D` (not a strikeout)."""
        def char_shape(attribute):
            return builder.hwp_record(TAG_CHAR_SHAPE, 0, b"\x00" * 42 + struct.pack("<iI", 1000, attribute) + b"\x00" * 18)

        record = builder.hwp_record
        text = "가나다라마"
        positions = [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]  # '나' 3D, '다' wave, '라' double line, '마' 3D (thick)
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text(text)),
            record(TAG_PARA_CHAR_SHAPE, 1, b"".join(struct.pack("<II", *pair) for pair in positions)),
        ])
        docinfo = char_shape(0) + b"".join(char_shape(1 << 18 | shape << 26) for shape in (15, 11, 7, 13))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "가나다~~라~~마"


class TestEmbeddedObjects:
    """OLE objects (a pasted Excel sheet or chart) and videos have no text, so they vanished without a trace. Their contents are
    not read; a placeholder and a warning are left."""

    def test_hwpx_ole_object_and_video_leave_a_mark_and_a_warning(self):
        body = ('<hp:p><hp:run><hp:t>매출 현황:</hp:t><hp:ole binaryItemIDRef="ole1"><hp:shapeComment>OLE 개체입니다.'
                '</hp:shapeComment><hp:caption side="BOTTOM"><hp:subList><hp:p><hp:run><hp:t>표 1. 매출</hp:t></hp:run></hp:p>'
                "</hp:subList></hp:caption></hp:ole></hp:run></hp:p>"
                '<hp:p><hp:run><hp:ole binaryItemIDRef="ole2"/><hp:video videotype="VT_LOCAL"/></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "매출 현황: [OLE object] 표 1. 매출\n\n[OLE object] [Video]"
        assert result.warnings == ["The contents of 2 OLE object(s) (pasted tables, charts, etc.) could not be converted.",
                                   "1 video(s) could not be converted."]

    def test_hwpx_document_without_embedded_objects_has_no_warning(self):
        result = convert(builder.hwpx_bytes(), "a.hwpx")

        assert result.warnings == []

    def test_hwp_ole_object_leaves_a_mark_and_a_warning(self, monkeypatch):
        """The SHAPE_COMPONENT_OLE record under a drawing object (gso). The mark is left even with picture extraction off."""
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("매출 현황:", (11, "gso "), "끝")),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")),
            record(TAG_SHAPE_OLE, 2, b"\x00" * 24),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp", ConvertOptions(extract_images=False))

        assert result.text == "매출 현황: [OLE object] 끝"
        assert result.warnings == ["The contents of 1 OLE object(s) (pasted tables, charts, etc.) could not be converted."]


def _auto_number(kind: int, number: int, *, shape: int = 0, prefix: str = "\x00", suffix: str = "\x00") -> bytes:
    """Auto number (atno) CTRL_HEADER: control ID (4) + attributes (4; kind in bits 0-3, number shape in bits 4-11) + number (2)
    + user symbol, prefix and suffix decoration characters (2×3)."""
    return builder.hwp_ctrl("atno")[:4] + struct.pack("<IHHHH", kind | (shape << 4), number, 0, ord(prefix), ord(suffix))


def _hwp_table(*cells: tuple[int, int, list[bytes]], rows: int, cols: int, caption: list[bytes] | None = None) -> bytes:
    """One table paragraph. `cells` are (row, column, paragraph records built at level 2); `caption` is the caption paragraph
    records that come before TABLE."""
    record = builder.hwp_record
    return b"".join([
        record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
        record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
        record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
        *([record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6), *caption] if caption else []),
        record(TAG_TABLE, 2, struct.pack("<IHH", 0, rows, cols) + b"\x00" * 10),
        *(part for row, col, paragraphs in cells for part in (record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, col)), *paragraphs)),
    ])


def _cell_paragraph(text: str, shape_id: int = 0) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, 2, _para_header(shape_id))
            + builder.hwp_record(TAG_PARA_TEXT, 3, builder.hwp_text(text)))


def _hwpx_auto_number(kind: str, number: int, *, shape: str = "DIGIT", prefix: str = "", suffix: str = "") -> str:
    return (f'<hp:ctrl><hp:autoNum num="{number}" numType="{kind}"><hp:autoNumFormat type="{shape}" userChar="" '
            f'prefixChar="{prefix}" suffixChar="{suffix}" supscript="0"/></hp:autoNum></hp:ctrl>')


class TestAutoNumber:
    """The number in a caption's "표 1" or "그림 2" is an auto-number control, not characters. It was not read, so "표 1 예산"
    became "표 예산"."""

    def test_hwp_caption_number_is_kept(self, monkeypatch):
        record = builder.hwp_record
        caption = [record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
                   record(TAG_PARA_TEXT, 3, builder.hwp_text("표 ", (18, "atno"), " 예산")),
                   record(TAG_CTRL_HEADER, 3, _auto_number(4, 3))]
        section = _hwp_table((0, 0, [_cell_paragraph("항목")]), (0, 1, [_cell_paragraph("금액")]), rows=1, cols=2,
                             caption=caption)
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "표 3 예산\n\n| 항목 | 금액 |\n| --- | --- |"

    def test_hwp_number_follows_its_shape_and_decorations(self, monkeypatch):
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("그림 ", (18, "atno"), " 조직도, 식 ", (18, "atno"))),
            record(TAG_CTRL_HEADER, 1, _auto_number(3, 2, shape=2)),
            record(TAG_CTRL_HEADER, 1, _auto_number(5, 4, prefix="(", suffix=")")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "그림 II 조직도, 식 (4)"

    def test_hwp_page_and_note_numbers_are_not_inserted(self, monkeypatch):
        """A page number is not document content, and footnote/endnote numbers are replaced by `[^n]`. Inserting them would
        duplicate the number, as in "[^1]: 1) 각주"."""
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("- ", (18, "atno"), " -")),
            record(TAG_CTRL_HEADER, 1, _auto_number(0, 7)),
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("본문", (17, "fn  "), (17, "en  "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("fn  ")),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text((18, "atno"), " 각주 내용")),
            record(TAG_CTRL_HEADER, 3, _auto_number(1, 1, suffix=")")),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("en  ")),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text((18, "atno"), " 미주 내용")),
            record(TAG_CTRL_HEADER, 3, _auto_number(2, 1, suffix=")")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "- -\n\n본문[^1][^2]\n\n[^1]: 각주 내용\n[^2]: 미주 내용"

    def test_hwpx_caption_number_is_kept(self):
        body = ('<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:caption side="TOP"><hp:subList><hp:p><hp:run><hp:t>표 </hp:t>'
                + _hwpx_auto_number("TABLE", 3) + "<hp:t> 요구사항 목록</hp:t></hp:run></hp:p></hp:subList></hp:caption>"
                "<hp:tr>" + builder._hwpx_cell(0, 0, "항목") + builder._hwpx_cell(0, 1, "금액") + "</hp:tr></hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "표 3 요구사항 목록\n\n| 항목 | 금액 |\n| --- | --- |"

    def test_hwpx_number_follows_its_shape_and_decorations(self):
        body = ("<hp:p><hp:run><hp:t>그림 </hp:t>" + _hwpx_auto_number("PICTURE", 2, shape="ROMAN_CAPITAL")
                + "<hp:t> 조직도, 식 </hp:t>" + _hwpx_auto_number("EQUATION", 4, prefix="(", suffix=")") + "</hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "그림 II 조직도, 식 (4)"

    def test_hwpx_page_and_note_numbers_are_not_inserted(self):
        body = ("<hp:p><hp:run><hp:t>- </hp:t>" + _hwpx_auto_number("PAGE", 7) + "<hp:t>/</hp:t>"
                + _hwpx_auto_number("TOTAL_PAGE", 24) + "<hp:t> -</hp:t></hp:run></hp:p>"
                "<hp:p><hp:run><hp:t>본문</hp:t><hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run>"
                + _hwpx_auto_number("FOOTNOTE", 1, suffix=")") + "<hp:t> 각주 내용</hp:t></hp:run></hp:p></hp:subList>"
                "</hp:footNote></hp:ctrl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "- / -\n\n본문[^1]\n\n[^1]: 각주 내용"


class TestListLevelsInsideTableCells:
    """Leading spaces of a line inside a table cell are removed when the table is built. The list levels that relied on
    indentation vanished, so sub-items ended up level with their parent items. Inside a cell the level is conveyed by `└`/`└─` marks,
    one per level."""

    def test_hwp(self, monkeypatch):
        record = builder.hwp_record
        docinfo = (record(TAG_NUMBERING, 0, _numbering(("^1.", 0), ("^2.", 8)))
                   + record(TAG_BULLET, 0, _bullet(""))
                   + b"".join(record(TAG_PARA_SHAPE, 0, shape) for shape in (
                       _para_shape(), _para_shape(3, 0, 1), _para_shape(3, 1, 1), _para_shape(3, 2, 1), _para_shape(2, 1, 1))))
        items = [_cell_paragraph("추천 유형", 1), _cell_paragraph("지식 기반", 2), _cell_paragraph("관심 분야", 3),
                 _cell_paragraph("세부", 4)]
        section = _hwp_table((0, 0, [_cell_paragraph("요건")]), (0, 1, items), rows=1, cols=2)
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp")

        assert result.text == "| 요건 | - 추천 유형<br>└ - 지식 기반<br>└─ - 관심 분야<br>└ 가. 세부 |\n| --- | --- |"

    def test_hwpx(self):
        heads = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">'
                 '<hh:paraHead start="1" level="1" numFormat="DIGIT">^1.</hh:paraHead>'
                 '<hh:paraHead start="1" level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead>'
                 '</hh:numbering></hh:numberings>'
                 '<hh:bullets itemCnt="1"><hh:bullet id="1" char="&#xF0A7;"><hh:paraHead level="0"/></hh:bullet></hh:bullets>'
                 '<hh:paraProperties itemCnt="4">'
                 '<hh:paraPr id="1"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>'
                 '<hh:paraPr id="2"><hh:heading type="BULLET" idRef="1" level="1"/></hh:paraPr>'
                 '<hh:paraPr id="3"><hh:heading type="BULLET" idRef="1" level="2"/></hh:paraPr>'
                 '<hh:paraPr id="4"><hh:heading type="NUMBER" idRef="1" level="1"/></hh:paraPr>'
                 "</hh:paraProperties>")
        items = "".join(f'<hp:p paraPrIDRef="{shape}"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>'
                        for shape, text in ((1, "추천 유형"), (2, "지식 기반"), (3, "관심 분야"), (4, "세부")))
        body = ('<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "요건")
                + f"<hp:tc><hp:subList>{items}</hp:subList>"
                '<hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>')
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", heads + "</hh:refList>")})

        result = convert(data, "a.hwpx")

        assert result.text == "| 요건 | - 추천 유형<br>└ - 지식 기반<br>└─ - 관심 분야<br>└ 가. 세부 |\n| --- | --- |"


class TestPreviewFallback:
    """If no text comes out of the body, the preview text saved by Hancom Office is used instead. But if the preview is nothing but table
    cell marks (`<`, `>`), the document has no text (an empty table, a document of shapes only). Emitting that as the body gave `<>`
    noise plus a "The body could not be parsed…" warning."""

    def test_hwp_preview_with_text_replaces_an_empty_body(self, monkeypatch):
        empty = builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
        data = _fake_hwp(monkeypatch, sections=[empty], preview="<항목><금액>\r\n미리보기 본문")

        result = convert(data, "a.hwp")

        assert result.text == "<항목><금액>\n미리보기 본문"
        assert any("the preview text saved by Hancom Office was used instead" in warning for warning in result.warnings)

    def test_hwp_preview_of_empty_cells_only_is_not_used(self, monkeypatch):
        empty = builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
        data = _fake_hwp(monkeypatch, sections=[empty], preview="<>\r\n<><>\r\n")

        result = convert(data, "a.hwp")

        assert result.text == ""
        assert result.warnings == ["No text was extracted."]

    def test_hwp_distribution_document_with_preview_of_empty_cells_only_raises(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=0b100, preview="<>\r\n<>")

        with pytest.raises(EncryptedDocumentError):
            convert(data, "배포.hwp")

    def test_hwpx_preview_with_text_replaces_an_empty_body(self):
        data = builder.zip_with(builder.hwpx_bytes("<hp:p><hp:run/></hp:p>"), add={"Preview/PrvText.txt": "<항목><금액>\r\n미리보기 본문"})

        result = convert(data, "a.hwpx")

        assert result.text == "<항목><금액>\n미리보기 본문"
        assert any("the preview text saved by Hancom Office was used instead" in warning for warning in result.warnings)

    def test_hwpx_preview_of_empty_cells_only_is_not_used(self):
        data = builder.zip_with(builder.hwpx_bytes("<hp:p><hp:run/></hp:p>"), add={"Preview/PrvText.txt": "<>\r\n<><>\r\n"})

        result = convert(data, "a.hwpx")

        assert result.text == ""
        assert result.warnings == ["No text was extracted."]


class TestMemo:
    """Comments (review remarks): HWPX puts them at the end of the document as `> Comment(author, on "range"): text` lines, like
    DOCX and PPTX (comments were dropped without a warning, so review remarks could not be seen). The commented text stays in the
    body as is. Comments in binary HWP are not read because there is no evidence for their structure (known accepted gap)."""

    def test_hwpx_memo_is_written_after_the_body_with_its_target_text(self):
        body = ('<hp:p><hp:run><hp:t>운영체제: </hp:t><hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:parameters cnt="1">'
                '<hp:stringParam name="Author">검토자</hp:stringParam></hp:parameters><hp:subList><hp:p><hp:run>'
                "<hp:t>상세 버전 정보 부탁드립니다.</hp:t></hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl>"
                '<hp:t>Ubuntu 22.04</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl><hp:t> LTS</hp:t></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == '운영체제: Ubuntu 22.04 LTS\n\n> Comment(검토자, on "Ubuntu 22.04"): 상세 버전 정보 부탁드립니다.'

    def test_hwpx_memo_target_spanning_paragraphs_and_memo_without_author(self):
        body = ('<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="7" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>첫 메모</hp:t>'
                "</hp:run></hp:p><hp:p><hp:run><hp:t>둘째 줄</hp:t></hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl>"
                "<hp:t>앞 문단</hp:t></hp:run></hp:p><hp:p><hp:run><hp:t>뒤 문단</hp:t><hp:ctrl><hp:fieldEnd beginIDRef=\"7\"/>"
                "</hp:ctrl><hp:t> 끝</hp:t></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == '앞 문단\n\n뒤 문단 끝\n\n> Comment(on "앞 문단 뒤 문단"): 첫 메모 둘째 줄'

    @pytest.mark.timing
    def test_hwpx_memos_without_an_end_take_linear_time(self):
        """The range of a comment without a fieldEnd stays open to the end of the document. Every paragraph appended the
        inter-paragraph space to the open ranges without limit, and every text piece recounted the collected characters, which took
        the square of the paragraph count (1 open comment, 40,000 paragraphs: 9 s). A range collects only as much as will be shown."""
        memos = "".join(f'<hp:ctrl><hp:fieldBegin id="{index}" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>메모{index}</hp:t>'
                        "</hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl>" for index in range(10))
        body = f"<hp:p><hp:run>{memos}<hp:t>시작</hp:t></hp:run></hp:p>" + "<hp:p><hp:run><hp:t>가</hp:t></hp:run></hp:p>" * 10_000
        data = builder.hwpx_bytes(body)

        started = time.perf_counter()
        result = convert(data, "a.hwpx")
        elapsed = time.perf_counter() - started

        assert elapsed < 2.0
        assert result.text.endswith('> Comment(on "시작 가 가 가 가 가 가 가 가 가 가 가 가 가 가 가 가 가 가 가…"): 메모9')

    @pytest.mark.timing
    def test_many_hwpx_memos_over_empty_paragraphs_take_linear_time(self):
        """A comment whose range is empty gets no inter-paragraph space, so it stays open until text appears. Every paragraph
        rescanned all open comments, so one comment without a fieldEnd per empty paragraph took quadratic time (24,000 of them:
        16 s). Twice the input must take little more than twice the time."""
        def elapsed(count: int) -> tuple[float, str]:
            body = "".join(f'<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="{index}" type="MEMO"><hp:subList><hp:p><hp:run>'
                           f"<hp:t>메모{index}</hp:t></hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl></hp:run></hp:p>"
                           for index in range(count))
            data = builder.hwpx_bytes(body)
            # The faster of two runs. A single pause of the machine (about 0.2 s on the Windows runner, where the conversion itself
            # measured linear) in the one run of the larger document read as a conversion slower than linear.
            spent = []
            for _ in range(2):
                started = time.perf_counter()
                text = convert(data, "a.hwpx").text
                spent.append(time.perf_counter() - started)
            return min(spent), text

        small, _ = elapsed(4_000)
        large, text = elapsed(8_000)

        assert large < 3 * small + 0.1
        assert text.endswith("> Comment: 메모7998\n> Comment: 메모7999")

    def test_hwp_paragraphs_under_a_memo_field_control_are_left_out(self, monkeypatch):
        """HWP reads only the inner paragraphs of the controls it knows how to read (table, text box, footnote, header). Paragraphs
        under a comment field (`%%me`) are not read either (known accepted gap: there is no sample or specification evidence for
        where the comment text of binary HWP lives)."""
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("운영체제: ", (3, "%%me"), "Ubuntu 22.04", (4, "%%me"), " LTS")),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("%%me")),
            record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("상세 버전 정보 부탁드립니다.")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "운영체제: Ubuntu 22.04 LTS"


class TestEquations:
    """Hangul equations were emitted only as the raw script (`[Equation: {1} over {n}]`). They are converted to LaTeX, and the raw
    script is kept when that is not possible."""

    def test_hwp_equation_becomes_latex(self, monkeypatch):
        script = "{ 1} over { n} sum _{i`=`1``} ^{n} (B _{i} `-A _{i} `)"
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("평균 차이 ", (11, "eqed"), " 를 구한다")),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("eqed")),
            record(TAG_EQEDIT, 2, struct.pack("<IH", 0, len(script)) + script.encode("utf-16-le")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "평균 차이 $\\frac{1}{n} \\sum_{i = 1}^{n} (B_{i} -A_{i} )$ 를 구한다"

    @pytest.mark.parametrize(("direction", "expected"), [(3, "$\\frac{a}{b}$ 수식 1 넓이"), (2, "수식 1 넓이 $\\frac{a}{b}$")])
    def test_hwp_equation_caption_is_kept_on_its_side(self, monkeypatch, direction, expected):
        """The caption paragraph under the equation control ('수식 1 넓이') was dropped. As in HWPX, a top or left caption goes
        before and any other after. The caption LIST_HEADER is paragraph count (2), attributes (4) and, 2 bytes later, the caption
        attributes (4; bits 0-1 direction: 0 left, 1 right, 2 top, 3 bottom) — the structure of the public hwplib '수식.hwp'."""
        script = "a over b"
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "eqed"))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("eqed")),
            record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6 + struct.pack("<I", direction) + b"\x00" * 18),
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text("수식 ", (18, "atno"), " 넓이")),
            record(TAG_CTRL_HEADER, 3, _auto_number(5, 1)),
            record(TAG_EQEDIT, 2, struct.pack("<IH", 0, len(script)) + script.encode("utf-16-le")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == expected

    def test_hwpx_equation_that_cannot_be_converted_keeps_its_script(self):
        body = "<hp:p><hp:run><hp:t>몫: </hp:t><hp:equation><hp:script>LONGDIV {6}{422}{2532}</hp:script></hp:equation></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "몫: [Equation: LONGDIV {6}{422}{2532}]"


class TestNestedTables:
    """A table inside a table was flattened into 'cell / cell' lines, so the row and column structure vanished. The outer table
    is HTML, with the inner table inside its cell (markdown.TableGrid.embed)."""

    _EXPECTED = ("<table>\n<tr><th>구분</th><th>인력</th></tr>\n"
                 "<tr><td>하반기</td><td><table><tr><th>담당</th><th>인원</th></tr><tr><td>운영팀</td><td>3명</td></tr>"
                 "</table></td></tr>\n</table>")

    def test_hwpx_nested_table(self):
        inner = ("<hp:tbl>" + "".join(f"<hp:tr>{builder._hwpx_cell(row, 0, a)}{builder._hwpx_cell(row, 1, b)}</hp:tr>"
                                      for row, (a, b) in enumerate([("담당", "인원"), ("운영팀", "3명")])) + "</hp:tbl>")
        nested_cell = ("<hp:tc><hp:subList><hp:p><hp:run>" + inner + '</hp:run></hp:p></hp:subList>'
                       '<hp:cellAddr colAddr="1" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        body = ("<hp:p><hp:run><hp:tbl>"
                f"<hp:tr>{builder._hwpx_cell(0, 0, '구분')}{builder._hwpx_cell(0, 1, '인력')}</hp:tr>"
                f"<hp:tr>{builder._hwpx_cell(1, 0, '하반기')}{nested_cell}</hp:tr>"
                "</hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == self._EXPECTED

    def test_hwp_nested_table(self, monkeypatch):
        record = builder.hwp_record

        def cell_text(level, text):
            return [record(TAG_PARA_HEADER, level, builder.hwp_para_header()),
                    record(TAG_PARA_TEXT, level + 1, builder.hwp_text(text))]

        inner_cells = [(0, 0, "담당"), (0, 1, "인원"), (1, 0, "운영팀"), (1, 1, "3명")]
        inner = [  # table in a paragraph inside a cell (level 2): paragraph 3, control 4, table/cells 5, cell paragraphs 5
            record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 3, builder.hwp_text((11, "tbl "))),
            record(TAG_CTRL_HEADER, 3, builder.hwp_ctrl("tbl ")),
            record(TAG_TABLE, 4, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10),
            *(part for row, col, text in inner_cells
              for part in (record(TAG_LIST_HEADER, 4, builder.hwp_cell(row, col)), *cell_text(4, text))),
        ]
        section = _hwp_table((0, 0, cell_text(2, "구분")), (0, 1, cell_text(2, "인력")), (1, 0, cell_text(2, "하반기")),
                             (1, 1, inner), rows=2, cols=2)
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == self._EXPECTED

    def test_table_in_a_footnote_inside_a_cell_is_kept(self):
        """A footnote inside a cell goes to the footnote list, not the cell text. Handing a table inside it to the outer table
        leaks the placeholder into the footnote, where it is lost. A table inside a footnote is flattened to 'cell / cell' as
        before."""
        note_table = ("<hp:tbl><hp:tr>" + builder._hwpx_cell(0, 0, "주석표") + builder._hwpx_cell(0, 1, "값") + "</hp:tr></hp:tbl>")
        note = ("<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run>" + note_table
                + "</hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl>")
        cell = ("<hp:tc><hp:subList><hp:p><hp:run><hp:t>본문</hp:t>" + note + "</hp:run></hp:p></hp:subList>"
                '<hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        body = "<hp:p><hp:run><hp:tbl><hp:tr>" + builder._hwpx_cell(0, 0, "항목") + cell + "</hp:tr></hp:tbl></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert "| 항목 | 본문[^1] |" in result.text
        assert "[^1]: 주석표 / 값" in result.text

    def test_caption_of_an_inner_table_is_kept_with_it(self):
        """The caption of an inner table was discarded, so '표 2' vanished, and a picture in the caption was registered but its
        placeholder was discarded, so it only cost a description."""
        inner = ('<hp:tbl><hp:caption side="TOP"><hp:subList><hp:p><hp:run><hp:t>표 2 인력</hp:t></hp:run></hp:p>'
                 "</hp:subList></hp:caption><hp:tr>" + builder._hwpx_cell(0, 0, "담당") + builder._hwpx_cell(0, 1, "3명")
                 + "</hp:tr></hp:tbl>")
        cell = ("<hp:tc><hp:subList><hp:p><hp:run>" + inner + "</hp:run></hp:p></hp:subList>"
                '<hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        body = "<hp:p><hp:run><hp:tbl><hp:tr>" + builder._hwpx_cell(0, 0, "인력") + cell + "</hp:tr></hp:tbl></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == ("<table>\n<tr><th>인력</th><th>표 2 인력<br><table><tr><th>담당</th><th>3명</th></tr></table>"
                               "</th></tr>\n</table>")


class TestTableInsideFootnote:
    """A table inside a footnote was drawn as a Markdown table and then the footnote was joined into one line, giving
    '| 연도 | 값 | | --- | --- | | 2022 | 5 |' (for a footnote inside a cell, '연도 / 값 2022 / 5' with the row boundary lost). A
    footnote is one line, so cells are joined with ' / ' and rows with '; ' (`_notation.note_table`)."""

    @staticmethod
    def _hwpx_table(rows: list[tuple[str, str]]) -> str:
        return ("<hp:tbl>" + "".join(f"<hp:tr>{builder._hwpx_cell(row, 0, a)}{builder._hwpx_cell(row, 1, b)}</hp:tr>"
                                     for row, (a, b) in enumerate(rows)) + "</hp:tbl>")

    def _hwpx_note(self) -> str:
        return ("<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>출처 표:</hp:t></hp:run></hp:p><hp:p><hp:run>"
                + self._hwpx_table([("연도", "값"), ("2022", "5")]) + "<hp:t/></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl>")

    def test_hwpx_table_in_a_body_footnote_is_one_line_with_rows(self):
        body = f"<hp:p><hp:run><hp:t>본문</hp:t>{self._hwpx_note()}<hp:t> 끝</hp:t></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "본문[^1] 끝\n\n[^1]: 출처 표: 연도 / 값; 2022 / 5"

    def test_hwpx_table_in_a_footnote_inside_a_cell_keeps_its_rows(self):
        cell = ("<hp:tc><hp:subList><hp:p><hp:run><hp:t>배출량</hp:t>" + self._hwpx_note() + "</hp:run></hp:p></hp:subList>"
                '<hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        body = "<hp:p><hp:run><hp:tbl><hp:tr>" + builder._hwpx_cell(0, 0, "항목") + cell + "</hp:tr></hp:tbl></hp:run></hp:p>"

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert "[^1]: 출처 표: 연도 / 값; 2022 / 5" in result.text

    def test_hwp_table_in_a_body_footnote_is_one_line_with_rows(self, monkeypatch):
        record = builder.hwp_record

        def paragraph(level, *text):
            return (record(TAG_PARA_HEADER, level, builder.hwp_para_header())
                    + record(TAG_PARA_TEXT, level + 1, builder.hwp_text(*text)))

        cells = [(0, 0, "연도"), (0, 1, "값"), (1, 0, "2022"), (1, 1, "5")]
        note = b"".join([  # footnote control of the body paragraph (level 0) 1, its paragraphs 2, table control 3, table/cells 4, cell paragraphs 4
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("fn  ")), record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6),
            paragraph(2, "출처 표:"), paragraph(2, (11, "tbl ")), record(TAG_CTRL_HEADER, 3, builder.hwp_ctrl("tbl ")),
            record(TAG_TABLE, 4, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10),
            *(record(TAG_LIST_HEADER, 4, builder.hwp_cell(row, col)) + paragraph(4, text) for row, col, text in cells),
        ])
        data = _fake_hwp(monkeypatch, sections=[paragraph(0, "본문", (17, "fn  "), " 끝") + note])

        result = convert(data, "a.hwp")

        assert result.text == "본문[^1] 끝\n\n[^1]: 출처 표: 연도 / 값; 2022 / 5"


class TestOneOddValueInHangulDocuments:
    """`str.isdigit()` is true for '²' too, but `int('²')` raises ValueError. A single attribute value or a single part got the
    whole document rejected as a 'corrupted document'. Only that value is taken as its default and the rest is read."""

    def test_hwpx_odd_cell_span_is_read_as_one(self):
        cell = ('<hp:tc><hp:subList><hp:p><hp:run><hp:t>칸</hp:t></hp:run></hp:p></hp:subList>'
                '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="²" rowSpan="1"/></hp:tc>')
        body = (f"<hp:p><hp:run><hp:tbl><hp:tr>{cell}{builder._hwpx_cell(0, 1, '둘')}</hp:tr>"
                f"<hp:tr>{builder._hwpx_cell(1, 0, '셋')}{builder._hwpx_cell(1, 1, '넷')}</hp:tr></hp:tbl></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "| 칸 | 둘 |\n| --- | --- |\n| 셋 | 넷 |"

    def test_hwpx_odd_auto_number_is_left_out(self):
        body = '<hp:p><hp:run><hp:t>표 </hp:t><hp:ctrl><hp:autoNum num="²" numType="TABLE"/></hp:ctrl><hp:t>현황</hp:t></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "표 현황"

    def test_hwpx_with_a_broken_content_hpf_falls_back_to_section_and_picture_names(self):
        """A broken content.hpf (body order and picture list) got the whole document rejected. The body is found in section number
        order and the pictures by their file names in BinData (the same as the manifest id)."""
        data = builder.zip_with(builder.hwpx_with_images(), edit={
            "Contents/content.hpf": lambda xml: xml.replace("</opf:package>", "")})

        result = convert(data, "a.hwpx")

        assert result.text.startswith("그림 앞 문단")
        assert len(result.images) == 2

    def test_hwp_section_stream_with_an_odd_number_is_skipped(self, monkeypatch):
        record = builder.hwp_record
        body = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("본문"))
        data = _fake_hwp(monkeypatch, sections=[body], extra={"BodyText/Section²": body})

        result = convert(data, "a.hwp")

        assert result.text == "본문"


class TestLinearTimeOnCraftedInput:
    @pytest.mark.timing
    def test_many_unpaired_hwpx_deletions_are_read_in_linear_time(self):
        """Every hp:deleteBegin scanned all its following siblings for its pair, taking 12 s for 16,000 of them (quadratic)."""
        import time

        inner = "".join(f'<hp:deleteBegin Id="{index}"/>x' for index in range(16_000))
        data = builder.hwpx_bytes(f"<hp:p><hp:run><hp:t>{inner}</hp:t></hp:run></hp:p>")

        started = time.perf_counter()
        result = convert(data, "a.hwpx")
        elapsed = time.perf_counter() - started

        assert result.char_count == 16_000
        assert elapsed < 2, "took 12 s before the fix"

    def test_paired_hwpx_deletion_after_many_unpaired_ones_is_still_left_out(self):
        inner = "".join(f'<hp:deleteBegin Id="{index}"/>' for index in range(100))
        body = (f'<hp:p><hp:run><hp:t>가{inner}나<hp:deleteBegin Id="d"/>지운 글<hp:deleteEnd Id="d"/>다'
                '<hp:deleteBegin Id="5"/></hp:t></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "가나다"


class TestHwpRecordBudget:
    """A body stream holding nothing but millions of zero-size records (4-byte header) turned 62KB of compressed data into 16
    million record objects (12 s, 2.4GB). The record count has a per-document limit (`max_uncompressed_bytes` // 128), and the body
    is read as a stream, one top-level record at a time."""

    @staticmethod
    def _flood(count: int) -> bytes:
        record = builder.hwp_record
        return (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("본문"))
                + struct.pack("<I", 0x0FF) * count)

    def test_record_flood_beyond_the_budget_is_too_large(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, sections=[self._flood(10_000)])

        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_records_within_the_budget_are_read(self, monkeypatch):
        data = _fake_hwp(monkeypatch, flags=1, sections=[self._flood(5_000)])

        result = convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=1_000_000))

        assert result.text == "본문"

    def test_records_after_the_character_limit_are_not_parsed(self, monkeypatch):
        """Once reading stops past the limit, the records after it are neither built nor counted (streamed reading)."""
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("가" * 100))
                   + struct.pack("<I", 0x0FF) * 10_000)
        data = _fake_hwp(monkeypatch, flags=1, sections=[section])

        result = convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=1_000_000, max_chars=50))

        assert result.exceeded


def _hwp_paragraph(level: int, *text, shape_id: int = 0) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, level, _para_header(shape_id))
            + builder.hwp_record(TAG_PARA_TEXT, level + 1, builder.hwp_text(*text)))


def _hwp_note(level: int, *paragraphs: bytes) -> bytes:
    """Footnote control of a paragraph at `level`, with its paragraphs (built at `level + 2`) under a LIST_HEADER."""
    return (builder.hwp_record(TAG_CTRL_HEADER, level + 1, builder.hwp_ctrl("fn  "))
            + builder.hwp_record(TAG_LIST_HEADER, level + 2, b"\x01\x00" + b"\x00" * 6) + b"".join(paragraphs))


def _hwpx_note(*paragraphs: str) -> str:
    return "<hp:ctrl><hp:footNote><hp:subList>" + "".join(paragraphs) + "</hp:subList></hp:footNote></hp:ctrl>"


def _hwpx_p(inner: str, shape: str = "0") -> str:
    return f'<hp:p paraPrIDRef="{shape}"><hp:run>{inner}</hp:run></hp:p>'


class TestNotesInsideNotes:
    """A note took its number before its text was rendered, and the reference and definition read the counter afterwards, so a note
    inside a note gave both the inner number: 'Body[^2] end / [^2]: inner / [^2]: outer[^2]'. As in RTF and ODF, a note inside a
    note is left out; a note without text leaves neither reference nor definition (as in DOCX, RTF and ODF) and does not use up a
    number."""

    def test_hwp_note_inside_a_note_is_left_out(self, monkeypatch):
        inner = _hwp_note(2, _hwp_paragraph(4, "inner"))
        outer = _hwp_note(0, _hwp_paragraph(2, "outer", (17, "fn  ")) + inner)
        data = _fake_hwp(monkeypatch, sections=[_hwp_paragraph(0, "Body", (17, "fn  "), " end") + outer])

        assert convert(data, "a.hwp").text == "Body[^1] end\n\n[^1]: outer"

    def test_hwpx_note_inside_a_note_is_left_out(self):
        body = _hwpx_p("<hp:t>Body</hp:t>" + _hwpx_note(_hwpx_p("<hp:t>outer</hp:t>" + _hwpx_note(_hwpx_p("<hp:t>inner</hp:t>"))))
                       + "<hp:t> end</hp:t>")

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "Body[^1] end\n\n[^1]: outer"

    def test_hwp_empty_note_leaves_nothing_and_the_next_note_is_one(self, monkeypatch):
        section = (_hwp_paragraph(0, "A", (17, "fn  "), " B", (17, "fn  "))
                   + _hwp_note(0, _hwp_paragraph(2, "")) + _hwp_note(0, _hwp_paragraph(2, "second")))
        data = _fake_hwp(monkeypatch, sections=[section])

        assert convert(data, "a.hwp").text == "A B[^1]\n\n[^1]: second"

    def test_hwpx_empty_note_leaves_nothing_and_the_next_note_is_one(self):
        body = _hwpx_p("<hp:t>A</hp:t>" + _hwpx_note(_hwpx_p("<hp:t></hp:t>")) + "<hp:t> B</hp:t>"
                       + _hwpx_note(_hwpx_p("<hp:t>second</hp:t>")))

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "A B[^1]\n\n[^1]: second"

    def test_hwpx_note_inside_a_comment_takes_its_own_number(self):
        memo = ('<hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:subList>'
                + _hwpx_p("<hp:t>see</hp:t>" + _hwpx_note(_hwpx_p("<hp:t>memo note</hp:t>")))
                + "</hp:subList></hp:fieldBegin></hp:ctrl>")
        body = _hwpx_p(memo + "<hp:t>Body</hp:t>" + '<hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>'
                       + _hwpx_note(_hwpx_p("<hp:t>body note</hp:t>")))

        text = convert(builder.hwpx_bytes(body), "a.hwpx").text

        assert text.startswith("Body[^2]")
        assert "[^1]: memo note" in text and "[^2]: body note" in text and 'on "Body"): see[^1]' in text


class TestNumberingOutsideTheBody:
    """A numbered paragraph in a header, footer or note advanced the body's counters, so the body went '1. 2. 4.'. They are
    counted on their own."""

    _HWPX_HEADS = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">'
                   '<hh:paraHead start="1" level="1" numFormat="DIGIT">^1.</hh:paraHead></hh:numbering></hh:numberings>'
                   '<hh:paraProperties itemCnt="2"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                   '<hh:paraPr id="1"><hh:heading type="NUMBER" idRef="1" level="0"/></hh:paraPr></hh:paraProperties>')

    def _hwpx(self, body: str) -> bytes:
        return builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", self._HWPX_HEADS + "</hh:refList>")})

    def test_hwpx_header_and_note_numbers_do_not_advance_the_body(self):
        header = ("<hp:ctrl><hp:header><hp:subList>" + _hwpx_p("<hp:t>head item</hp:t>", "1")
                  + "</hp:subList></hp:header></hp:ctrl>")
        body = (_hwpx_p(header + "<hp:t>one</hp:t>", "1")
                + _hwpx_p("<hp:t>two</hp:t>" + _hwpx_note(_hwpx_p("<hp:t>note item</hp:t>", "1")), "1")
                + _hwpx_p("<hp:t>three</hp:t>", "1"))

        result = convert(self._hwpx(body), "a.hwpx", ConvertOptions(include_headers_footers=True))

        assert "1. one\n2. two[^1]\n3. three" in result.text
        assert "1. head item" in result.text and "[^1]: 1. note item" in result.text

    def test_hwp_header_and_note_numbers_do_not_advance_the_body(self, monkeypatch):
        record = builder.hwp_record
        docinfo = (record(TAG_NUMBERING, 0, _numbering(("^1.", 0)))
                   + record(TAG_PARA_SHAPE, 0, _para_shape()) + record(TAG_PARA_SHAPE, 0, _para_shape(2, 0, 1)))
        header = (record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("head")) + record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6)
                  + _hwp_paragraph(2, "head item", shape_id=1))
        section = (_hwp_paragraph(0, (16, "head"), "one", shape_id=1) + header
                   + _hwp_paragraph(0, "two", (17, "fn  "), shape_id=1) + _hwp_note(0, _hwp_paragraph(2, "note item", shape_id=1))
                   + _hwp_paragraph(0, "three", shape_id=1))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)

        result = convert(data, "a.hwp", ConvertOptions(include_headers_footers=True))

        assert "1. one\n2. two[^1]\n3. three" in result.text
        assert "1. head item" in result.text and "[^1]: 1. note item" in result.text
