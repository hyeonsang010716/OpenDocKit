"""Hangul headers written without `include_headers_footers` (`_hangul.header_by_default`). Hangul forms and reports put the form
number and the document title in the header (`[별지 제3호 서식] … 평가표 및 출석부`, `시험합의서` and its number); in 9 of 34
real HWP / HWPX files that text was nowhere else, and it was dropped without a warning."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors.hwp import TAG_SHAPE_OLE
from tests import builder
from tests.test_hwp import _fake_hwp, _hwp_header_footer, _hwp_paragraph, _hwpx_header_footer, _hwpx_p


_HC = 'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"'


def _hwpx(*parts: str, body: str = "<hp:t>본문</hp:t>") -> bytes:
    return builder.hwpx_bytes(_hwpx_p("".join(parts) + body))


def _hwpx_logo_header(text: str) -> str:
    picture = f'<hp:pic><hc:img {_HC} binaryItemIDRef="image1"/></hp:pic>'
    return f"<hp:ctrl><hp:header><hp:subList>{_hwpx_p(picture + f'<hp:t>{text}</hp:t>')}</hp:subList></hp:header></hp:ctrl>"


class TestHeadersByDefault:
    def test_hwpx_header_with_words_is_written_before_the_body(self):
        data = _hwpx(_hwpx_header_footer("header", "[별지 제3호 서식] 평가표"), _hwpx_header_footer("footer", "꼬리말"))

        assert convert(data, "a.hwpx").text == "[별지 제3호 서식] 평가표\n\n본문"

    def test_hwp_header_with_words_is_written_before_the_body(self):
        section = (_hwp_paragraph(0, (16, "head"), (16, "foot"), "본문")
                   + _hwp_header_footer("head", "시험합의서") + _hwp_header_footer("foot", "꼬리말"))

        assert convert(_fake_hwp(None, sections=[section]), "a.hwp").text == "시험합의서\n\n본문"

    def test_header_without_a_word_is_left_out(self):
        assert convert(_hwpx(_hwpx_header_footer("header", "- 3 -")), "a.hwpx").text == "본문"

    def test_page_number_with_a_page_word_is_left_out(self):
        """A page number field is written as nothing, so `Page` + field came out as `Page`, and `- ` field ` 쪽 -` as `- 쪽 -`."""
        field = '<hp:ctrl><hp:autoNum num="1" numType="PAGE"/></hp:ctrl>'
        for header in (f"<hp:t>Page </hp:t>{field}", f"<hp:t>- </hp:t>{field}<hp:t> 쪽 -</hp:t>", "<hp:t>- iv -</hp:t>",
                       "<hp:t>p. 2 of 9</hp:t>", "<hp:t>3 페이지</hp:t>", "<hp:t>- IV -</hp:t>", "<hp:t>(ii)</hp:t>",
                       f"<hp:t>Page </hp:t>{field}<hp:t> of </hp:t>{field}", f"{field}<hp:t> of </hp:t>{field}",
                       "<hp:t>xii</hp:t>", f"<hp:t>제 </hp:t>{field}<hp:t> 쪽</hp:t>", "<hp:t>총 9쪽 중 3쪽</hp:t>"):
            control = f"<hp:ctrl><hp:header><hp:subList>{_hwpx_p(header)}</hp:subList></hp:header></hp:ctrl>"
            assert convert(_hwpx(control), "a.hwpx").text == "본문", header

    @pytest.mark.parametrize("words", ["Civil Mix", "MIX", "CV", "DC", "MD", "XL", "I", "No", "No. 1", "II 단계", "Vol. II"])
    def test_header_of_words_that_look_like_page_words_is_written(self, words):
        """Only page words, or one Roman numeral set between dashes or brackets as front-matter pages are, make a page number:
        a header that spells a Roman numeral (`MIX`, `CV`) or reads `No. 1` was dropped as one."""
        assert convert(_hwpx(_hwpx_header_footer("header", words)), "a.hwpx").text == f"{words}\n\n본문"

    def test_object_of_a_header_not_written_does_not_warn(self):
        """The OLE object of a header that is not written, or written by its words only, is not written: it warned that its
        contents could not be converted."""
        for words in ("- 1 -", "제목"):
            ole = f'<hp:ole binaryItemIDRef="x"/><hp:t>{words}</hp:t>'
            control = f"<hp:ctrl><hp:header><hp:subList>{_hwpx_p(ole)}</hp:subList></hp:header></hp:ctrl>"
            assert convert(_hwpx(control), "a.hwpx").warnings == []
            assert convert(_hwpx(control), "a.hwpx", ConvertOptions(include_headers_footers=True)).warnings == [
                "The contents of 1 OLE object(s) (pasted tables, charts, etc.) could not be converted."]

        record = builder.hwp_record
        header = (record(0x10 + 55, 1, builder.hwp_ctrl("head")) + record(0x10 + 56, 2, b"\x01\x00" + b"\x00" * 6)
                  + _hwp_paragraph(2, (11, "gso "), "제목") + record(0x10 + 55, 3, builder.hwp_ctrl("gso "))
                  + record(TAG_SHAPE_OLE, 4, b"\x00" * 24))
        data = _fake_hwp(None, sections=[_hwp_paragraph(0, (16, "head"), "본문") + header])

        assert convert(data, "a.hwp").warnings == []
        assert convert(data, "a.hwp", ConvertOptions(include_headers_footers=True)).warnings == [
            "The contents of 1 OLE object(s) (pasted tables, charts, etc.) could not be converted."]

    def test_header_written_by_default_is_no_heading(self):
        """A header in a 개요 style was written as `# 시험합의서`, the first heading of every such document (an outline, the
        heading metadata of a chunk). With the option on it is written as before."""
        control = '<hp:ctrl><hp:header><hp:subList><hp:p styleIDRef="2"><hp:run><hp:t>시험합의서</hp:t></hp:run></hp:p></hp:subList></hp:header></hp:ctrl>'

        assert convert(_hwpx(control), "a.hwpx").text == "시험합의서\n\n본문"
        assert convert(_hwpx(control), "a.hwpx", ConvertOptions(include_headers_footers=True)).text == "# 시험합의서\n\n본문"

        from tests.test_hwp import _para_header
        record = builder.hwp_record
        header = (record(0x10 + 55, 1, builder.hwp_ctrl("head")) + record(0x10 + 56, 2, b"\x01\x00" + b"\x00" * 6)
                  + record(0x10 + 50, 2, _para_header(0, 2)) + record(0x10 + 51, 3, builder.hwp_text("시험합의서")))
        data = _fake_hwp(None, sections=[_hwp_paragraph(0, (16, "head"), "본문") + header])

        assert convert(data, "a.hwp").text == "시험합의서\n\n본문"
        assert convert(data, "a.hwp", ConvertOptions(include_headers_footers=True)).text == "# 시험합의서\n\n본문"

    def test_header_is_written_once(self):
        header = _hwpx_header_footer("header", "보고서 제목")
        data = builder.hwpx_bytes(_hwpx_p(header + "<hp:t>첫 쪽</hp:t>") + _hwpx_p(header + "<hp:t>둘째 쪽</hp:t>"))

        assert convert(data, "a.hwpx").text == "보고서 제목\n\n첫 쪽\n\n둘째 쪽"

    def test_notes_of_a_header_left_out_are_left_out_too(self):
        note = "<hp:ctrl><hp:footNote><hp:subList>" + _hwpx_p("<hp:t>각주 내용</hp:t>") + "</hp:subList></hp:footNote></hp:ctrl>"
        header = f"<hp:ctrl><hp:header><hp:subList>{_hwpx_p('<hp:t>- 1 -</hp:t>' + note)}</hp:subList></hp:header></hp:ctrl>"
        body_note = note.replace("각주 내용", "본문 각주")

        assert convert(_hwpx(header, body="<hp:t>본문</hp:t>" + body_note), "a.hwpx").text == "본문[^1]\n\n[^1]: 본문 각주"

    def test_notes_of_a_hwp_header_left_out_are_left_out_too(self):
        from tests.test_hwp import _hwp_note
        # the header's paragraph holds a note
        header_with_note = (builder.hwp_record(0x10 + 55, 1, builder.hwp_ctrl("head"))
                            + builder.hwp_record(0x10 + 56, 2, b"\x01\x00" + b"\x00" * 6)
                            + _hwp_paragraph(2, "- 1 -", (17, "fn  ")) + _hwp_note(2, _hwp_paragraph(4, "각주 내용")))
        section = _hwp_paragraph(0, (16, "head"), "본문") + header_with_note

        assert convert(_fake_hwp(None, sections=[section]), "a.hwp").text == "본문"

    def test_picture_in_a_header_is_not_written_or_kept(self):
        """A logo in the header was never written by default; kept, it would be described in every document."""
        data = builder.hwpx_with_picture(_hwpx_p(_hwpx_logo_header("회사 이름") + "<hp:t>본문</hp:t>"))

        result = convert(data, "a.hwpx")

        assert result.text == "회사 이름\n\n본문"
        assert result.images == []

    def test_header_of_only_a_picture_is_left_out(self):
        data = builder.hwpx_with_picture(_hwpx_p(_hwpx_logo_header("") + "<hp:t>본문</hp:t>"))

        result = convert(data, "a.hwpx")

        assert result.text == "본문"
        assert result.images == []

    def test_picture_in_the_body_after_a_dropped_header_keeps_its_place(self):
        body = _hwpx_logo_header("") + f'<hp:t>본문</hp:t><hp:pic><hc:img {_HC} binaryItemIDRef="image1"/></hp:pic>'
        data = builder.hwpx_with_picture(_hwpx_p(body))

        result = convert(data, "a.hwpx", ConvertOptions(min_image_side=1))

        assert len(result.images) == 1
        assert result.text.startswith("본문")

    def test_with_the_option_headers_footers_and_pictures_are_written_as_before(self):
        data = builder.hwpx_with_picture(_hwpx_p(_hwpx_logo_header("회사 이름") + _hwpx_header_footer("footer", "꼬리말")
                                                 + "<hp:t>본문</hp:t>"))

        result = convert(data, "a.hwpx", ConvertOptions(include_headers_footers=True, min_image_side=1))

        assert result.text.endswith("본문\n\n꼬리말")
        assert len(result.images) == 1
