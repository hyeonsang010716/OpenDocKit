"""Sheets read by `_openpyxl_reading._quick_rows` give exactly what openpyxl's parser gives through the replacements of
`_openpyxl_reading`: the same text, the same warnings, the same failures. Whatever it does not take on — an odd cell, an odd
row, a part with a document type declaration or another encoding, XML that does not parse — is read the way it always was."""

import contextlib
from io import BytesIO
import zipfile

import pytest

from opendockit import convert
from opendockit.exceptions import ConversionError
from opendockit.extractors import _openpyxl_reading, spreadsheet
from opendockit.models import ConvertOptions
from tests import builder
from tests.test_audit_spreadsheet import _MAIN, _SHEET, _workbook


# Cell formats by position: 1 `#,##0.00`, 2 a date (14), 3 `0.00%`, 4 `[h]:mm`, 5 `aaa`, 6 `@" 님"`, 7 indented, 8 an East Asian date id
_STYLES = (f'<styleSheet xmlns="{_MAIN}"><numFmts count="4"><numFmt numFmtId="164" formatCode="#,##0.00"/>'
           '<numFmt numFmtId="165" formatCode="[h]:mm"/><numFmt numFmtId="166" formatCode="aaa"/>'
           '<numFmt numFmtId="167" formatCode="@&quot; 님&quot;"/></numFmts><fonts count="1"><font/></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs><cellXfs count="9">'
           '<xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/><xf numFmtId="14" applyNumberFormat="1"/>'
           '<xf numFmtId="10" applyNumberFormat="1"/><xf numFmtId="165" applyNumberFormat="1"/><xf numFmtId="166" applyNumberFormat="1"/>'
           '<xf numFmtId="167" applyNumberFormat="1"/><xf numFmtId="0" applyAlignment="1"><alignment indent="2"/></xf>'
           '<xf numFmtId="31" applyNumberFormat="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
_STRINGS = (f'<sst xmlns="{_MAIN}"><si><t>서울</t></si><si><r><rPr><vertAlign val="superscript"/></rPr><t>2</t></r><r><t>m</t></r></si>'
            '<si><t xml:space="preserve"> sp _x005F_x000D_ </t></si><si><t/></si><si><t>a</t><rPh sb="0" eb="1"><t>ア</t></rPh></si></sst>')
_STRINGS_TYPE = ('<Override PartName="/xl/sharedStrings.xml" '
                 'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')

_USUAL = ('<row r="1" spans="1:4"><c r="A1" t="s"><v>0</v></c><c r="B1" s="1"><v>1234.5</v></c><c r="C1" s="2"><v>45000</v></c>'
          '<c r="D1" s="3"><v>0.125</v></c></row>'
          '<row r="2" ht="20" customHeight="1"><c r="A2" t="str"><v>&amp;text&lt;</v></c><c r="B2" t="b"><v>1</v></c>'
          '<c r="C2" t="e"><v>#N/A</v></c><c r="D2" t="inlineStr"><is><t>inline x005F_</t></is></c></row>'
          '<row r="3"><c r="A3" s="7" t="s"><v>0</v></c><c r="B3"><f>A1+1</f><v>7</v></c><c r="C3" s="4"><v>1.5</v></c>'
          '<c r="D3" s="5"><v>45000</v></c><c r="E3" s="8"><v>45000.5</v></c><c r="F3" s="6" t="s"><v>0</v></c></row>'
          '<row r="5"><c r="A5"/><c r="B5" t="s"><v>1</v></c><c r="C5" t="s"><v>2</v></c><c r="D5" t="s"><v>3</v></c>'
          '<c r="E5" t="s"><v>4</v></c><c r="F5"><v>-2</v></c><c r="G5" s="2"><v>-1</v></c><c r="H5"><v>1E3</v></c></row>')
_ODD_CELLS = ('<row r="1"><c r="A1"><v>1,5</v></c><c r="B1"><v>nan</v></c><c r="C1"><v> 7</v></c><c r="D1"><v>1e400</v></c>'
              '<c r="E1" s="99"><v>1</v></c><c r="F1" s="x"><v>2</v></c><c r="G1" s=""><v>3</v></c><c r="h1"><v>4</v></c></row>'
              '<row r="2"><c><v>5</v></c><c t="d"><v>2024-01-05 13:00:00</v></c><c t="b"><v>true</v></c><c t="s"><v>99</v></c>'
              '<c t="s"><v>-1</v></c><c vm="1" t="e"><v>#VALUE!</v></c><c t="zz"><v>x</v></c><c cm="1"><v>6</v></c></row>'
              '<row r="3"><c r="A3" t="inlineStr"><is><r><t>a</t></r><r><rPr><b/></rPr><t>b</t></r></is></c>'
              '<c r="B3" t="inlineStr"/><c r="C3" t="inlineStr"><v>9</v></c><c r="D3"><!-- note --><v>8</v><?pi x?></c>'
              '<c r="E3"><v><![CDATA[12]]></v></c><c r="F3"><v/></c><c r="Z3"><v>1</v></c><c r="B3"><v>over</v></c>'
              '<c r="A0"><v>0</v></c><c r="ZZZZ3"><v>1</v></c><c r="XFD3"><v>far</v></c></row>'
              '<extra/>')
_ODD_ROWS = ('<row r="2.0"><c r="A2"><v>1</v></c></row><row r="abc"><c r="A3"><v>2</v></c></row><row><c><v>3</v></c></row>'
             '<row r="1"><c r="A1"><v>back</v></c></row><row r="9000"><c r="A9000"><v>far</v></c></row>'
             '<row r="9001"><c r="A12"><v>other row</v></c></row><row r="9002" spans="1:1" x14ac:dyDescent="0.25" '
             'xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"><c r="A9002"><v>4</v></c></row>')
_MERGES = '<mergeCells count="2"><mergeCell ref="A1:B2"/><mergeCell ref="D3:C1"/></mergeCells>'


def _xlsx(rows: str, after: str = "", prolog: str = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          encoding: str = "utf-8") -> bytes:
    data = builder.zip_with(_workbook(), edit={"xl/styles.xml": lambda _: _STYLES,
                                               "[Content_Types].xml": lambda xml: xml.replace("</Types>", _STRINGS_TYPE)},
                            add={"xl/sharedStrings.xml": _STRINGS})
    sheet = f'{prolog}<worksheet xmlns="{_MAIN}"><dimension ref="A1"/><sheetData>{rows}</sheetData>{after}</worksheet>'
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, sheet.encode(encoding) if name == _SHEET else source.read(name))
    return buffer.getvalue()


def _outcome(data: bytes, options: ConvertOptions) -> tuple:
    try:
        result = convert(data, "a.xlsx", options=options)
    except Exception as exc:  # noqa: BLE001 — the failure is part of what is compared
        return type(exc), str(exc)
    return result.text, tuple(result.warnings)


# The readers compared with openpyxl's parser: the quick rows alone, and the native reader in front of them where it is installed
_READERS = ["quick rows", *(["native reader"] if _openpyxl_reading._native is not None else [])]


@pytest.fixture(params=_READERS)
def reader(request: pytest.FixtureRequest) -> str:
    return request.param


def _both(monkeypatch: pytest.MonkeyPatch, data: bytes, options: ConvertOptions, reader: str = "quick rows") -> tuple:
    """The outcome through openpyxl's parser alone, and through `reader` in front of it."""
    monkeypatch.setattr(_openpyxl_reading, "QUICK_ROWS", False)
    monkeypatch.setattr(_openpyxl_reading, "NATIVE_ROWS", False)
    expected = _outcome(data, options)
    monkeypatch.setattr(_openpyxl_reading, "QUICK_ROWS", reader == "quick rows")
    monkeypatch.setattr(_openpyxl_reading, "NATIVE_ROWS", reader == "native reader")
    return expected, _outcome(data, options)


@pytest.fixture(params=["full load", "streamed"])
def streamed(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    if request.param == "streamed":
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
    return request.param == "streamed"


_SHEETS = {
    "usual cells": (_USUAL, _MERGES),
    "odd cells": (_ODD_CELLS, ""),
    "odd rows": (_ODD_ROWS, ""),
    # openpyxl's parser reads a row and a merge list wherever they stand
    "row after the sheet data": ('<row r="1"><c r="A1"><v>1</v></c></row>', '<row r="9"><c r="A9"><v>9</v></c></row>'),
    "row inside an extension": ('<row r="1"><c r="A1"><v>1</v></c></row>',
                                '<extLst><row r="7"><c r="A7"><v>7</v></c></row><mergeCells><mergeCell ref="A1:B2"/></mergeCells></extLst>'),
    "row inside a cell": ('<row r="1"><c r="A1"><f>x</f><extra><row r="5"><c r="B5"><v>5</v></c></row></extra><v>1</v></c></row>', ""),
    "cells beyond the column limit": ("".join(f'<row r="{row}">' + "".join(f'<c r="{letter}{row}"><v>{row}</v></c>'
                                                                         for letter in ("A", "B", "CW", "CX", "XFD")) + "</row>"
                                              for row in range(1, 6)), ""),
}


class TestSameRowsAsOpenpyxlsParser:
    @pytest.mark.parametrize("sheet", list(_SHEETS))
    @pytest.mark.parametrize("max_chars", [None, 30])
    def test_sheet(self, monkeypatch, streamed, reader, sheet, max_chars):
        rows, after = _SHEETS[sheet]
        expected, quick = _both(monkeypatch, _xlsx(rows, after), ConvertOptions(max_chars=max_chars), reader)
        assert quick == expected
        assert isinstance(expected[0], str)

    def test_usual_cells_are_read_as_excel_shows_them(self, monkeypatch, streamed, reader):
        expected, quick = _both(monkeypatch, _xlsx(_USUAL), ConvertOptions(), reader)
        assert quick == expected
        for shown in ("서울", "1,234.50", "2023-03-15", "12.50%", "36:00", "&text<", "TRUE", "#N/A", "inline", "sp", "m"):
            assert shown in quick[0]

    @pytest.mark.parametrize("prolog", [
        '<!DOCTYPE worksheet [<!ENTITY e "ENTITY TEXT">]>',
        '<?xml version="1.1" encoding="UTF-8"?>',
        "<?xml version='1.0' encoding='utf-8'?>",
        "",
    ], ids=["document type", "version 1.1", "single quotes", "no declaration"])
    def test_prologs(self, monkeypatch, streamed, reader, prolog):
        rows = '<row r="1"><c r="A1" t="str"><v>&e;</v></c><c r="B1"><v>1</v></c></row>' if "ENTITY" in prolog else _USUAL
        expected, quick = _both(monkeypatch, _xlsx(rows, prolog=prolog), ConvertOptions(), reader)
        assert quick == expected

    @pytest.mark.parametrize("value", ["&#65;&#x42;&amp;&lt;", "&#x+41;", "&#+65;", "&#x;", "&#0;", "&undefined;"])
    def test_references(self, monkeypatch, streamed, reader, value):
        rows = f'<row r="1"><c r="A1" t="str"><v>{value}</v></c><c r="B1" t="str"><v>after</v></c></row>'
        expected, quick = _both(monkeypatch, _xlsx(rows), ConvertOptions(), reader)
        assert quick == expected

    def test_part_in_another_encoding(self, monkeypatch, streamed, reader):
        rows = '<row r="1"><c r="A1" t="str"><v>café</v></c></row>'
        data = _xlsx(rows, prolog='<?xml version="1.0" encoding="ISO-8859-1"?>', encoding="latin-1")
        expected, quick = _both(monkeypatch, data, ConvertOptions(), reader)
        assert quick == expected

    @pytest.mark.parametrize("cut", [0.3, 0.6, 0.95])
    def test_xml_that_breaks_off(self, monkeypatch, streamed, reader, cut):
        rows = "".join(f'<row r="{row}"><c r="A{row}" t="str"><v>row {row}</v></c></row>' for row in range(1, 400))
        data = _xlsx(rows)
        with zipfile.ZipFile(BytesIO(data)) as archive:
            sheet = archive.read(_SHEET)
        broken = sheet[:int(len(sheet) * cut)] + b"<</broken>" + sheet[int(len(sheet) * cut):]
        data = builder.zip_with(data, edit={_SHEET: lambda _: broken.decode()})
        expected, quick = _both(monkeypatch, data, ConvertOptions(max_chars=None), reader)
        assert quick == expected

    @pytest.mark.parametrize("part", ["<extLst><ext/></extLst>", "<extLst/>", "<hyperlinks><hyperlink ref=\"A1\"/></hyperlinks>",
                                      "<cols/>", "<mergeCells><mergeCell ref=\"A1:B1\"/></mergeCells>"])
    def test_a_part_inside_a_cell_leaves_the_cell_as_it_is(self, monkeypatch, streamed, reader, part):
        """`CT_Cell` may end with an `<extLst>`, which ends before its cell does: letting go of what came before it took the
        cell's `<v>`, and the cell came out empty where openpyxl's parser reads its value. The same for the other parts."""
        rows = (f'<row r="1"><c r="A1" t="b"><v>1</v>{part}</c><c r="B1"><v>42</v>{part}</c><c r="C1" t="s"><v>0</v>{part}</c>'
                f'</row><row r="2">{part}<c r="A2"><v>7</v></c>{part}</row>')
        expected, quick = _both(monkeypatch, _xlsx(rows), ConvertOptions(), reader)
        assert quick == expected
        assert "TRUE" in quick[0] and "7" in quick[0]  # (B1 is hidden by the merge where the part is one)

    def test_formulas_read_as_written(self, monkeypatch, streamed, reader):
        rows = ('<row r="1"><c r="A1"><v>1</v></c><c r="B1"><f t="shared" ref="B1:B3" si="0">A1*2</f></c></row>'
                '<row r="2"><c r="A2"><v>2</v></c><c r="B2"><f t="shared" si="0"/></c></row>')
        expected, quick = _both(monkeypatch, _xlsx(rows), ConvertOptions(), reader)
        assert quick == expected


class TestUsualCellsAreReadDirectly:
    def test_no_usual_cell_goes_through_openpyxls_cell_parser(self, monkeypatch, streamed):
        calls = []
        original = _openpyxl_reading._tolerant_parse_cell
        monkeypatch.setattr(_openpyxl_reading, "_tolerant_parse_cell", lambda *args: calls.append(1) or original(*args))
        rows = _USUAL.replace('<c r="D2" t="inlineStr"><is><t>inline x005F_</t></is></c>', "")
        convert(_xlsx(rows), "a.xlsx")
        assert calls == []

    def test_part_with_a_document_type_is_read_by_openpyxls_parser(self, monkeypatch, streamed):
        read = []
        original = _openpyxl_reading._openpyxl_rows
        monkeypatch.setattr(_openpyxl_reading, "_openpyxl_rows", lambda *args: read.append(1) or original(*args))
        convert(_xlsx(_USUAL), "a.xlsx")
        assert read == []
        # Whether the declaration is accepted depends on openpyxl's parser (defusedxml, when installed, refuses entities)
        with contextlib.suppress(ConversionError):
            convert(_xlsx(_USUAL, prolog='<!DOCTYPE worksheet [<!ENTITY e "x">]>'), "a.xlsx")
        assert read  # once, or once per repaired copy when the first reading is refused


@pytest.mark.skipif(_openpyxl_reading._native is None, reason="the native reader is not installed")
class TestNativeReader:
    def test_usual_sheet_is_read_by_the_native_reader(self, monkeypatch, streamed):
        quick = []
        original = _openpyxl_reading._quick_rows
        monkeypatch.setattr(_openpyxl_reading, "_quick_rows", lambda *args: quick.append(1) or original(*args))
        convert(_xlsx(_USUAL, _MERGES), "a.xlsx")
        assert quick == []

    @pytest.mark.parametrize("rows", [_ODD_CELLS, _ODD_ROWS], ids=["odd cells", "odd rows"])
    def test_what_it_does_not_take_on_goes_to_the_quick_rows(self, monkeypatch, streamed, rows):
        quick = []
        original = _openpyxl_reading._quick_rows
        monkeypatch.setattr(_openpyxl_reading, "_quick_rows", lambda *args: quick.append(1) or original(*args))
        convert(_xlsx(rows), "a.xlsx")
        assert quick
