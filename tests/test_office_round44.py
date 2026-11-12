"""Office fixes of review round 44: one sheet part not used in conversion got the whole XLSX rejected; a broken comment part was
not reported."""

from io import BytesIO
import zipfile

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.worksheet.table import Table
import pytest

from opendockit import convert
from tests import builder


_SHEET = "xl/worksheets/sheet1.xml"
_PIVOT_TABLE = (
    '<pivotTableDefinition xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="P" cacheId="99" '
    'dataCaption="Values"><location ref="D1:E3" firstHeaderRow="1" firstDataRow="1" firstDataCol="1"/></pivotTableDefinition>'
)


def _workbook(*, table: bool = False, comment: bool = False, merged: bool = False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in (["이름", "값"], ["사과", 3], ["배", 5]):
        sheet.append(row)
    if table:
        sheet.add_table(Table(displayName="T1", ref="A1:B3"))
    if comment:
        sheet["B2"].comment = Comment("검토 필요", "kim")
    if merged:
        sheet["A5"] = "합계"
        sheet.merge_cells("A5:A6")
        sheet["B5"], sheet["B6"] = 8, 9
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _after_cells(fragment: str) -> dict:
    return {_SHEET: lambda xml: xml.replace("</sheetData>", "</sheetData>" + fragment, 1)}


def _before_cells(fragment: str) -> dict:
    return {_SHEET: lambda xml: xml.replace("<sheetData", fragment + "<sheetData", 1)}


def _malformed_part(prefix: str):
    def edit(data: bytes) -> bytes:
        return builder.zip_with(data, {name: (lambda xml: "<broken") for name in _names(data) if name.startswith(prefix)})

    return edit


def _names(data: bytes) -> list[str]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return archive.namelist()


def _pivot_without_cache(data: bytes) -> bytes:
    types = "[Content_Types].xml"
    override = ('<Override PartName="/xl/pivotTables/pivotTable1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.pivotTable+xml"/></Types>')
    relations = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/pivotTable" '
                 'Target="../pivotTables/pivotTable1.xml"/></Relationships>')
    return builder.zip_with(data, {types: lambda xml: xml.replace("</Types>", override)},
                            {"xl/worksheets/_rels/sheet1.xml.rels": relations, "xl/pivotTables/pivotTable1.xml": _PIVOT_TABLE})


# Out-of-spec values in parts not used in conversion. Excel ignores or repairs these values when opening
_UNUSED_PARTS = {
    "data-validation-type": lambda: builder.zip_with(_workbook(), _after_cells(
        '<dataValidations count="1"><dataValidation type="foo" sqref="A1"/></dataValidations>')),
    "data-validation-range": lambda: builder.zip_with(_workbook(), _after_cells(
        '<dataValidations count="1"><dataValidation type="list" sqref="??"><formula1>"a,b"</formula1></dataValidation>'
        '</dataValidations>')),
    "conditional-format-type": lambda: builder.zip_with(_workbook(), _after_cells(
        '<conditionalFormatting sqref="A1"><cfRule type="bogus" priority="1"/></conditionalFormatting>')),
    "autofilter-column": lambda: builder.zip_with(_workbook(), _after_cells(
        '<autoFilter ref="A1:B2"><filterColumn colId="x"/></autoFilter>')),
    "empty-hyperlink-range": lambda: builder.zip_with(_workbook(), _after_cells(
        '<hyperlinks><hyperlink ref="" display="x"/></hyperlinks>')),
    "tab-color": lambda: builder.zip_with(_workbook(), _before_cells('<sheetPr><tabColor rgb="red"/></sheetPr>')),
    "page-breaks": lambda: builder.zip_with(_workbook(), _after_cells('<rowBreaks count="1"><brk id="x"/></rowBreaks>')),
    "table-definition-attribute": lambda: builder.zip_with(_workbook(table=True), {
        "xl/tables/table1.xml": lambda xml: xml.replace('ref="A1:B3"', 'ref="A1:B3" totalsRowCount="x"', 1)}),
    "table-definition-xml": lambda: _malformed_part("xl/tables/")(_workbook(table=True)),
    "pivot-table-without-cache": lambda: _pivot_without_cache(_workbook()),
}


class TestSheetPartsTheConverterDoesNotUse:
    """Table definitions, pivot tables, data validation, conditional formatting, auto filters, hyperlinks, tab colours and page
    breaks are not used in the conversion result. When one such value was out of spec, openpyxl rejected the whole document
    (streaming read the same file). Nothing is lost, so there is no warning."""

    @pytest.mark.parametrize("streaming", [False, True], ids=["full-load", "streamed"])
    @pytest.mark.parametrize("case", list(_UNUSED_PARTS))
    def test_malformed_unused_part_does_not_reject_the_workbook(self, case, streaming, monkeypatch):
        data = _UNUSED_PARTS[case]()
        if streaming:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert "| 배 | 5 |" in result.text
        assert result.warnings == []

    def test_merged_cells_are_still_filled_next_to_a_malformed_unused_part(self):
        data = builder.zip_with(_workbook(merged=True), _after_cells(
            '<dataValidations count="1"><dataValidation type="foo" sqref="A1"/></dataValidations>'))

        text = convert(data, "a.xlsx").text

        assert "| 합계 | 8 |" in text
        assert "| 합계 | 9 |" in text


class TestBrokenCommentPart:
    """A broken comment part skips only the comments of that part, with a warning. In full load openpyxl parsed the comment XML
    first and rejected the whole document; streaming dropped the comments without a warning."""

    @pytest.mark.parametrize("streaming", [False, True], ids=["full-load", "streamed"])
    def test_malformed_comment_part_is_skipped_with_a_warning(self, streaming, monkeypatch):
        data = _malformed_part("xl/comments")(_workbook(comment=True))
        if streaming:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert "검토 필요" not in result.text
        assert result.warnings == ["The cell comment part of sheet 'Sheet' is corrupted, so its comments could not be read."]

    def test_intact_comment_part_gives_no_warning(self):
        data = _workbook(comment=True)

        result = convert(data, "a.xlsx")

        assert "검토 필요" in result.text
        assert result.warnings == []
