"""Audited defects of the Hangul extractors (HWPX, HWP, equation script): a part or section referenced many times, odd values
that failed or slowed down the whole document.

HWP record streams are built with the fake OLE container (`_fake_hwp`) of `test_hwp.py`.
"""

import time
import zlib

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import hwpx
from opendockit.extractors._equation import equation_to_latex, render_equation
from tests import builder
from tests.test_hwp import (
    _CHART_XML,
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    _auto_number,
    _fake_hwp,
)


_CHART_REFERENCE = ('<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/ooxmlchart">'
                    '<hp:chart chartIDRef="Chart/chart1.xml"/></hp:case></hp:switch>')
_CHART_TEXT = "[Chart] 판매\n\n| Category | 판매량 |\n| --- | --- |\n| 1월 | 5 |\n| 2월 | 7 |"


class TestHwpxChartReferencedManyTimes:
    """Every hp:chart that pointed at the same chart part read, parsed and rendered it again: a 7.8 KB document whose chart part
    was 300,000 empty elements took 47 s with 100 references in one paragraph."""

    def test_chart_part_is_rendered_once_and_later_references_give_its_title(self, monkeypatch):
        rendered = []
        render_chart = hwpx.render_chart
        monkeypatch.setattr(hwpx, "render_chart", lambda *args: rendered.append(args[0]) or render_chart(*args))
        body = (f"<hp:p><hp:run><hp:t>판매 현황:</hp:t>{_CHART_REFERENCE * 3}</hp:run></hp:p>"
                f"<hp:p><hp:run>{_CHART_REFERENCE}</hp:run></hp:p>")
        data = builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": _CHART_XML})

        result = convert(data, "a.hwpx")

        assert len(rendered) == 1
        assert result.text == f"판매 현황:\n\n{_CHART_TEXT}\n\n[Chart] 판매\n\n[Chart] 판매\n\n[Chart] 판매"

    def test_chart_part_that_cannot_be_read_is_parsed_once(self, monkeypatch):
        parsed = []
        parse_part = hwpx.parse_part
        monkeypatch.setattr(hwpx, "parse_part", lambda data: parsed.append(data) or parse_part(data))
        body = f"<hp:p><hp:run><hp:t>판매 현황:</hp:t>{_CHART_REFERENCE * 50}</hp:run></hp:p>"
        data = builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": "<c:chartSpace><broken"})

        result = convert(data, "a.hwpx")

        assert result.text == "판매 현황:"
        assert parsed.count(b"<c:chartSpace><broken") == 1


def _paragraph_xml(text: str) -> str:
    return f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>"


def _section_xml(text: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
            f'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">{_paragraph_xml(text)}</hs:sec>')


def _with_second_section(spine: str) -> bytes:
    """A document of two sections ('첫 구역', '둘째 구역') whose spine is the usual one entry for the first, then `spine`."""
    def edit(xml: str) -> str:
        xml = xml.replace("</opf:manifest>", '<opf:item id="section1" href="Contents/section1.xml"/></opf:manifest>')
        return xml.replace("</opf:spine>", spine + "</opf:spine>")

    return builder.zip_with(builder.hwpx_bytes(_paragraph_xml("첫 구역")), edit={"Contents/content.hpf": edit},
                            add={"Contents/section1.xml": _section_xml("둘째 구역")})


_FIRST, _SECOND = '<opf:itemref idref="section0"/>', '<opf:itemref idref="section1"/>'


class TestHwpxSpine:
    """The spine of content.hpf decides what is read, so it must not decide how much work is done: one section of 300,000 empty
    paragraphs listed 40 times (a 33 KB file) was parsed and rendered 40 times, 29 s."""

    def test_section_listed_again_in_the_spine_is_read_once(self, monkeypatch):
        parsed = []
        parse_xml = hwpx.parse_xml
        monkeypatch.setattr(hwpx, "parse_xml", lambda data: parsed.append(data) or parse_xml(data))

        result = convert(_with_second_section(_FIRST * 40 + _SECOND + _FIRST), "a.hwpx")

        assert len(parsed) == 2
        assert result.text == "첫 구역\n\n둘째 구역"
        assert result.warnings == []

    def test_spine_longer_than_the_cap_is_cut_with_a_warning(self):
        result = convert(_with_second_section(_FIRST * (hwpx._MAX_SPINE_ITEMS - 1) + _SECOND), "a.hwpx")

        assert result.text == "첫 구역"
        assert result.warnings == ["The HWPX document lists more than 5,000 sections; the rest were not read."]

    def test_spine_at_the_cap_is_read_to_its_end(self):
        result = convert(_with_second_section(_FIRST * (hwpx._MAX_SPINE_ITEMS - 2) + _SECOND), "a.hwpx")

        assert result.text == "첫 구역\n\n둘째 구역"
        assert result.warnings == []


class TestHwpAutoNumberDecoration:
    """The prefix and suffix characters of an auto number (atno) are raw UINT16 values. Half of a surrogate pair went into the
    body as it was, and the result could not be encoded as UTF-8."""

    @pytest.mark.parametrize("prefix, suffix", [("\ud800", "\udfff"), ("\udbff", "\x00"), ("\x00", "\udc00")])
    def test_lone_surrogate_decoration_is_dropped(self, monkeypatch, prefix, suffix):
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("표 ", (18, "atno"), " 제목")),
            record(TAG_CTRL_HEADER, 1, _auto_number(4, 1, prefix=prefix, suffix=suffix)),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        result = convert(data, "a.hwp")

        assert result.text == "표 1 제목"
        assert result.text.encode("utf-8").decode("utf-8") == result.text

    def test_ordinary_decoration_around_the_surrogate_range_is_kept(self, monkeypatch):
        record = builder.hwp_record
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("식 ", (18, "atno"))),
            record(TAG_CTRL_HEADER, 1, _auto_number(5, 2, prefix="힣", suffix="）")),
        ])
        data = _fake_hwp(monkeypatch, sections=[section])

        assert convert(data, "a.hwp").text == "식 힣2）"


class TestHugeHwpxSectionNumber:
    """The number in a section file name went through `int()` before anything else: one extra member named
    `Contents/section` + 5,000 digits + `.xml` rejected a document whose spine never mentions it."""

    _JUNK = f"Contents/section{'9' * 5000}.xml"

    def test_member_with_a_huge_section_number_outside_the_spine_is_ignored(self):
        data = builder.zip_with(builder.hwpx_bytes(), add={self._JUNK: _section_xml("목록에 없는 구역")})

        result = convert(data, "a.hwpx")

        assert result.text == convert(builder.hwpx_bytes(), "a.hwpx").text
        assert result.warnings == []

    def test_without_a_spine_it_is_read_after_the_numbered_sections(self):
        data = builder.zip_with(builder.hwpx_bytes(_paragraph_xml("첫 구역")), edit={"Contents/content.hpf": lambda xml: xml[:-20]},
                                add={self._JUNK: _section_xml("끝 구역"), "Contents/section7.xml": _section_xml("일곱째 구역")})

        result = convert(data, "a.hwpx")

        assert result.text == "첫 구역\n\n일곱째 구역\n\n끝 구역"


class TestEquationWithARepeatedScript:
    """A second superscript (or subscript) on the same term came out as `x^{2}^{3}`, which LaTeX rejects (double superscript).
    What Hancom draws for it is not in the spec, so the original script is kept like any other script that cannot be converted."""

    @pytest.mark.parametrize("script", [
        "x^2^3", "x_1_2", "x ^2 ^3", "x sup 2 sup 3", "x^2_1^3", "a over b^2^3", "e^{x^2^3}", "{x^2}^3^4",
        "sum from 1 to n from 2",
    ])
    def test_repeated_script_keeps_the_original_script(self, script):
        assert equation_to_latex(script) is None
        assert render_equation(script) == f" [Equation: {script}] "

    @pytest.mark.parametrize("script, latex", [
        ("x_1^2", "x_{1}^{2}"),
        ("x^2_1", "x^{2}_{1}"),
        ("{x^2}^3", "{x^{2}}^{3}"),
        ("x^{2^3}", "x^{2^{3}}"),
        ("x^2 y^3", "x^{2} y^{3}"),
        ("x^2 ~^3", "x^{2} {}^{3}"),
        ("lsup 2 lsup 3 x", "{}^{3}{}^{2} x"),
        ('"a_b_c"^2', r"\text{a\_b\_c}^{2}"),
    ])
    def test_scripts_on_different_terms_or_of_different_kinds_are_converted(self, script, latex):
        assert equation_to_latex(script) == latex

    def test_hwpx_equation_with_a_repeated_script_is_kept_as_written(self):
        body = "<hp:p><hp:run><hp:t>식:</hp:t><hp:equation><hp:script>x^2^3</hp:script></hp:equation></hp:run></hp:p>"

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "식: [Equation: x^2^3]"


class TestHwpSectionWarningNumber:
    """The number in a `Section N:` warning was read from the stream name, so up to 24 digits chosen by the document reached
    the warning. It is the position of the section in the body, as for HWPX."""

    _LAST = "BodyText/Section" + "9" * 24

    def test_broken_section_is_numbered_by_its_position_not_by_its_stream_name(self, monkeypatch):
        section = builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) \
            + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text("첫 구역"))
        data = _fake_hwp(monkeypatch, flags=1, sections=[section], extra={self._LAST: b"\xff\xff\xff garbage" * 10})

        result = convert(data, "a.hwp")

        assert result.text == "첫 구역"
        assert result.warnings == ["Section 2: the compressed data is broken and could not be read."]

    def test_truncated_section_is_numbered_by_its_position(self, monkeypatch):
        record = builder.hwp_record
        section = record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text("첫 구역"))
        long = b"".join(record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                        + record(TAG_PARA_TEXT, 1, builder.hwp_text(f"문단 {index}")) for index in range(50))
        data = _fake_hwp(monkeypatch, flags=1, sections=[section], extra={self._LAST: zlib.compress(long)[2:-4][:60]})

        result = convert(data, "a.hwp")

        assert result.text.startswith("첫 구역\n\n문단 0")
        assert result.warnings == ["Section 2: the stream is truncated, so only the part that could be read was converted."]


def _hwp_headers(texts: list[str]) -> bytes:
    """One paragraph per text, each holding a header control (`head`) with that text."""
    record = builder.hwp_record
    return b"".join(
        record(TAG_PARA_HEADER, 0, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 1, builder.hwp_text((16, "head")))
        + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("head")) + record(TAG_LIST_HEADER, 2, b"\x00" * 8)
        + record(TAG_PARA_HEADER, 2, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 3, builder.hwp_text(text))
        for text in texts)


def _hwpx_headers(texts: list[str]) -> str:
    return "".join(f"<hp:p><hp:run><hp:ctrl><hp:header><hp:subList>{_paragraph_xml(text)}</hp:subList></hp:header></hp:ctrl>"
                   "</hp:run></hp:p>" for text in texts)


class TestManyDistinctHeaders:
    """Each header or footer text is written once. Whether a text had been seen was looked up in a list, so the time was
    quadratic in the number of different texts: 60,000 took 11 s (HWP and HWPX alike)."""

    _OPTIONS = ConvertOptions(include_headers_footers=True, max_chars=None)
    _TEXTS = ["머리말 나", "머리말 가", "머리말 나", "머리말 다", "머리말 가"]

    def test_hwp_header_texts_are_written_once_in_order_of_first_appearance(self, monkeypatch):
        data = _fake_hwp(monkeypatch, sections=[_hwp_headers(self._TEXTS)])

        assert convert(data, "a.hwp", self._OPTIONS).text == "머리말 나\n\n머리말 가\n\n머리말 다"

    def test_hwpx_header_texts_are_written_once_in_order_of_first_appearance(self):
        data = builder.hwpx_bytes(_hwpx_headers(self._TEXTS))

        assert convert(data, "a.hwpx", self._OPTIONS).text == "머리말 나\n\n머리말 가\n\n머리말 다"

    @pytest.mark.timing
    def test_hwp_headers_take_linear_time(self, monkeypatch):
        data = _fake_hwp(monkeypatch, sections=[_hwp_headers([f"머리말 {index:08d}" for index in range(60_000)])])

        started = time.perf_counter()
        result = convert(data, "a.hwp", self._OPTIONS)

        assert time.perf_counter() - started < 5.0
        assert result.text.count("머리말") == 60_000

    @pytest.mark.timing
    def test_hwpx_headers_take_linear_time(self):
        data = builder.hwpx_bytes(_hwpx_headers([f"머리말 {index:08d}" for index in range(60_000)]))

        started = time.perf_counter()
        result = convert(data, "a.hwpx", self._OPTIONS)

        assert time.perf_counter() - started < 5.0
        assert result.text.count("머리말") == 60_000
