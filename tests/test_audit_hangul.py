"""Audited defects of the Hangul extractors (HWPX, HWP, equation script): a part or section referenced many times, odd values
that failed or slowed down the whole document.

HWP record streams are built with the fake OLE container (`_fake_hwp`) of `test_hwp.py`.
"""

from opendockit import convert
from opendockit.extractors import hwpx
from tests import builder
from tests.test_hwp import _CHART_XML


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
