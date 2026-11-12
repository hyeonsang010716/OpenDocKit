"""Excel: a single defect in a part the result does not use (character formatting, named styles, form-control VML, drawing
records) neither rejects the whole document nor drops text."""

from io import BytesIO
import time

import openpyxl
import pytest

from opendockit import convert
from tests import builder
from tests.test_spreadsheet_cell_values_and_controls import (
    _vml_control,
    _workbook_with_controls,
)


_SHEET = "xl/worksheets/sheet1.xml"
_TABLE = "| 품목 | 수량 |\n| --- | --- |\n| 볼트 | 120 |"


def _workbook() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["품목", "수량"])
    sheet.append(["볼트", 120])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(params=["full", "streamed"])
def load_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both paths, full load and streaming (read_only), must give the same result."""
    if request.param == "streamed":
        monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)
    return request.param


def _with_shared_string(item: str) -> bytes:
    """A workbook whose cell A2 points to shared string 0 (the `<si>` given as `item`)."""
    strings = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">'
               f"{item}</sst>")
    relationship = ('<Relationship Id="rIdS" Target="sharedStrings.xml" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/sharedStrings"/></Relationships>')
    override = ('<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sharedStrings+xml"/></Types>')
    return builder.zip_with(_workbook(), edit={
        _SHEET: lambda xml: xml.replace('<c r="A2" t="inlineStr"><is><t>&#48380;&#53944;</t></is></c>',
                                        '<c r="A2" t="s"><v>0</v></c>'),
        "xl/_rels/workbook.xml.rels": lambda xml: xml.replace("</Relationships>", relationship),
        "[Content_Types].xml": lambda xml: xml.replace("</Types>", override),
    }, add={"xl/sharedStrings.xml": strings})


_BAD_RUNS = {
    "hash-color": '<rPr><color rgb="#FF0000"/></rPr>',
    "comma-size": '<rPr><sz val="10,5"/></rPr>',
    "unknown-underline": '<rPr><u val="bogus"/></rPr>',
    "unknown-script": '<rPr><vertAlign val="bogus"/></rPr>',
}


class TestMalformedTextRunProperties:
    """When one character-formatting attribute of a shared or inline string was outside the spec (`color rgb="#FF0000"`,
    `sz val="10,5"`), openpyxl rejected the whole shared-string table and the document became 'corrupted'. Excel and LibreOffice
    open it. The formatting is dropped and only the text is read (the text is unchanged, so no warning)."""

    @pytest.mark.parametrize("run", _BAD_RUNS.values(), ids=_BAD_RUNS.keys())
    def test_shared_string_with_a_malformed_run_is_read_as_its_text(self, load_mode, run):
        data = _with_shared_string(f"<si><r>{run}<t>볼</t></r><r><t>트</t></r></si>")

        result = convert(data, "a.xlsx")

        assert (result.sections[0].text, result.warnings) == (_TABLE, [])

    def test_shared_string_with_malformed_phonetic_reading_keeps_only_the_text(self, load_mode):
        data = _with_shared_string('<si><t>볼트</t><rPh sb="abc" eb="1"><t>ボルト</t></rPh><phoneticPr type="bogus"/></si>')

        result = convert(data, "a.xlsx")

        assert (result.sections[0].text, result.warnings) == (_TABLE, [])

    def test_inline_string_with_a_malformed_run_is_read_as_its_text(self, load_mode):
        data = builder.zip_with(_workbook(), edit={_SHEET: lambda xml: xml.replace(
            "<is><t>&#48380;&#53944;</t></is>", '<is><r><rPr><sz val="abc"/></rPr><t>볼</t></r><r><t>트</t></r></is>')})

        result = convert(data, "a.xlsx")

        assert (result.sections[0].text, result.warnings) == (_TABLE, [])


class TestNamedStyleOutOfRange:
    """When the fontId/fillId/borderId of a named style (cellStyleXfs) is a missing index, openpyxl rejected it with IndexError.
    As with the same defect in an ordinary cell format, it is read again with a stylesheet keeping only the display formats
    (`_repaired_copies`)."""

    @pytest.mark.parametrize("attribute", ["fontId", "fillId", "borderId"])
    def test_workbook_is_read_with_the_number_formats_only(self, attribute):
        def damaged(xml: str) -> str:
            head, _, tail = xml.partition("<cellStyleXfs")
            return head + "<cellStyleXfs" + tail.replace(f'{attribute}="0"', f'{attribute}="7"', 1)

        data = builder.zip_with(_workbook(), edit={"xl/styles.xml": damaged})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == _TABLE

    def test_out_of_range_style_number_prints_nothing(self, capsys):
        """With a named-style index out of range, openpyxl printed '-99999 is out of range' to standard output and raised
        IndexError. It cluttered server logs, so only that output is removed (the exception still leads to the re-read)."""
        data = builder.zip_with(_workbook(), edit={"xl/styles.xml": lambda xml: xml.replace('xfId="0" builtinId="0"',
                                                                                         'xfId="-99999" builtinId="0"', 1)})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == _TABLE
        assert capsys.readouterr().out == ""


class TestDamagedFormControlDrawing:
    """Form-control check boxes are read from the sheet VML as an extra. An empty or broken VML does not reject the whole
    document, and the unclosed `<br>` that Excel writes does not push the shape structure so that a selected check box is lost."""

    @pytest.mark.parametrize("vml", ["", "\x00garbage"], ids=["empty", "garbage"])
    def test_unreadable_drawing_skips_only_the_controls(self, vml):
        base = _workbook_with_controls(_vml_control("Checkbox", "동의함", "1, 15, 4, 2, 2, 15, 5, 2", "1"))
        data = builder.zip_with(base, edit={"xl/drawings/vmlDrawing1.vml": lambda _: vml})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text.endswith("동의 여부")

    def test_label_with_an_unclosed_line_break_keeps_the_check_box(self):
        shapes = (_vml_control("Checkbox", "동의<br>함", "1, 15, 4, 2, 2, 15, 5, 2", "1")
                  + _vml_control("Checkbox", "동의하지 않음", "2, 15, 4, 2, 3, 15, 5, 2", None))

        result = convert(_workbook_with_controls(shapes), "a.xlsx")

        assert result.sections[0].text.endswith("동의 여부\n\n☒ 동의함\n\n☐ 동의하지 않음")  # same text as with a closed `<br/>`

    @pytest.mark.parametrize(("label", "shown"), [
        ("동의<br></br>함", "동의함"),
        ("동의<BR>함", "동의함"),
        ("<p>동의함", "동의함"),
        ("동의&nbsp;함 &amp; 확인", "동의 함 & 확인"),
    ], ids=["closed-break", "upper-case-break", "unclosed-paragraph", "html-entities"])
    def test_html_shaped_label_keeps_both_check_boxes(self, label, shown):
        shapes = (_vml_control("Checkbox", label, "1, 15, 4, 2, 2, 15, 5, 2", "1")
                  + _vml_control("Checkbox", "아니오", "2, 15, 4, 2, 3, 15, 5, 2", None))

        result = convert(_workbook_with_controls(shapes), "a.xlsx")

        assert result.sections[0].text.endswith(f"동의 여부\n\n☒ {shown}\n\n☐ 아니오")

    @pytest.mark.timing
    def test_many_unfinished_tags_take_linear_time(self):
        base = _workbook_with_controls(_vml_control("Checkbox", "동의함", "1, 15, 4, 2, 2, 15, 5, 2", "1"))
        data = builder.zip_with(base, edit={"xl/drawings/vmlDrawing1.vml": lambda xml: xml + "<br" * 60_000})

        started = time.perf_counter()
        result = convert(data, "a.xlsx")

        assert time.perf_counter() - started < 2
        assert result.sections[0].text.startswith("개인정보")


class TestLongDrawingContinuation:
    """Joining the CONTINUE chain after an XLS drawing record took quadratic time and memory (16MB: 2.2s, 1.1GB; 48MB: 11s,
    3.3GB)."""

    @pytest.mark.timing
    def test_long_continue_chain_takes_linear_time(self):
        chain = builder.xls_record(0x003C, b"\x00" * 8000) * 2000
        extra = builder.xls_record(0x00EC, builder.escher_record(0xF004, version=0xF)) + chain
        data = builder.xls_bytes([("시트1", [["품목", "수량"], ["볼트", "120"]], extra)])  # 16MB (a stream not wrapped in OLE)

        started = time.perf_counter()
        result = convert(data, "a.xls")

        assert time.perf_counter() - started < 1
        assert result.sections[0].text == _TABLE


class TestDamagedDrawingRelationships:
    """Only the converter reads the drawing's relationship part (`drawing1.xml.rels`). When this one part was broken, the whole
    workbook, table included, was rejected as 'corrupted' — only the pictures and charts are left out, with a notice."""

    _RELATIONSHIPS = "xl/drawings/_rels/drawing1.xml.rels"
    _WARNING = "Pictures and charts of a sheet that have no relationship information were left out."

    @pytest.mark.parametrize("broken", ["<not xml", ""], ids=["garbage", "empty"])
    def test_the_table_is_kept_and_the_picture_is_left_out_with_a_warning(self, load_mode, broken):
        data = builder.zip_with(builder.xlsx_with_image(), edit={self._RELATIONSHIPS: lambda xml: broken})

        result = convert(data, "a.xlsx")

        assert "| 항목 | 값 |" in result.text
        assert result.images == []
        assert result.warnings == [self._WARNING]

    def test_a_workbook_with_intact_relationships_keeps_its_picture(self, load_mode):
        result = convert(builder.xlsx_with_image(), "a.xlsx")

        assert len(result.images) == 1 and result.warnings == []
