"""Office fixes of review round 45: one out-of-spec value in the XLSX document properties, merge ranges, chart sheets or workbook
list got the whole document rejected; slide-number placeholder text of PPTX saved by LibreOffice; Hangul/ideograph numbers past
ten."""

from io import BytesIO
import re
import zipfile

from lxml import etree
from openpyxl import Workbook
from openpyxl.packaging.custom import IntProperty
from pptx import Presentation
from pptx.util import Inches
import pytest

from opendockit import convert
from opendockit.extractors._shared import DOCX_NUMBER_SHAPES, format_number
from tests import builder


_SHEET = "xl/worksheets/sheet1.xml"
_WORKBOOK = "xl/workbook.xml"
_CORE = "docProps/core.xml"
_CUSTOM = "docProps/custom.xml"


def _workbook(*, title: str | None = None, custom: bool = False, merged: bool = False, chart_sheet: bool = False,
              second_sheet: bool = False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in (["이름", "값"], ["사과", 3], ["배", 5]):
        sheet.append(row)
    if merged:
        sheet["A5"] = "합계"
        sheet.merge_cells("A5:A6")
        sheet["B5"], sheet["B6"] = 8, 9
    if title:
        workbook.properties.title = title
    if custom:
        workbook.custom_doc_props.append(IntProperty(name="쪽수", value=3))
    if chart_sheet:
        workbook.create_chartsheet("차트")
    if second_sheet:
        workbook.create_sheet("둘째")["A1"] = "둘째 시트 글"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _created(value: str):
    return {_CORE: lambda xml: re.sub(r"(<dcterms:created[^>]*>)[^<]*<", rf"\g<1>{value}<", xml, count=1)}


# Values in the document property parts (docProps) that are out of spec, or in spec but unreadable for openpyxl. Only the title is
# used in the conversion result
_PROPERTY_PARTS = {
    "created-date-only": lambda: builder.zip_with(_workbook(), _created("2024-01-05")),
    "created-year-only": lambda: builder.zip_with(_workbook(), _created("2024")),
    "created-space-separated": lambda: builder.zip_with(_workbook(), _created("2024-01-05 10:00:00")),
    "last-printed-zero-filled": lambda: builder.zip_with(_workbook(), {_CORE: lambda xml: xml.replace(
        "</cp:coreProperties>", "<cp:lastPrinted>0000-00-00T00:00:00Z</cp:lastPrinted></cp:coreProperties>")}),
    "broken-core-properties-xml": lambda: builder.zip_with(_workbook(), {_CORE: lambda xml: "<broken"}),
    "custom-property-date-only": lambda: builder.zip_with(_workbook(custom=True), {_CUSTOM: lambda xml: re.sub(
        r"<vt:i4([^>]*)>3</vt:i4>", r"<vt:filetime\1>2024-01-05</vt:filetime>", xml)}),
    "custom-property-not-a-number": lambda: builder.zip_with(_workbook(custom=True), {_CUSTOM: lambda xml: xml.replace(">3<", ">abc<")}),
    "broken-custom-properties-xml": lambda: builder.zip_with(_workbook(custom=True), {_CUSTOM: lambda xml: "<broken"}),
}


class TestDocumentPropertiesTheConverterDoesNotNeed:
    """XLSX document properties (creation date, last-printed date, custom properties) are not used in the conversion result. openpyxl
    could not read a single such value (including a date-only value, which W3CDTF allows) and the whole document was rejected.
    DOCX/PPTX with the same core.xml were read."""

    @pytest.mark.parametrize("streaming", [False, True], ids=["full-load", "streamed"])
    @pytest.mark.parametrize("case", list(_PROPERTY_PARTS))
    def test_unreadable_document_property_does_not_reject_the_workbook(self, case, streaming, monkeypatch):
        data = _PROPERTY_PARTS[case]()
        if streaming:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert "| 배 | 5 |" in result.text
        assert result.warnings == []

    def test_title_is_kept_when_only_a_date_cannot_be_read(self):
        data = builder.zip_with(_workbook(title="분기 실적"), _created("2024-01-05"))

        result = convert(data, "a.xlsx")

        assert result.metadata["title"] == "분기 실적"

    def test_broken_document_property_part_gives_no_title(self):
        data = builder.zip_with(_workbook(title="분기 실적"), {_CORE: lambda xml: "<broken"})

        result = convert(data, "a.xlsx")

        assert "title" not in result.metadata


def _merge(fragment_from: str, fragment_to: str) -> bytes:
    return builder.zip_with(_workbook(merged=True), {_SHEET: lambda xml: xml.replace(fragment_from, fragment_to, 1)})


class TestMalformedMergeRange:
    """A merge that cannot be used as a cell range (reversed range, row 0) and unknown attributes discard only that merge. One of
    them got the whole document rejected."""

    @pytest.mark.parametrize(("fragment_from", "fragment_to"), [
        ("</mergeCells>", '<mergeCell ref="D3:C2"/></mergeCells>'),
        ("</mergeCells>", '<mergeCell ref="C0:C2"/></mergeCells>'),
        ('<mergeCell ref="A5:A6"/>', '<mergeCell ref="A5:A6" foo="1"/>'),
        ("<mergeCells ", '<mergeCells foo="2" '),
    ], ids=["reversed-range", "row-0", "unknown-merge-attribute", "unknown-merge-list-attribute"])
    def test_malformed_merge_does_not_reject_the_workbook(self, fragment_from, fragment_to):
        data = _merge(fragment_from, fragment_to)

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert "| 합계 | 9 |" in result.text
        assert result.warnings == []


class TestChartSheetWithoutRelationships:
    """On a chart sheet without a relationships file (.rels) (one openpyxl creates without a chart), openpyxl raised AttributeError
    and the whole document was rejected."""

    def test_chart_sheet_without_relationships_does_not_reject_the_workbook(self):
        data = _workbook(chart_sheet=True)
        assert "xl/chartsheets/_rels/sheet1.xml.rels" not in zipfile.ZipFile(BytesIO(data)).namelist()

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert result.metadata["sheet_count"] == "2"
        assert result.warnings == []


class TestWorkbookListOddities:
    """An unknown attribute on a defined name in the workbook list (workbook.xml), which conversion does not use, or a sheet state
    in the wrong letter case got the whole document rejected."""

    def test_unknown_attribute_on_a_defined_name_does_not_reject_the_workbook(self):
        data = builder.zip_with(_workbook(), {_WORKBOOK: lambda xml: xml.replace(
            "<definedNames/>", '<definedNames><definedName name="x" foo="1">Sheet!$A$1</definedName></definedNames>')})

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert result.warnings == []

    @pytest.mark.parametrize(("state", "hidden"), [("VeryHidden", True), ("HIDDEN", True), ("Visible", False)])
    def test_sheet_state_is_read_regardless_of_letter_case(self, state, hidden):
        data = builder.zip_with(_workbook(second_sheet=True), {_WORKBOOK: lambda xml: xml.replace(
            'name="둘째" sheetId="2" state="visible"', f'name="둘째" sheetId="2" state="{state}"')})

        result = convert(data, "a.xlsx")

        assert "| 사과 | 3 |" in result.text
        assert ("둘째 시트 글" in result.text) is not hidden
        assert result.warnings == (["Hidden sheet '둘째' was excluded."] if hidden else [])

    def test_unknown_sheet_state_is_treated_as_hidden(self):
        data = builder.zip_with(_workbook(second_sheet=True), {_WORKBOOK: lambda xml: xml.replace(
            'name="둘째" sheetId="2" state="visible"', 'name="둘째" sheetId="2" state="foo"')})

        result = convert(data, "a.xlsx")

        assert "둘째 시트 글" not in result.text
        assert result.warnings == ["Hidden sheet '둘째' was excluded."]


def _pptx_with_slide_number(text: str) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = "본문 글"
    paragraph = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(3), Inches(1)).text_frame.paragraphs[0]
    field = etree.SubElement(paragraph._p, "{http://schemas.openxmlformats.org/drawingml/2006/main}fld",
                             id="{88E195E9-9B2B-40B6-90F7-598787693243}", type="slidenum")
    etree.SubElement(field, "{http://schemas.openxmlformats.org/drawingml/2006/main}t").text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TestSlideNumberPlaceholder:
    """PPTX saved by LibreOffice writes `<number>` instead of the number in the slide-number field (PowerPoint writes `‹#›` in the
    master). The section heading ('## Slide N') already gives the number, so the placeholder text is left out. A field holding
    a real number is kept as is."""

    @pytest.mark.parametrize("placeholder", ["<number>", "‹#›"])
    def test_slide_number_placeholder_is_left_out(self, placeholder):
        data = _pptx_with_slide_number(placeholder)

        text = convert(data, "a.pptx").text

        assert "본문 글" in text
        assert placeholder not in text

    def test_slide_number_written_as_a_number_is_kept(self):
        data = _pptx_with_slide_number("7")

        text = convert(data, "a.pptx").text

        assert "7" in text


class TestNumbersPastTheTable:
    """Hangul/ideograph counting numbers (일, 一) are written as counting words past 10 too (checked on the LibreOffice screen: 십일,
    이십, 백일, 백십일, 十一, 二十, 一百, 一百零一, 一百一十一). Word's koreanDigital writes digit by digit (일영, 일일). 가나다 and ㄱㄴㄷ
    cycle through 14 and return to 가 / ㄱ (same as the LibreOffice screen, `bRecycleSymbol`)."""

    @pytest.mark.parametrize(("value", "expected"), [
        (10, "십"), (11, "십일"), (14, "십사"), (20, "이십"), (21, "이십일"), (99, "구십구"), (100, "백"), (101, "백일"),
        (111, "백십일"), (1001, "천일"), (1010, "천십"), (9999, "구천구백구십구")])
    def test_hangul_counting_numbers(self, value, expected):
        shape = DOCX_NUMBER_SHAPES["koreanCounting"]

        text = format_number(value, shape)

        assert text == expected

    @pytest.mark.parametrize(("value", "expected"), [
        (10, "十"), (11, "十一"), (20, "二十"), (21, "二十一"), (99, "九十九"), (100, "一百"), (101, "一百零一"),
        (111, "一百一十一"), (1001, "一千零一"), (1010, "一千零一十"), (1100, "一千一百")])
    def test_ideograph_counting_numbers(self, value, expected):
        shape = DOCX_NUMBER_SHAPES["chineseCounting"]

        text = format_number(value, shape)

        assert text == expected

    @pytest.mark.parametrize(("value", "expected"), [(9, "구"), (10, "일영"), (11, "일일"), (100, "일영영"), (111, "일일일")])
    def test_korean_digital_numbers_are_written_digit_by_digit(self, value, expected):
        shape = DOCX_NUMBER_SHAPES["koreanDigital"]

        text = format_number(value, shape)

        assert text == expected

    @pytest.mark.parametrize(("shape", "value", "expected"), [
        ("HANGUL_SYLLABLE", 14, "하"), ("HANGUL_SYLLABLE", 15, "가"), ("HANGUL_SYLLABLE", 111, "파"),
        ("HANGUL_JAMO", 15, "ㄱ")])
    def test_hangul_letters_cycle_after_the_fourteenth(self, shape, value, expected):
        text = format_number(value, shape)

        assert text == expected


class TestMalformedMergeRangeAndTheMergeLimit:
    """A reversed merge was counted as covering a negative number of cells; once such merges were discarded, it could offset the
    cell count of a large merge and lead to the full load that expands merges."""

    def test_reversed_merge_does_not_offset_the_merged_cell_count(self, monkeypatch):
        monkeypatch.setattr("opendockit.extractors.spreadsheet._MAX_MERGED_CELLS", 10)
        data = _merge('<mergeCell ref="A5:A6"/>', '<mergeCell ref="D1:B30"/><mergeCell ref="A5:A20"/>')

        text = convert(data, "a.xlsx").text

        assert text.count("합계") == 1
