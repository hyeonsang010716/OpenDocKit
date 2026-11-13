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
