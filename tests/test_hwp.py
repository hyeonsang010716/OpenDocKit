"""HWPX (XML) and HWP 5.x (binary) conversion.

No Python library can build an OLE compound document, so for HWP only the OLE container is faked, while the record streams
inside it are built in the real format. Results checked against real Hangul (HWP) files are in docs/design.md.
"""

import struct
import time
import zlib

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
from tests.timing import fastest


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

    def test_comment_opened_only_in_a_passed_over_switch_branch_is_kept(self):
        """A comment the case opens and the fallback does not hold is not a copy: taken back with the case, the remark was lost.
        Its range runs on through the fallback text to the hp:fieldEnd after the switch."""
        memo = ('<hp:ctrl><hp:fieldBegin id="7" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>rem</hp:t></hp:run></hp:p>'
                "</hp:subList></hp:fieldBegin></hp:ctrl>")
        body = ("<hp:p><hp:run><hp:t>B </hp:t><hp:switch><hp:case>" + memo + "</hp:case><hp:default><hp:t>fb</hp:t></hp:default>"
                '</hp:switch><hp:t>covered</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="7"/></hp:ctrl></hp:run></hp:p>')

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == 'B fbcovered\n\n> Comment(on "fbcovered"): rem'
        note = ("<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>src</hp:t></hp:run></hp:p></hp:subList></hp:footNote>"
                "</hp:ctrl>")
        noted = convert(builder.hwpx_bytes(body.replace("<hp:t>rem</hp:t>", "<hp:t>rem</hp:t>" + note)), "a.hwpx")
        assert noted.text == 'B fbcovered\n\n[^1]: src\n\n> Comment(on "fbcovered"): rem[^1]'

    def test_note_in_a_comment_of_a_passed_over_switch_branch_is_numbered_once(self):
        """The comment the fallback holds too is taken back with the note inside it and the note's number: the note of the
        fallback's copy is the first one."""
        memo = ('<hp:ctrl><hp:fieldBegin id="5" type="MEMO"><hp:subList><hp:p><hp:run><hp:t>check</hp:t><hp:ctrl><hp:footNote>'
                "<hp:subList><hp:p><hp:run><hp:t>src</hp:t></hp:run></hp:p></hp:subList></hp:footNote></hp:ctrl></hp:run></hp:p>"
                "</hp:subList></hp:fieldBegin></hp:ctrl>")
        body = ("<hp:p><hp:run><hp:t>Before </hp:t><hp:switch><hp:case>" + memo + "</hp:case>"
                "<hp:default>" + memo + '<hp:t>fallback</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="5"/></hp:ctrl></hp:default>'
                "</hp:switch></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == 'Before fallback\n\n[^1]: src\n\n> Comment(on "fallback"): check[^1]'

    def test_switch_branch_whose_text_is_followed_by_an_empty_box_is_the_one_read(self):
        """A text box flushes the text before it into a block of its own. The text of the case went with it, so the case looked
        empty and the fallback was written after it as well."""
        box = "<hp:rect><hp:drawText><hp:subList><hp:p><hp:run><hp:t></hp:t></hp:run></hp:p></hp:subList></hp:drawText></hp:rect>"
        body = ("<hp:p><hp:run><hp:t>B </hp:t><hp:switch><hp:case><hp:t>new</hp:t>" + box + "</hp:case>"
                "<hp:default><hp:t>old</hp:t></hp:default></hp:switch></hp:run></hp:p>")

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "B new"

    def test_text_art_is_kept(self):
        """The text of text art (hp:textart) is in the text attribute, not the element text, so it was dropped. ␍␊ is a line break."""
        body = '<hp:p><hp:run><hp:textart text="hwpx 라이브러리 테스트␍␊neolord"/></hp:run></hp:p>'

        result = convert(builder.hwpx_bytes(body), "a.hwpx")

        assert result.text == "hwpx 라이브러리 테스트\nneolord"

    def test_text_art_caption_is_kept(self):
        """hp:textart has an hp:caption like a picture or equation; it was dropped. It is a block before (TOP) or after the art."""
        def art(side: str) -> str:
            return (f'<hp:p><hp:run><hp:textart text="Slogan"><hp:caption side="{side}"><hp:subList><hp:p><hp:run>'
                    "<hp:t>그림 2. 표어</hp:t></hp:run></hp:p></hp:subList></hp:caption></hp:textart></hp:run></hp:p>")

        assert convert(builder.hwpx_bytes(art("BOTTOM")), "a.hwpx").text == "Slogan\n\n그림 2. 표어"
        assert convert(builder.hwpx_bytes(art("TOP")), "a.hwpx").text == "그림 2. 표어\n\nSlogan"

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
    # A real OLE container, so that the reading of its streams is the one of a real file. `monkeypatch` is no longer used
    storages = () if any(name.startswith("BodyText/") for name in streams) else ("BodyText",)
    return builder.ole_file(streams, storages=storages)


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

    def test_uncompressed_streams_are_bounded_by_uncompressed_limit(self, monkeypatch):
        """Streams stored without compression were never counted. The directory entries of an OLE file can all point at one
        sector chain, so 300 picture entries on one 4 MB stream read 1.2 GB from a 4 MB file and 100 section entries on one
        1 MB stream took 12 s, past any limit. They count as a stored ZIP member does."""
        record = builder.hwp_record
        section = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, "가".encode("utf-16-le") * 50_000)
        data = _fake_hwp(monkeypatch, flags=0, sections=[section] * 20)  # 20 sections of 100 KB, as if aliased to one stream

        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=1_000_000, max_chars=None))
        assert convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=3_000_000, max_chars=None)).text.count("가") == 1_000_000

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


class TestNumberingLevelsEightToTen:
    """Hangul 5.1 numbers to level 10: a NUMBERING record carries levels 8-10 after the per-level start numbers, and a paragraph
    shape keeps levels 8-10 in the UINT32 at byte 54, its 3-bit level stopping at 6. Both were read only to level 7, so the
    paragraphs of levels 8-10 came out with the numbers of level 7 (`②③④` where the HWPX save of a Hancom file numbered to
    level 10 has `㉮`). The number shapes are 5 bits (15 갑을병, 16 甲乙丙)."""

    @staticmethod
    def _docinfo() -> bytes:
        record = builder.hwp_record
        levels = [("^1.", 0)] * 7 + [("^8)", 15), ("^9.", 16), ("(^10)", 0)]
        payload = b"".join(struct.pack("<IhhIH", shape << 5, 0, 0, 0, len(template)) + template.encode("utf-16-le")
                           for template, shape in levels[:7])
        payload += struct.pack("<H7I", 1, *[1] * 7)
        payload += b"".join(struct.pack("<IhhIH", shape << 5, 0, 0, 0, len(template)) + template.encode("utf-16-le")
                            for template, shape in levels[7:])
        payload += struct.pack("<3I", 1, 1, 1)
        shapes = [_para_shape(), _para_shape(2, 0, 1)] + [_para_shape(2, 6, 1) + struct.pack("<I", tail) for tail in (7, 8, 9)]
        return record(TAG_NUMBERING, 0, payload) + b"".join(record(TAG_PARA_SHAPE, 0, shape) for shape in shapes)

    def test_paragraphs_of_levels_eight_to_ten_take_their_numbers(self, monkeypatch):
        record = builder.hwp_record
        section = b"".join(record(TAG_PARA_HEADER, 0, _para_header(shape)) + record(TAG_PARA_TEXT, 1, builder.hwp_text(text))
                           for shape, text in ((1, "하나"), (2, "여덟"), (3, "아홉"), (4, "열"), (2, "다시")))

        lines = convert(_fake_hwp(monkeypatch, sections=[section], docinfo_extra=self._docinfo()), "a.hwp").text.splitlines()

        assert [line.strip() for line in lines] == ["1. 하나", "- 갑) 여덟", "- 甲. 아홉", "- (1) 열", "- 을) 다시"]

    def test_record_without_levels_eight_to_ten_keeps_seven(self):
        from opendockit.extractors.hwp import _numbering_levels

        assert len(_numbering_levels(_numbering(("^1.", 0)))) == 7
        assert len(_numbering_levels(_numbering(("^1.", 0)) + b"\x05\x00")) == 7   # a broken tail is left out

    @pytest.mark.parametrize(("shape", "values", "expected"), [
        ("DECAGON_CIRCLE", (1, 2, 10, 11), ["갑", "을", "계", "11"]),
        ("DECAGON_CIRCLE_HANJA", (1, 3, 10), ["甲", "丙", "癸"]),
    ])
    def test_heavenly_stem_shapes(self, shape, values, expected):
        from opendockit.extractors._lists import format_number

        assert [format_number(value, shape) for value in values] == expected


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


class TestSoftHyphen:
    """HWP code 24 and HWPX hp:hypen are a soft hyphen, drawn only at a line break inside the word. Hancom's PDF of
    `Multilingual Word<hyphen>processor` reads `Wordprocessor`; written as `-`, HWP gave `Word-processor`, another word than
    the HWPX save of the same document."""

    def test_hwp_soft_hyphen_is_left_out(self, monkeypatch):
        text = "Word".encode("utf-16-le") + struct.pack("<H", 24) + builder.hwp_text("processor")
        section = builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + builder.hwp_record(TAG_PARA_TEXT, 1, text)

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "Wordprocessor"

    @pytest.mark.parametrize("element", ["hypen", "hyphen"])
    def test_hwpx_soft_hyphen_is_left_out(self, element):
        body = f"<hp:p><hp:run><hp:t>Word<hp:{element}/>processor</hp:t></hp:run></hp:p>"

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "Wordprocessor"


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

    def test_tab_or_line_break_in_ruby_text_is_a_space(self, monkeypatch):
        """The control characters of the text a control holds are dropped, but a tab or a line break there separates words:
        dropped too, '본\t말' became '본말'. It is a space, as before."""
        record = builder.hwp_record
        ruby = "tdut"[::-1].encode("latin-1") + b"".join(
            struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in ("본\t말", "덧\r\n말")) + b"\x00" * 10
        composed = "tcps"[::-1].encode("latin-1") + struct.pack("<H", 3) + "가\n나".encode("utf-16-le") + b"\x00" * 10
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("앞 ", (23, "tdut"), " ", (23, "tcps"), " 뒤")),
            record(TAG_CTRL_HEADER, 1, ruby),
            record(TAG_CTRL_HEADER, 1, composed),
        ])

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "앞 본 말(덧 말) 가 나 뒤"

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

    @staticmethod
    def _tcps(text: str, border: int) -> bytes:
        units = text.encode("utf-16-le")
        return "tcps"[::-1].encode("latin-1") + struct.pack("<H", len(units) // 2) + units + bytes([border, 0xfd, 0, 0x0a]) + b"\xff" * 40

    def _hwp(self, monkeypatch, *controls: bytes) -> str:
        record = builder.hwp_record
        section = b"".join([record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
                            record(TAG_PARA_TEXT, 1, builder.hwp_text(*[part for _ in controls for part in ((23, "tcps"), " ")])),
                            *[record(TAG_CTRL_HEADER, 1, control) for control in controls]])
        return convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text

    @pytest.mark.parametrize(("text", "border"), [
        ("\U000f0289\U000f0294", 1),              # 12 in a circle: the private-use pieces of a tens 1 and a ones 2
        ("\U000f029d\U000f02a8", 2),              # in a reversed circle
        ("\U000f02ba\U000f02c5", 3),              # in a box
        ("\U000f02d7\U000f02e2", 4),              # in a reversed box
        ("\U000f02ba\U000f02c5", 0),              # the pieces of a box, with no border kind
        ("\ua2ba\ua2c5", 3),                      # the same written as HWP 5 units 0xA000 | X
    ])
    def test_hwp_number_in_a_frame_is_its_digits(self, monkeypatch, text, border):
        """Hangul writes a two- or three-digit number in a frame as private-use pieces of its digits; the HWPX save of the same
        document holds the digits in composeText."""
        assert self._hwp(monkeypatch, self._tcps(text, border)) == "12"

    @pytest.mark.parametrize(("text", "expected"), [
        ("\U000f0289", "①"),               # one piece is a symbol of its own (`_hancom_symbols`)
        ("\U000f0289\U000f028a", "①②"),   # two pieces of the tens place are no number: each is its symbol
        ("\U000f0289가", "①가"),            # a piece among letters
    ])
    def test_hwp_pieces_that_are_no_framed_number(self, monkeypatch, text, expected):
        assert self._hwp(monkeypatch, self._tcps(text, 0)) == expected

    def test_hwp_three_digit_number_in_a_circle(self, monkeypatch):
        assert self._hwp(monkeypatch, self._tcps("\U000f0491\U000f049c\U000f04a7", 1)) == "123"

    @pytest.mark.parametrize(("frame", "border"), [("◯", 1), ("●", 2), ("□", 3), ("■", 4), ("△", 5), ("▲", 6), ("☼", 7),
                                                   ("◇", 8), ("◆", 9), ("▢", 10), ("♲", 11), ("♺", 12), ("♻", 13)])
    def test_hwp_frame_glyph_of_the_border_kind_is_left_out(self, monkeypatch, frame, border):
        """HWP writes the frame first (`△12`, `◯가`); HWPX keeps it as circleType and writes `12`, `가`. Matched on a Hancom file
        holding every border kind in both formats."""
        assert self._hwp(monkeypatch, self._tcps(f"{frame}가나", border)) == "가나"

    def test_hwp_glyph_that_is_not_the_frame_of_the_border_kind_stays(self, monkeypatch):
        assert self._hwp(monkeypatch, self._tcps("△가", 1), self._tcps("한글", 0), self._tcps("◯", 1)) == "△가 한글 ◯"

    def test_hwpx_number_in_a_frame_is_its_digits(self):
        body = ('<hp:p><hp:run><hp:compose circleType="SHAPE_RECTANGLE" composeText="&#xF02BA;&#xF02C3;"/>'
                "<hp:t> 공공부문</hp:t></hp:run></hp:p>")

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "10 공공부문"


class TestHancomSymbols:
    """Hangul writes its own symbols as plane-15 private-use characters (HWPX U+F0000 | X, HWP 5 also the unit 0xA000 | X), which
    the cleaning removed: the seal mark `(인)` of a form, boxed numbers, bullets and book brackets were lost, and HWP 5 units like
    0xA832 came out as letters of another script. The symbols whose meaning Hancom's PDFs show are written out."""

    def _hwp(self, monkeypatch, text: str) -> str:
        section = (builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(text)))
        return convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text

    def test_hwp_symbols_are_written_out(self, monkeypatch):
        text = "신청인 : 홍길동 \U000f012b \U000f0854한성순보\U000f0855 \U000f02b1 항목 \U000f03c5 확인"

        assert self._hwp(monkeypatch, text) == "신청인 : 홍길동 (인) 『한성순보』 ① 항목 □ 확인"

    def test_hwp_5_units_of_hancom_symbols_are_written_out(self, monkeypatch):
        assert self._hwp(monkeypatch, "기증자 : \ua12b \ua2b1 \ua2b2") == "기증자 : (인) ① ②"

    def test_letters_of_the_yi_and_syloti_nagri_blocks_that_are_no_hancom_symbol_stay(self, monkeypatch):
        """Hangul keeps U+A813 as it is in an English word set in Malgun Gothic; only the measured units are symbols."""
        assert self._hwp(monkeypatch, "spectro\ua813scopy \ua000") == "spectro\ua813scopy \ua000"

    def test_line_pieces_and_unknown_symbols_are_left_out(self, monkeypatch):
        """Rule and box pieces (`\U000f080f` drawn as a rule, `\ua832` a double rule) and symbols whose meaning is not known."""
        assert self._hwp(monkeypatch, "제목\U000f080f\U000f080f \ua832\ua832 끝\U000f03e0") == "제목 끝"

    def test_hwpx_symbols_are_written_out(self):
        body = "<hp:p><hp:run><hp:t>&#xF0854;조선 상고사&#xF0855; &#xF012B; &#xF080F;&#xF080F;</hp:t></hp:run></hp:p>"

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "『조선 상고사』 (인)"


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


def _form_properties(caption: str = "", value: int | None = None, text: str | None = None) -> str:
    """The properties string of an HWP form object, as Hangul writes it (`name:type:value` items, sets and wstrings by length)."""
    common = "Name:wstring:8:CheckBox ForeColor:int:0 GroupName:wstring:0: TabOrder:int:2 Enabled:bool:1 "
    items = f"CommonSet:set:{len(common)}:{common} "
    if text is not None:
        edit = f"Text:wstring:{len(text)}:{text} MultiLine:bool:0 AlignText:int:0 "
        items += f"EditSet:set:{len(edit)}:{edit} "
    else:
        button = f"Caption:wstring:{len(caption)}:{caption} " + (f"Value:int:{value} " if value is not None else "")
        items += f"ButtonSet:set:{len(button)}:{button} "
    return items


def _hwp_form(level: int, kind: bytes, properties: str) -> bytes:
    """The `form` CTRL_HEADER of a paragraph at `level - 1` and its FORM_OBJECT record (kind twice, UINT32, WORD length, WCHARs)."""
    from opendockit.extractors.hwp import TAG_FORM_OBJECT
    body = kind * 2 + struct.pack("<I", len(properties)) + struct.pack("<H", len(properties)) + properties.encode("utf-16-le")
    return builder.hwp_record(TAG_CTRL_HEADER, level, builder.hwp_ctrl("form")) + builder.hwp_record(TAG_FORM_OBJECT, level + 1, body)


def _hwp_ole_storage(streams: dict[str, bytes]) -> bytes:
    """The BinData stream of an HWP OLE object: a UINT32 size and an OLE container of `streams`."""
    container = builder.ole_file(streams)
    return struct.pack("<I", len(container)) + container


def _hwp_with_ole_objects(monkeypatch, bins: dict[int, bytes], *objects: int, text: str = "매출 현황:") -> bytes:
    """An HWP whose first paragraph holds one drawing object (gso) per BinItem ID in `objects`, each an OLE object
    (SHAPE_COMPONENT_OLE: attribute, extent, BinItem ID); `bins` are the BinData streams, as STORAGE items (kind 2) of DocInfo."""
    record = builder.hwp_record
    docinfo = b"".join(record(TAG_BIN_DATA, 0, struct.pack("<HHH", 0x2, item, 3) + "OLE".encode("utf-16-le"))
                       for item in range(1, max(bins) + 1))
    section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
               + record(TAG_PARA_TEXT, 1, builder.hwp_text(text, *[(11, "gso ")] * len(objects)))
               + b"".join(record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso "))
                          + record(TAG_SHAPE_OLE, 2, struct.pack("<IiiH", 1, 7200, 7200, item) + b"\x00" * 16) for item in objects))
    extra = {f"BinData/BIN{item:04X}.OLE": data for item, data in bins.items()}
    return _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo, extra=extra)


class TestHwpCharts:
    """A Hangul chart is an OLE object whose BinData stream holds an OLE container with the chart as OOXML XML in its
    `OOXMLChartContents` stream, the same part the HWPX save of the document keeps as Chart/chart1.xml. HWP gave `[OLE object]`
    and a warning where HWPX gave the chart's values; in 92 public Hangul files saved both ways, the two now give the same text."""

    _CHART = {"OOXMLChartContents": _CHART_XML.encode(), "Contents": b"\x00" * 64}

    def test_chart_values_are_kept(self, monkeypatch):
        result = convert(_hwp_with_ole_objects(monkeypatch, {1: _hwp_ole_storage(self._CHART)}, 1), "a.hwp")

        assert result.text == "매출 현황:\n\n[Chart] 판매\n\n| Category | 판매량 |\n| --- | --- |\n| 1월 | 5 |\n| 2월 | 7 |"
        assert result.warnings == []

    def test_chart_referenced_again_gives_its_title_line(self, monkeypatch):
        result = convert(_hwp_with_ole_objects(monkeypatch, {1: _hwp_ole_storage(self._CHART)}, 1, 1), "a.hwp")

        assert result.text.count("| 1월 | 5 |") == 1
        assert result.text.endswith("\n\n[Chart] 판매")

    @pytest.mark.parametrize("data", [
        _hwp_ole_storage({"Workbook": b"\x09\x08" + b"\x00" * 30}),       # a pasted sheet, not a chart
        _hwp_ole_storage({"OOXMLChartContents": b"<not xml"}),
        _hwp_ole_storage({"OOXMLChartContents": b"<a/>"}),
        struct.pack("<I", 9999) + b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40,  # a broken container
        b"\x01\x02",
        # a mini sector shift of 200: olefile raised OverflowError, and the document failed that converted before
        (lambda data: data[:36] + struct.pack("<H", 200) + data[38:])(_hwp_ole_storage({"OOXMLChartContents": _CHART_XML.encode()})),
    ], ids=["workbook", "broken xml", "other xml", "broken container", "too short", "huge mini sector shift"])
    def test_ole_object_that_is_no_chart_stays_a_placeholder(self, monkeypatch, data):
        result = convert(_hwp_with_ole_objects(monkeypatch, {1: data}, 1), "a.hwp")

        assert result.text == "매출 현황: [OLE object]"
        assert result.warnings == ["The contents of 1 OLE object(s) (pasted tables, charts, etc.) could not be converted."]

    def test_object_referenced_many_times_is_read_once(self, monkeypatch):
        """What an object is, a chart or not, is kept: opened and parsed again at each reference, 2,000 references to one object
        of 300,000 empty elements took 28 s."""
        from opendockit.extractors import hwp

        calls = []
        original = hwp._chart_part
        monkeypatch.setattr(hwp, "_chart_part", lambda data: calls.append(1) or original(data))
        data = _hwp_with_ole_objects(monkeypatch, {1: _hwp_ole_storage({"OOXMLChartContents": b"<a>" + b"<b/>" * 1000 + b"</a>"}),
                                                   2: _hwp_ole_storage(self._CHART)}, *[1, 2] * 50)

        result = convert(data, "a.hwp")

        assert len(calls) == 2
        assert result.text.count("[OLE object]") == 50 and result.text.count("[Chart] 판매") == 50

    def test_large_ole_object_does_not_make_the_document_too_large(self, monkeypatch):
        """An OLE object is read only to look for a chart, under a budget of its own: a pasted sheet of 3 MB made a document fail
        as too large under `max_uncompressed_bytes` of 2 MB, which it never read before."""
        record = builder.hwp_record
        sheet = _hwp_ole_storage({"Workbook": bytes(3_000_000)})
        stored = zlib.compress(sheet)[2:-4]
        docinfo = record(TAG_BIN_DATA, 0, struct.pack("<HHH", 0x2 | 1 << 4, 1, 3) + "OLE".encode("utf-16-le"))
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("표:", (11, "gso ")))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso "))
                   + record(TAG_SHAPE_OLE, 2, struct.pack("<IiiH", 1, 7200, 7200, 1) + b"\x00" * 16))
        second = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("둘째"))
        data = _fake_hwp(monkeypatch, sections=[section, second], docinfo_extra=docinfo, extra={"BinData/BIN0001.OLE": stored})

        result = convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=2_000_000))

        # The section read after the object is within the document's budget: charged to it, the object made it too large
        assert result.text == "표: [OLE object]\n\n둘째"

    def test_bin_items_of_each_kind_name_their_stream(self):
        """Pictures are read from EMBEDDING items only and OLE objects from STORAGE items only: a picture record pointing at an
        OLE object gave the picture decoder its container."""
        from opendockit.extractors.hwp import parse_bin_items

        records = [builder.hwp_record(TAG_BIN_DATA, 0, struct.pack("<HHH", kind, 1, 3) + "OLE".encode("utf-16-le")) for kind in (0, 1, 2)]
        doc_info = parse_records(b"".join(records))

        assert parse_bin_items(doc_info) == [(None, None), ("BinData/BIN0001.OLE", None), (None, None)]
        assert parse_bin_items(doc_info, 2) == [(None, None), (None, None), ("BinData/BIN0001.OLE", None)]


class TestHwpFormObjects:
    """HWP form objects keep their value in the properties string of the FORM_OBJECT record, and were not read: a form of nothing
    but form objects gave only its preview text. They are read as in HWPX (`TestHwpxFormObjects`); the kinds and values were
    matched against the HWPX save of the same Hangul document (check box `Value:int:1` is value="CHECKED")."""

    def _paragraph(self, text: str, *forms: tuple[bytes, str]) -> bytes:
        record = builder.hwp_record
        return (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                + record(TAG_PARA_TEXT, 1, builder.hwp_text(text, *[(11, "form")] * len(forms)))
                + b"".join(_hwp_form(1, kind, properties) for kind, properties in forms))

    def test_check_boxes_radio_buttons_and_values_are_kept(self, monkeypatch):
        section = (self._paragraph("동의:", (b"tbc+", _form_properties("개인정보 수집", 1)), (b"tbc+", _form_properties("마케팅 수신", 0)))
                   + self._paragraph("성별:", (b"tbr+", _form_properties("남", 0)), (b"tbr+", _form_properties("여", 1)),
                                     (b"tbp+", _form_properties("제출")))
                   + self._paragraph("사번:", (b"tde+", _form_properties(text="1234: 가나 다")), (b"boc+", _form_properties(text=""))))

        result = convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp")

        assert result.text == "동의: ☒ 개인정보 수집 ☐ 마케팅 수신\n\n성별: ☐ 남 ☒ 여\n\n사번: 1234: 가나 다"

    def test_double_ampersand_of_a_caption_is_one(self, monkeypatch):
        """Hancom's PDF of a real form shows the caption `R&&D 자율성트랙(일반)` as `R&D 자율성트랙(일반)`, in HWP and HWPX alike."""
        section = self._paragraph("트랙:", (b"tbc+", _form_properties("R&&D 자율성트랙(일반)", 1)))
        hwpx = builder.hwpx_bytes('<hp:p><hp:run><hp:t>트랙:</hp:t><hp:checkBtn caption="R&amp;&amp;D 자율성트랙(일반)" '
                                  'value="CHECKED" name="x"/></hp:run></hp:p>')

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "트랙: ☒ R&D 자율성트랙(일반)"
        assert convert(hwpx, "a.hwpx").text == "트랙: ☒ R&D 자율성트랙(일반)"

    def test_text_is_taken_by_its_length_and_names_are_whole(self):
        """A wstring value may hold spaces and colons; `AlignText` is not `Text`."""
        from opendockit.extractors.hwp import _form_properties as properties

        assert properties("EditSet:set:41:Text:wstring:10:a b:c Text AlignText:int:3 ") == {"Text": "a b:c Text", "AlignText": "3"}

    @pytest.mark.parametrize("broken", ["", "Caption", "Caption:wstring", "Caption:wstring:x:abc", "Caption:wstring:99:short",
                                        "a:set:-1:b:int:2", ":::"])
    def test_broken_properties_do_not_fail(self, broken):
        from opendockit.extractors.hwp import _form_properties as properties

        assert isinstance(properties(broken), dict)

    def test_broken_form_record_gives_nothing(self, monkeypatch):
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("앞", (11, "form")))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("form")) + record(0x10 + 75, 2, b"tbc+tb"))

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "앞"


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

    @pytest.mark.parametrize("preview", ["<.<><><>\r\n<><><><>\r\n<><><><><><> >\r\n", "<->\r\n<·><>"])
    def test_preview_of_marks_and_punctuation_only_is_not_used(self, monkeypatch, preview):
        """Empty tables with a `.` in a text box: the preview had the `.` between the cell marks and became the body."""
        empty = builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
        hwp = _fake_hwp(monkeypatch, sections=[empty], preview=preview)
        hwpx = builder.zip_with(builder.hwpx_bytes("<hp:p><hp:run/></hp:p>"), add={"Preview/PrvText.txt": preview})

        for data, name in ((hwp, "a.hwp"), (hwpx, "a.hwpx")):
            result = convert(data, name)

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
    """Comments (review remarks): HWP and HWPX put them at the end of the document as `> Comment(author, on "range"): text` lines,
    like DOCX and PPTX (comments were dropped without a warning, so review remarks could not be seen). The commented text stays in
    the body as is. In HWP the range is a field whose command is `MEMO/<shape>/<number>/<time>/<time>/<author>/`, and the text a
    MEMO_LIST record with that number among the children of a paragraph, followed by its paragraphs (Hancom files saved both ways
    give the same comment lines)."""

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
            # The fastest of a few runs. A single pause of the machine (about 0.2 s on the Windows runner, where the conversion
            # itself measured linear) in the one run of the larger document read as a conversion slower than linear.
            return fastest(lambda: convert(data, "a.hwpx")), convert(data, "a.hwpx").text

        small, _ = elapsed(4_000)
        large, text = elapsed(8_000)

        assert large < 3 * small + 0.1
        assert text.endswith("> Comment: 메모7998\n> Comment: 메모7999")

    def test_hwp_paragraphs_under_a_memo_field_control_are_left_out(self, monkeypatch):
        """HWP reads only the inner paragraphs of the controls it knows how to read (table, text box, footnote, header). Paragraphs
        under a field control are not read either: a comment's text is not there but in its MEMO_LIST (`test_hwp_memo_...`)."""
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


def _hwp_memo_document(monkeypatch, memos: list[tuple[int, str, str]], texts: dict[int, str], body: str = "운영체제: ",
                       after: str = " LTS") -> bytes:
    """An HWP of one paragraph holding a comment field per (number, author, range text) in `memos`, and the comment text of each
    number in `texts` under a last paragraph (MEMO_LIST, LIST_HEADER, paragraph), as Hangul saves them."""
    from opendockit.extractors.hwp import TAG_MEMO_LIST
    record = builder.hwp_record
    parts: list = [body]
    controls = b""
    for number, author, target in memos:
        parts += [(3, "%unk"), target, (4, "%unk")]
        controls += record(TAG_CTRL_HEADER, 1, _hwp_field("%unk", f"MEMO/65535/{number}/1112218336/31288509/{author}/\\;;"))
    parts.append(after)
    last = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("끝"))
    for number, text in texts.items():
        last += (record(TAG_MEMO_LIST, 1, struct.pack("<I", number)) + record(TAG_LIST_HEADER, 1, b"\x01\x00" + b"\x00" * 6)
                 + record(TAG_PARA_HEADER, 1, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 2, builder.hwp_text(text)))
    section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text(*parts))
               + controls + last)
    return _fake_hwp(monkeypatch, sections=[section])


class TestHwpMemo:
    def test_hwp_memo_is_written_after_the_body_with_its_target_text(self, monkeypatch):
        data = _hwp_memo_document(monkeypatch, [(1, "검토자", "Ubuntu 22.04")], {1: "상세 버전 정보 부탁드립니다."})

        result = convert(data, "a.hwp")

        assert result.text == '운영체제: Ubuntu 22.04 LTS\n\n끝\n\n> Comment(검토자, on "Ubuntu 22.04"): 상세 버전 정보 부탁드립니다.'

    def test_hwp_memos_follow_their_fields_and_texts_their_numbers(self, monkeypatch):
        """Hangul writes the MEMO_LIST records in the order of the fields, and a reply is a memo of its own on the same range."""
        data = _hwp_memo_document(monkeypatch, [(1, "kkomi", "모를 답"), (5, "kkomi", "모를 답"), (2, "", "니다")],
                                  {5: "댓글입니다.", 1: "첫 메모", 2: "둘째"})

        lines = convert(data, "a.hwp").text.splitlines()[-3:]

        assert lines == ['> Comment(kkomi, on "모를 답"): 첫 메모', '> Comment(kkomi, on "모를 답"): 댓글입니다.',
                         '> Comment(on "니다"): 둘째']

    def test_hwp_memo_without_text_writes_nothing(self, monkeypatch):
        data = _hwp_memo_document(monkeypatch, [(1, "검토자", "Ubuntu 22.04"), (9, "검토자", " LTS")], {1: "확인"}, after="")

        assert convert(data, "a.hwp").text.endswith('> Comment(검토자, on "Ubuntu 22.04"): 확인')

    def test_link_in_the_text_of_a_comment_is_plain(self, monkeypatch):
        """The text of a comment is plain, as in HWPX and DOCX."""
        from opendockit.extractors.hwp import TAG_MEMO_LIST
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("가", (3, "%unk"), "B", (4, "%unk")))
                   + record(TAG_CTRL_HEADER, 1, _hwp_field("%unk", "MEMO/65535/1/0/0/a/\\;;"))
                   + record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("끝"))
                   + record(TAG_MEMO_LIST, 1, struct.pack("<I", 1)) + record(TAG_LIST_HEADER, 1, b"\x01\x00" + b"\x00" * 6)
                   + record(TAG_PARA_HEADER, 1, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 2, builder.hwp_text("see ", (3, "%hlk"), "site", (4, "%hlk")))
                   + record(TAG_CTRL_HEADER, 2, _hwp_field("%hlk", "https\\://x.org/;1;0;0;")))

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text.endswith('> Comment(a, on "B"): see site')

    def test_comment_of_a_repeated_header_is_written_once(self):
        header = ('<hp:ctrl><hp:header><hp:subList><hp:p><hp:run><hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:parameters cnt="1">'
                  '<hp:stringParam name="Author">hdr</hp:stringParam></hp:parameters><hp:subList><hp:p><hp:run><hp:t>h</hp:t>'
                  '</hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl><hp:t>Form</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="1"/>'
                  "</hp:ctrl></hp:run></hp:p></hp:subList></hp:header></hp:ctrl>")
        body = f"<hp:p><hp:run>{header}<hp:t>첫</hp:t></hp:run></hp:p><hp:p><hp:run>{header}<hp:t>둘</hp:t></hp:run></hp:p>"

        for options in (ConvertOptions(), ConvertOptions(include_headers_footers=True)):
            text = convert(builder.hwpx_bytes(body), "a.hwpx", options).text

            assert text.count('> Comment(hdr, on "Form"): h') == 1

    def test_comment_of_a_repeated_hwp_header_is_written_once(self, monkeypatch):
        from opendockit.extractors.hwp import TAG_MEMO_LIST
        record = builder.hwp_record

        def section(body: str, *memo_text: str) -> bytes:
            header = (record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("head")) + record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6)
                      + record(TAG_PARA_HEADER, 2, builder.hwp_para_header())
                      + record(TAG_PARA_TEXT, 3, builder.hwp_text((3, "%unk"), "Form", (4, "%unk")))
                      + record(TAG_CTRL_HEADER, 3, _hwp_field("%unk", "MEMO/65535/1/0/0/hdr/\\;;")))
            memos = b"".join(record(TAG_MEMO_LIST, 1, struct.pack("<I", 1)) + record(TAG_LIST_HEADER, 1, b"\x01\x00" + b"\x00" * 6)
                             + record(TAG_PARA_HEADER, 1, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 2, builder.hwp_text(text))
                             for text in memo_text)
            return (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text((16, "head"), body))
                    + header + memos)

        data = _fake_hwp(monkeypatch, sections=[section("첫", "h"), section("둘", "h")])

        for options in (ConvertOptions(), ConvertOptions(include_headers_footers=True)):
            assert convert(data, "a.hwp", options).text.count('> Comment(hdr, on "Form"): h') == 1

    def test_field_with_another_command_is_no_memo(self, monkeypatch):
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("가", (3, "%unk"), "나", (4, "%unk")))
                   + record(TAG_CTRL_HEADER, 1, _hwp_field("%unk", "MEMOS/1/2/3/4/a/")))

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "가나"


def _hwpx_link(field_id: int, command: str, text: str) -> str:
    """A hyperlink field of HWPX around `text`: the address is in the Command parameter, as Hangul writes it."""
    return (f'<hp:ctrl><hp:fieldBegin id="{field_id}" type="HYPERLINK" name=""><hp:parameters cnt="2">'
            '<hp:integerParam name="Prop">0</hp:integerParam>'
            f'<hp:stringParam name="Command">{command}</hp:stringParam></hp:parameters></hp:fieldBegin></hp:ctrl>'
            f'<hp:t>{text}</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="{field_id}"/></hp:ctrl>')


def _hwp_field(control: str, command: str = "") -> bytes:
    """The CTRL_HEADER payload of an HWP field: control ID, attribute, other attribute, command (WORD length + WCHARs), id."""
    return (control[::-1].encode("latin-1") + struct.pack("<IBH", 0, 0, len(command)) + command.encode("utf-16-le")
            + struct.pack("<I", 7))


def _hwpx_click_here(field_id: int, guide: str, text: str) -> str:
    """A click-here field (누름틀) of HWPX holding `text`, its guide in the Command parameter as Hangul writes it."""
    command = f"Clickhere:set:44:Direction:wstring:{len(guide)}:{guide} HelpState:wstring:0:  "
    return (f'<hp:ctrl><hp:fieldBegin id="{field_id}" type="CLICK_HERE" name="{guide}" editable="1" dirty="0"><hp:parameters cnt="2">'
            f'<hp:integerParam name="Prop">9</hp:integerParam><hp:stringParam name="Command">{command}</hp:stringParam>'
            f'</hp:parameters></hp:fieldBegin></hp:ctrl><hp:t>{text}</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="{field_id}"/></hp:ctrl>')


class TestClickHereFields:
    """Hangul shows the guide of a click-here field (누름틀) that was not filled in (`이곳을 마우스로 누르고 내용을 입력하세요`, or a
    word such as `날짜`) in its place on screen and does not print it, and it was read as the content of the form. The text of a
    field is left out when it is its guide; what a person typed stays."""

    def test_hwpx_unfilled_guide_is_left_out(self):
        guide = "이곳을 마우스로 누르고 내용을 입력하세요."
        body = (f"<hp:p><hp:run><hp:t>성명: </hp:t>{_hwpx_click_here(1, guide, guide)}</hp:run></hp:p>"
                f"<hp:p><hp:run><hp:t>소속: </hp:t>{_hwpx_click_here(2, guide, '총무과')}</hp:run></hp:p>"
                f"<hp:p><hp:run>{_hwpx_click_here(3, '날짜', '날짜')}</hp:run></hp:p><hp:p><hp:run><hp:t>끝</hp:t></hp:run></hp:p>")

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "성명:\n\n소속: 총무과\n\n끝"

    def test_hwp_unfilled_guide_is_left_out(self, monkeypatch):
        guide = "이곳을 마우스로 누르고 내용을 입력하세요."
        command = f"Clickhere:set:44:Direction:wstring:{len(guide)}:{guide} HelpState:wstring:0:  "
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("성명: ", (3, "%clk"), guide, (4, "%clk"), " 소속: ", (3, "%clk"), "총무과",
                                                      (4, "%clk"))),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%clk", command)),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%clk", command)),
        ])

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "성명: 소속: 총무과"

    def test_unfilled_guide_with_a_hancom_symbol_is_dropped(self, monkeypatch):
        """The text of the field has its Hancom symbols written out; so has the guide it is compared with."""
        guide = "\U000f012b 서명"
        body = f"<hp:p><hp:run><hp:t>성명 </hp:t>{_hwpx_click_here(1, guide, guide)}</hp:run></hp:p>"
        command = f"Clickhere:set:44:Direction:wstring:{len(guide.encode('utf-16-le')) // 2}:{guide} HelpState:wstring:0:  "
        record = builder.hwp_record
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                   + record(TAG_PARA_TEXT, 1, builder.hwp_text("성명 ", (3, "%clk"), "\ua12b 서명", (4, "%clk")))
                   + record(TAG_CTRL_HEADER, 1, _hwp_field("%clk", command.replace(guide, "\ua12b 서명"))))

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "성명"
        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "성명"

    @staticmethod
    def _nested_hwpx(count: int, guide: str = "g") -> bytes:
        command = f"Clickhere:set:1:Direction:wstring:{len(guide)}:{guide} "
        begins = "".join(f'<hp:ctrl><hp:fieldBegin id="{index}" type="CLICK_HERE"><hp:parameters><hp:stringParam name="Command">'
                         f"{command}</hp:stringParam></hp:parameters></hp:fieldBegin></hp:ctrl><hp:t>x</hp:t>" for index in range(count))
        ends = "".join(f'<hp:ctrl><hp:fieldEnd beginIDRef="{index}"/></hp:ctrl>' for index in reversed(range(count)))
        return builder.hwpx_bytes(f"<hp:p><hp:run>{begins}{ends}</hp:run></hp:p>")

    @staticmethod
    def _nested_hwp(monkeypatch, count: int, guide: str = "g") -> bytes:
        command = f"Clickhere:set:1:Direction:wstring:{len(guide)}:{guide} "
        record = builder.hwp_record
        text = builder.hwp_text(*[piece for _ in range(count) for piece in ((3, "%clk"), "x")], *[(4, "%clk")] * count)
        section = (record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, text)
                   + record(TAG_CTRL_HEADER, 1, _hwp_field("%clk", command)) * count)
        return _fake_hwp(monkeypatch, sections=[section])

    def test_field_with_another_click_here_field_inside_it_keeps_its_text(self, monkeypatch):
        """Only the innermost of click-here fields opened inside each other is compared with its guide: the outer one holds the
        text of the inner one, which is not a guide of its own."""
        assert convert(self._nested_hwpx(2, "x"), "a.hwpx").text == "x"
        assert convert(self._nested_hwp(monkeypatch, 2, "x"), "a.hwp").text == "x"

    @pytest.mark.timing
    def test_fields_opened_inside_each_other_take_linear_time(self, monkeypatch):
        """Every field end joined the text from its start to compare it with the guide, so n fields opened in one paragraph and
        then ended took the square of n (40,000 fields: 19 s in HWP and HWPX alike). Four times the fields must take less than
        eight times as long."""
        for build, name in ((self._nested_hwpx, "a.hwpx"), (lambda count: self._nested_hwp(monkeypatch, count), "a.hwp")):
            small, large = build(2_500), build(10_000)
            assert convert(large, name).text == "x" * 10_000
            assert fastest(lambda: convert(large, name)) < 8 * fastest(lambda: convert(small, name))


class TestHyperlinks:
    """Hyperlinks of HWPX (`hp:fieldBegin type="HYPERLINK"`) and HWP (the `%hlk` field) were plain text. Under `keep_links` they
    are `[text](address)` through the shared rules, with the address from the field command (`http\\://a.example;1;0;0;`) and
    only http, https and mailto kept, as for DOCX, ODF and RTF."""

    BODY = ('<hp:p><hp:run><hp:t>See </hp:t>' + _hwpx_link(1, r"https\://example.com/a\;b?x=1;1;0;0;", "the site")
            + "<hp:t>, </hp:t>" + _hwpx_link(2, r"mailto\:team@example.com;1;0;0;", "mail us")
            + "<hp:t>, </hp:t>" + _hwpx_link(3, r"C\:\\docs\\plan.hwp;1;0;0;", "the plan")
            + "<hp:t> and </hp:t>" + _hwpx_link(4, r"ftp\://files.example.com;1;0;0;", "files")
            + "<hp:t>.</hp:t></hp:run></hp:p>")

    def test_hwpx_hyperlink_becomes_a_markdown_link(self):
        """A file, a bookmark or an ftp address keeps only its text; with `keep_links` off nothing changes."""
        assert convert(builder.hwpx_bytes(self.BODY), "a.hwpx").text == (
            "See [the site](https://example.com/a;b?x=1), [mail us](mailto:team@example.com), the plan and files.")
        assert convert(builder.hwpx_bytes(self.BODY), "a.hwpx", ConvertOptions(keep_links=False)).text == (
            "See the site, mail us, the plan and files.")

    def test_hwpx_link_in_a_heading_cell_and_note_and_across_a_table(self):
        """A heading is decided on its text without the address, as in DOCX: a long address does not make it body text. A link
        in a table cell or a note is kept; a link whose end is past a table in the same paragraph stays text."""
        address = "https\\://example.com/" + "a" * 150 + ";1;0;0;"
        def row(field_id: int) -> str:   # a link in the first cell, text in the second
            return "<hp:tr>" + "".join(
                f'<hp:tc><hp:subList><hp:p><hp:run>{inner}</hp:run></hp:p></hp:subList><hp:cellAddr colAddr="{column}" rowAddr="0"/>'
                '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
                for column, inner in enumerate((_hwpx_link(field_id, r"https\://cell.example;1;0;0;", "셀"), "<hp:t>값</hp:t>"))
            ) + "</hp:tr>"

        body = (f'<hp:p styleIDRef="2"><hp:run>{_hwpx_link(1, address, "사업 개요")}</hp:run></hp:p>'
                f'<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2">{row(2)}</hp:tbl></hp:run></hp:p>'
                '<hp:p><hp:run><hp:t>본문</hp:t>' + _hwpx_note(_hwpx_p(_hwpx_link(3, r"https\://note.example;1;0;0;", "출처")))
                + "</hp:run></hp:p>"
                '<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="4" type="HYPERLINK"><hp:parameters cnt="1"><hp:stringParam name="Command">'
                r"https\://far.example;1;0;0;</hp:stringParam></hp:parameters></hp:fieldBegin></hp:ctrl><hp:t>앞</hp:t>"
                f'<hp:tbl rowCnt="1" colCnt="2">{row(5)}</hp:tbl>'
                '<hp:t>뒤</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="4"/></hp:ctrl></hp:run></hp:p>')

        text = convert(builder.hwpx_bytes(body), "a.hwpx").text

        assert text.startswith(f"# [사업 개요](https://example.com/{'a' * 150})\n\n| [셀](https://cell.example) | 값 |")
        assert "본문[^1]" in text and text.endswith("[^1]: [출처](https://note.example)")
        assert "앞\n\n| [셀](https://cell.example) | 값 |\n| --- | --- |\n\n뒤" in text

    def test_hwpx_link_in_a_comment_stays_text(self):
        """Comment text is plain, as in DOCX; the link around commented body text is kept."""
        body = ('<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:subList><hp:p><hp:run>'
                + _hwpx_link(2, r"https\://memo.example;1;0;0;", "참고") + "</hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl>"
                + _hwpx_link(3, r"https\://body.example;1;0;0;", "본문") + '<hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>'
                "</hp:run></hp:p>")

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == '[본문](https://body.example)\n\n> Comment(on "본문"): 참고'

    def test_hwp_hyperlink_field_becomes_a_markdown_link(self, monkeypatch):
        """The command of the `%hlk` field control holds the address; a field end closes the last field opened, so the end of a
        click-here field around a link does not close the link, and a link to a file keeps its text only."""
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text(
                "See ", (3, "%hlk"), "the site", (4, "%hlk"), ", ", (3, "%clk"), "[", (3, "%hlk"), "form", (4, "%hlk"), "]",
                (4, "%clk"), " and ", (3, "%hlk"), "the plan", (4, "%hlk"), ".")),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%hlk", r"https\://example.com/a\;b;1;0;0;")),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%clk", "Clickhere:set:0:Direction:wstring:0:;")),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%hlk", r"https\://form.example;1;0;0;")),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%hlk", r"C\:\\plan.hwp;1;0;0;")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        assert convert(data, "a.hwp").text == "See [the site](https://example.com/a;b), [[form](https://form.example)] and the plan."
        assert convert(data, "a.hwp", ConvertOptions(keep_links=False)).text == "See the site, [form] and the plan."

    def test_hwp_control_text_cannot_forge_a_link(self, monkeypatch):
        """Ruby text is read from the control as it is: the link marks it may hold are dropped, so it cannot write a link to an
        address that was not vetted."""
        record = builder.hwp_record
        forged = "\x12x\x13javascript:alert(1)\x14"
        ruby = "tdut"[::-1].encode("latin-1") + b"".join(
            struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in (forged, "덧말")) + b"\x00" * 10
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text((3, "%hlk"), "링크", (4, "%hlk"), " ", (23, "tdut"))),
            record(TAG_CTRL_HEADER, 1, _hwp_field("%hlk", r"https\://example.com;1;0;0;")),
            record(TAG_CTRL_HEADER, 1, ruby),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        text = convert(data, "a.hwp").text

        assert text == "[링크](https://example.com) xjavascript:alert(1)(덧말)"

    def test_hwp_control_text_cannot_forge_strike_or_script_marks(self, monkeypatch):
        """The script and strike marks (U+0002-U+0006) in the text of a ruby or in the prefix and suffix of an auto number wrote
        `~~본말~~(^x)` and `~~3~~` around text that is neither struck nor raised. Text read from a control has no control
        characters."""
        record = builder.hwp_record
        ruby = "tdut"[::-1].encode("latin-1") + b"".join(
            struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in ("\x05본말\x06", "\x02x\x03")) + b"\x00" * 10
        number = "atno"[::-1].encode("latin-1") + struct.pack("<IHHHH", 4, 3, 0, 5, 6)  # kind 4: a table number
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("가 ", (23, "tdut"), " 나 ", (18, "atno"), " 끝")),
            record(TAG_CTRL_HEADER, 1, ruby),
            record(TAG_CTRL_HEADER, 1, number),
        ])

        assert convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text == "가 본말(x) 나 3 끝"


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

    def test_hwp_equation_script_cannot_forge_strike_or_script_marks(self, monkeypatch):
        """The script is read raw from the equation record, so the temporary strike and script marks (U+0002-U+0006) in it wrote
        `$~~a+b~~ c$` and `$^x$`. The control characters of a script are dropped; white space still separates its terms."""
        record = builder.hwp_record

        def converted(script: str) -> str:
            section = b"".join([
                record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
                record(TAG_PARA_TEXT, 1, builder.hwp_text("식 ", (11, "eqed"), " 끝")),
                record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("eqed")),
                record(TAG_EQEDIT, 2, struct.pack("<IH", 0, len(script)) + script.encode("utf-16-le")),
            ])
            return convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp").text

        assert converted("\x05a+b\x06 c") == "식 $a+b c$ 끝"
        assert converted("\x02x\x03") == "식 $x$ 끝"
        assert converted("a\nover\tb") == "식 $\\frac{a}{b}$ 끝"

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


def _hwp_header_footer(kind: str, text: str) -> bytes:
    """Header (`head`) or footer (`foot`) control of a top-level paragraph, with one paragraph of text."""
    return (builder.hwp_record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl(kind))
            + builder.hwp_record(TAG_LIST_HEADER, 2, b"\x01\x00" + b"\x00" * 6) + _hwp_paragraph(2, text))


def _hwpx_header_footer(kind: str, text: str) -> str:
    return f"<hp:ctrl><hp:{kind}><hp:subList>{_hwpx_p(f'<hp:t>{text}</hp:t>')}</hp:subList></hp:{kind}></hp:ctrl>"


class TestHeaderAndFooterPlacement:
    """Header and footer texts were gathered together and written before the body, so a page footer came out on top
    ('머리말\n\n꼬리말\n\n본문'). As in DOCX, ODT and RTF, headers go before the body and footers after it, then notes and
    comments."""

    _OPTIONS = ConvertOptions(include_headers_footers=True, max_chars=None)

    def _hwp(self, monkeypatch, body: tuple[str, ...] = ("본문 첫 문단", "본문 끝 문단")) -> bytes:
        section = (_hwp_paragraph(0, (16, "head"), (16, "foot"), body[0], (17, "fn  "))
                   + _hwp_header_footer("head", "머리말") + _hwp_header_footer("foot", "꼬리말")
                   + _hwp_note(0, _hwp_paragraph(2, "각주"))
                   + b"".join(_hwp_paragraph(0, text) for text in body[1:]))
        return _fake_hwp(monkeypatch, sections=[section])

    def _hwpx(self, body: tuple[str, ...] = ("본문 첫 문단", "본문 끝 문단")) -> str:
        memo = ('<hp:ctrl><hp:fieldBegin id="1" type="MEMO"><hp:subList>' + _hwpx_p("<hp:t>메모</hp:t>")
                + '</hp:subList></hp:fieldBegin></hp:ctrl><hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>')
        return (_hwpx_p(_hwpx_header_footer("header", "머리말") + _hwpx_header_footer("footer", "꼬리말")
                        + f"<hp:t>{body[0]}</hp:t>" + _hwpx_note(_hwpx_p("<hp:t>각주</hp:t>")) + memo)
                + "".join(_hwpx_p(f"<hp:t>{text}</hp:t>") for text in body[1:]))

    def test_hwp_footer_comes_after_the_body_and_before_the_notes(self, monkeypatch):
        text = convert(self._hwp(monkeypatch), "a.hwp", self._OPTIONS).text

        assert text == "머리말\n\n본문 첫 문단[^1]\n\n본문 끝 문단\n\n꼬리말\n\n[^1]: 각주"

    def test_hwpx_footer_comes_after_the_body_and_before_the_notes_and_comments(self):
        text = convert(builder.hwpx_bytes(self._hwpx()), "a.hwpx", self._OPTIONS).text

        assert text == '머리말\n\n본문 첫 문단[^1]\n\n본문 끝 문단\n\n꼬리말\n\n[^1]: 각주\n\n> Comment: 메모'

    def test_hwp_text_used_as_header_and_footer_is_written_in_both_places(self, monkeypatch):
        section = (_hwp_paragraph(0, (16, "head"), (16, "foot"), "본문")
                   + _hwp_header_footer("head", "회사") + _hwp_header_footer("foot", "회사"))

        text = convert(_fake_hwp(monkeypatch, sections=[section]), "a.hwp", self._OPTIONS).text

        assert text == "회사\n\n본문\n\n회사"

    def test_hwpx_text_used_as_header_and_footer_is_written_in_both_places(self):
        body = _hwpx_p(_hwpx_header_footer("header", "회사") + _hwpx_header_footer("footer", "회사") + "<hp:t>본문</hp:t>")

        assert convert(builder.hwpx_bytes(body), "a.hwpx", self._OPTIONS).text == "회사\n\n본문\n\n회사"

    @pytest.mark.parametrize("limit", [5, 12, 20, 30, 45])
    def test_hwp_cut_text_is_the_front_of_the_full_conversion(self, monkeypatch, limit):
        body = ("본문 첫 문단", *(f"본문 {index} 번째 문단" for index in range(10)))
        data = self._hwp(monkeypatch, body)
        full = convert(data, "a.hwp", self._OPTIONS).text

        result = convert(data, "a.hwp", ConvertOptions(include_headers_footers=True, max_chars=limit))

        assert result.exceeded and result.text == full[:limit].rstrip()

    @pytest.mark.parametrize("limit", [5, 12, 20, 30, 45])
    def test_hwpx_cut_text_is_the_front_of_the_full_conversion(self, limit):
        data = builder.hwpx_bytes(self._hwpx(("본문 첫 문단", *(f"본문 {index} 번째 문단" for index in range(10)))))
        full = convert(data, "a.hwpx", self._OPTIONS).text

        result = convert(data, "a.hwpx", ConvertOptions(include_headers_footers=True, max_chars=limit))

        assert result.exceeded and result.text == full[:limit].rstrip()


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

    def test_hwpx_comment_numbers_do_not_advance_the_body(self):
        memo = ('<hp:ctrl><hp:fieldBegin id="5" type="MEMO"><hp:subList>' + _hwpx_p("<hp:t>memo item</hp:t>", "1")
                + '</hp:subList></hp:fieldBegin></hp:ctrl><hp:ctrl><hp:fieldEnd beginIDRef="5"/></hp:ctrl>')
        body = _hwpx_p("<hp:t>one</hp:t>", "1") + _hwpx_p("<hp:t>two</hp:t>" + memo, "1") + _hwpx_p("<hp:t>three</hp:t>", "1")

        result = convert(self._hwpx(body), "a.hwpx")

        assert result.text == "1. one\n2. two\n3. three\n\n> Comment: 1. memo item"

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
