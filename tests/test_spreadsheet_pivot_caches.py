"""The pivot caches of a workbook are not read: the conversion uses nothing of them, and one element of a cache that openpyxl
cannot read rejected the whole workbook as corrupted."""

from io import BytesIO

import openpyxl
import pytest

from opendockit import convert
from tests import builder
from tests.test_office_round44 import _workbook


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# A field grouped by hand ("apples and pears are fruit"), as Excel writes it
_GROUPED_FIELD = ('<cacheField name="이름" numFmtId="0"><sharedItems count="2"><s v="사과"/><s v="배"/></sharedItems>'
                  '<fieldGroup base="0"><discretePr count="2"><x v="0"/><x v="0"/></discretePr>'
                  '<groupItems count="1"><s v="과일"/></groupItems></fieldGroup></cacheField>')


def _with_pivot_cache(data: bytes, fields: str = _GROUPED_FIELD) -> bytes:
    definition = (f'<pivotCacheDefinition xmlns="{_MAIN}" xmlns:r="{_RELATIONSHIPS}" refreshedBy="kim" refreshedDate="45000" '
                  f'createdVersion="8" refreshedVersion="8" recordCount="2"><cacheSource type="worksheet">'
                  f'<worksheetSource ref="A1:B3" sheet="Sheet"/></cacheSource><cacheFields count="1">{fields}</cacheFields>'
                  "</pivotCacheDefinition>")
    relationship = (f'<Relationship Id="rIdPivot" Target="pivotCache/pivotCacheDefinition1.xml" '
                    f'Type="{_RELATIONSHIPS}/pivotCacheDefinition"/>')
    override = ('<Override PartName="/xl/pivotCache/pivotCacheDefinition1.xml" ContentType="application/vnd.openxmlformats-'
                'officedocument.spreadsheetml.pivotCacheDefinition+xml"/></Types>')
    return builder.zip_with(data, edit={
        "xl/_rels/workbook.xml.rels": lambda xml: xml.replace("</Relationships>", f"{relationship}</Relationships>"),
        "xl/workbook.xml": lambda xml: xml.replace(
            "</workbook>", f'<pivotCaches><pivotCache xmlns:r="{_RELATIONSHIPS}" cacheId="7" r:id="rIdPivot"/></pivotCaches></workbook>'),
        "[Content_Types].xml": lambda xml: xml.replace("</Types>", override),
    }, add={"xl/pivotCache/pivotCacheDefinition1.xml": definition})


class TestPivotCachesAreNotRead:
    def test_a_workbook_with_a_field_grouped_by_hand_is_converted(self):
        """openpyxl 3.1.5 raises on `discretePr` in a pivot cache; the workbook was refused as corrupted though every cell
        of it could be read (a workbook among the test files of calamine)."""
        result = convert(_with_pivot_cache(_workbook(merged=True)), "a.xlsx")

        assert "| 사과 | 3 |" in result.text and "| 배 | 5 |" in result.text
        assert "| 합계 | 8 |" in result.text and "| 합계 | 9 |" in result.text  # read in full: the merged cell fills its rows
        assert result.warnings == []

    @pytest.mark.parametrize("definition", ["<broken", "<pivotCacheDefinition/>"], ids=["not-xml", "no-fields"])
    def test_a_broken_pivot_cache_does_not_reject_the_workbook(self, definition):
        data = builder.zip_with(_with_pivot_cache(_workbook()), {"xl/pivotCache/pivotCacheDefinition1.xml": lambda xml: definition})

        assert "| 사과 | 3 |" in convert(data, "a.xlsx").text

    def test_openpyxl_reads_pivot_caches_as_before_outside_a_conversion(self, monkeypatch):
        """The change holds only while this package is reading: a program that uses openpyxl beside it gets its caches."""
        from openpyxl.pivot.cache import CacheDefinition

        read = []
        from_tree = CacheDefinition.from_tree.__func__
        monkeypatch.setattr(CacheDefinition, "from_tree", classmethod(lambda cls, node: read.append(1) or from_tree(cls, node)))
        plain = _with_pivot_cache(_workbook(), '<cacheField name="이름" numFmtId="0"><sharedItems/></cacheField>')

        convert(plain, "a.xlsx")
        assert read == []

        workbook = openpyxl.load_workbook(BytesIO(plain))
        assert workbook.active["A2"].value == "사과" and read
