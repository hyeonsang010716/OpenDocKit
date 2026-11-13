"""Excel: an XLS encrypted with the default password (VelvetSweatshop), in-cell pictures and data types (rich values), the state
of form-control check boxes and option buttons, a single malformed cell value, SmartArt placed on a sheet, date/time cells without
a format."""

from hashlib import md5, sha1
from io import BytesIO
import struct

import openpyxl
import pytest

from opendockit import ConvertOptions, EncryptedDocumentError, convert
from tests import builder


_SHEET = "xl/worksheets/sheet1.xml"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _workbook(rows: list[list[object]], title: str = "Sheet") -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(params=["full", "streamed"])
def load_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both paths, full load and streaming (read_only), must give the same result."""
    if request.param == "streamed":
        monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)
    return request.param


# ---- XLS with the default password (VelvetSweatshop) ------------------------------------------------------------

_VELVET = "VelvetSweatshop"
# Records that are not encrypted: BOF, FILEPASS, INTERFACEHDR, RRDHEAD, USREXCL, FILELOCK, RRDINFO ([MS-XLS] 2.2.10)
_PLAIN_RECORDS = {0x0809, 0x002F, 0x00E1, 0x0138, 0x0194, 0x0195, 0x0196}


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 255
        state[i], state[j] = state[j], state[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) & 255])
    return bytes(out)


def _standard_key(password: str, salt: bytes, block: int) -> bytes:
    first = md5(password.encode("utf-16-le")).digest()[:5]
    second = md5((first + salt) * 16).digest()[:5]
    return md5(second + struct.pack("<I", block)).digest()


def _cryptoapi_key(password: str, salt: bytes, block: int, key_bits: int) -> bytes:
    digest = sha1(sha1(salt + password.encode("utf-16-le")).digest() + struct.pack("<I", block)).digest()
    return digest[:5] + bytes(11) if key_bits == 40 else digest[:key_bits // 8]


def _encrypted_xls(password: str, *, cryptoapi: bool = False, key_bits: int = 128) -> bytes:
    """An XLS whose workbook stream from `builder.xls_bytes` is encrypted with RC4 (standard or CryptoAPI) and wrapped in OLE."""
    plain = builder.xls_bytes([("시트1", [["품목", "수량"], ["볼트", "120"]], b"")])
    records = []
    position = 0
    while position + 4 <= len(plain):
        record_id, length = struct.unpack_from("<HH", plain, position)
        records.append([record_id, plain[position + 4:position + 4 + length]])
        position += 4 + length

    salt, verifier = bytes(range(16)), bytes(range(16, 32))
    if cryptoapi:
        key_of = lambda block: _cryptoapi_key(password, salt, block, key_bits)  # noqa: E731
        sealed = _rc4(key_of(0), verifier + sha1(verifier).digest())
        header = struct.pack("<IIIIIIII", 0x04, 0, 0x6801, 0x8004, key_bits, 1, 0, 0) + "CSP\0".encode("utf-16-le")
        filepass = (struct.pack("<HHHII", 1, 4, 2, 0x04, len(header)) + header
                    + struct.pack("<I", 16) + salt + sealed[:16] + struct.pack("<I", 20) + sealed[16:])
    else:
        key_of = lambda block: _standard_key(password, salt, block)  # noqa: E731
        filepass = struct.pack("<HHH", 1, 1, 1) + salt + _rc4(key_of(0), verifier + md5(verifier).digest())
    records.insert(1, [0x002F, filepass])
    for record in records:  # shift the sheet positions (BOUNDSHEET) by the size of the inserted FILEPASS
        if record[0] == 0x0085:
            record[1] = struct.pack("<I", struct.unpack_from("<I", record[1])[0] + 4 + len(filepass)) + record[1][4:]

    out = bytearray()
    encrypting = False
    for record_id, body in records:
        out += struct.pack("<HH", record_id, len(body))
        if encrypting and record_id not in _PLAIN_RECORDS:
            stream = bytearray()
            offset, left = len(out), len(body)
            while left:
                block, at = divmod(offset, 1024)
                size = min(left, 1024 - at)
                stream += _rc4(key_of(block), bytes(at + size))[at:]
                offset, left = offset + size, left - size
            sealed_body = bytes(a ^ b for a, b in zip(body, stream, strict=True))
            body = body[:4] + sealed_body[4:] if record_id == 0x0085 else sealed_body
        out += body
        encrypting = encrypting or record_id == 0x002F
    return builder.ole_container({"Workbook": bytes(out)})


class TestXlsEncryptedWithTheDefaultPassword:
    """Excel uses the default password VelvetSweatshop when it encrypts without a password ('Protect Workbook' etc.). The person
    opening it is not asked for a password, yet it was rejected as a 'password-protected document'. It is read when the default
    password decrypts it and rejected only when it does not."""

    @pytest.mark.parametrize("kind", [{}, {"cryptoapi": True}, {"cryptoapi": True, "key_bits": 40}],
                             ids=["rc4", "cryptoapi-128", "cryptoapi-40"])
    def test_workbook_encrypted_with_the_default_password_is_read(self, kind):
        data = _encrypted_xls(_VELVET, **kind)

        result = convert(data, "a.xls")

        assert result.text == "## Sheet: 시트1\n\n| 품목 | 수량 |\n| --- | --- |\n| 볼트 | 120 |"

    @pytest.mark.parametrize("cryptoapi", [False, True])
    def test_workbook_with_a_real_password_is_still_rejected(self, cryptoapi):
        data = _encrypted_xls("secret", cryptoapi=cryptoapi)

        with pytest.raises(EncryptedDocumentError):
            convert(data, "a.xls")


# ---- In-cell pictures and data types ----------------------------------------------------------------------------

_RICH_DATA = "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
_VALUE_METADATA = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><metadata xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">'
                   '<metadataTypes count="1"><metadataType name="XLRICHVALUE" minSupportedVersion="120000"/></metadataTypes>'
                   '<futureMetadata name="XLRICHVALUE" count="{count}">{blocks}</futureMetadata>'
                   '<valueMetadata count="{count}">{values}</valueMetadata></metadata>')


def _rich_value_workbook(values: list[str], structures: str, *, pictures: int = 0, cells: str = "",
                         unresolved_vm: bool = False) -> bytes:
    """A workbook whose cells B2 and B3 are t="e" vm="1"/"2" + #VALUE!. `values` are the rv elements of rdrichvalue; pictures are
    linked through richValueRel."""
    count = len(values)
    blocks = "".join(f'<bk><extLst><ext uri="{{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}}"><xlrd:rvb i="{index}"/></ext></extLst></bk>'
                     for index in range(count))
    metadata = _VALUE_METADATA.format(count=count, blocks=blocks,
                                      values="".join(f'<bk><rc t="1" v="{index}"/></bk>' for index in range(count)))
    parts = {
        "xl/metadata.xml": metadata,
        "xl/richData/rdrichvalue.xml": f'<rvData xmlns="{_RICH_DATA}" count="{count}">{"".join(values)}</rvData>',
        "xl/richData/rdrichvaluestructure.xml": f'<rvStructures xmlns="{_RICH_DATA}">{structures}</rvStructures>',
        "xl/richData/richValueRel.xml": ('<richValueRels xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/'
                                         f'richvaluerel" xmlns:r="{_RELATIONSHIPS_NS}">'
                                         + "".join(f'<rel r:id="rId{index}"/>' for index in range(1, pictures + 1))
                                         + "</richValueRels>"),
        "xl/richData/_rels/richValueRel.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{index}" Type="{_RELATIONSHIPS_NS}/image" Target="../media/image{index}.png"/>'
                      for index in range(1, pictures + 1)) + "</Relationships>"),
    }
    parts.update({f"xl/media/image{index}.png": builder.png_bytes(seed=index) for index in range(1, pictures + 1)})
    relationships = ('<Relationship Id="rId90" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                     'sheetMetadata" Target="metadata.xml"/><Relationship Id="rId91" Type="http://schemas.microsoft.com/'
                     'office/2022/10/relationships/richValueRel" Target="richData/richValueRel.xml"/><Relationship '
                     'Id="rId92" Type="http://schemas.microsoft.com/office/2017/06/relationships/rdRichValue" '
                     'Target="richData/rdrichvalue.xml"/><Relationship Id="rId93" Type="http://schemas.microsoft.com/'
                     'office/2017/06/relationships/rdRichValueStructure" Target="richData/rdrichvaluestructure.xml"/>'
                     "</Relationships>")
    base = _workbook([["제품명", "사진", "단가"], ["A형 펌프", "PIC1", 120000], ["B형 밸브", "PIC2", 45000]], "제품")
    second_vm = "9" if unresolved_vm else "2"

    def sheet(xml: str) -> str:
        xml = xml.replace('<c r="B2" t="inlineStr"><is><t>PIC1</t></is></c>', '<c r="B2" t="e" vm="1"><v>#VALUE!</v></c>')
        xml = xml.replace('<c r="B3" t="inlineStr"><is><t>PIC2</t></is></c>',
                          f'<c r="B3" t="e" vm="{second_vm}"><v>#VALUE!</v></c>')
        return xml.replace("</sheetData>", f"{cells}</sheetData>") if cells else xml

    return builder.zip_with(base, edit={_SHEET: sheet, "xl/_rels/workbook.xml.rels": lambda xml: xml.replace(
        "</Relationships>", relationships)}, add=parts)


_LOCAL_IMAGE = '<s t="_localImage"><k n="_rvRel:LocalImageIdentifier" t="i"/><k n="CalcOrigin" t="i"/></s>'


class TestPicturesAndDataTypesPlacedInCells:
    """A 'Place in Cell' picture or data-type cell of Excel 365 has only `t="e" vm="N"` + `#VALUE!` in the sheet XML; the real
    value is in xl/richData. Picture cells came out as the '#VALUE!' error. A picture becomes a placeholder in its cell, a data
    type its displayed text (_DisplayString)."""

    def test_picture_placed_in_a_cell_becomes_a_placeholder_in_that_cell(self, load_mode):
        data = _rich_value_workbook(['<rv s="0"><v>0</v><v>5</v></rv>', '<rv s="0"><v>1</v><v>5</v></rv>'], _LOCAL_IMAGE,
                                    pictures=2)

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == ("| 제품명 | 사진 | 단가 |\n| --- | --- | --- |\n| A형 펌프 | [Image] | 120000 |\n"
                                           "| B형 밸브 | [Image] | 45000 |")
        assert len(result.images) == 2

    def test_picture_in_a_cell_is_left_out_when_images_are_not_extracted(self):
        data = _rich_value_workbook(['<rv s="0"><v>0</v><v>5</v></rv>', '<rv s="0"><v>1</v><v>5</v></rv>'], _LOCAL_IMAGE,
                                    pictures=2)

        result = convert(data, "a.xlsx", ConvertOptions(extract_images=False))

        assert result.sections[0].text == ("| 제품명 | 사진 | 단가 |\n| --- | --- | --- |\n| A형 펌프 | | 120000 |\n"
                                           "| B형 밸브 | | 45000 |")
        assert result.images == []

    def test_data_type_cell_shows_its_display_text(self, load_mode):
        structures = '<s t="_linkedEntity"><k n="%EntityId" t="s"/><k n="_DisplayString" t="s"/></s>'
        data = _rich_value_workbook(['<rv s="0"><v>X1</v><v>삼성전자</v></rv>', '<rv s="0"><v>X2</v><v>대한민국</v></rv>'],
                                    structures)

        result = convert(data, "a.xlsx")

        assert "| A형 펌프 | 삼성전자 | 120000 |" in result.text
        assert "| B형 밸브 | 대한민국 | 45000 |" in result.text

    def test_rich_value_that_cannot_be_read_is_blank_with_a_warning(self, load_mode):
        data = _rich_value_workbook(['<rv s="0"><v>0</v><v>5</v></rv>'], _LOCAL_IMAGE, pictures=1, unresolved_vm=True)

        result = convert(data, "a.xlsx")

        assert "| B형 밸브 | | 45000 |" in result.text
        assert "#VALUE!" not in result.text
        assert any("1 in-cell picture or data-type value(s)" in warning for warning in result.warnings)

    def test_ordinary_error_cell_is_kept(self, load_mode):
        data = builder.zip_with(_workbook([["가", "나"], ["x", 1]]), edit={
            _SHEET: lambda xml: xml.replace('<c r="A2" t="inlineStr"><is><t>x</t></is></c>', '<c r="A2" t="e"><v>#DIV/0!</v></c>')})

        result = convert(data, "a.xlsx")

        assert "| #DIV/0! | 1 |" in result.text


# ---- Form-control check boxes and option buttons ----------------------------------------------------------------

def _vml_control(object_type: str, label: str, anchor: str, checked: str | None) -> str:
    state = f"<x:Checked>{checked}</x:Checked>" if checked is not None else ""
    return ('<v:shape type="#_x0000_t201" filled="f" stroked="f"><v:textbox><div style="text-align:left">'
            f'<font face="맑은 고딕" size="220" color="auto">{label}</font></div></v:textbox>'
            f'<x:ClientData ObjectType="{object_type}"><x:Anchor>{anchor}</x:Anchor>{state}</x:ClientData></v:shape>')


def _workbook_with_controls(shapes: str, *, control_properties: dict[str, str] | None = None) -> bytes:
    """A workbook with VML (legacyDrawing) form controls on sheet 1. `control_properties` is shapeId → the checked value of
    ctrlProp."""
    base = _workbook([["개인정보 수집·이용 동의"], [], ["수집 항목", "성명, 연락처"], [], ["동의 여부"]], "동의서")
    vml = ('<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
           'xmlns:x="urn:schemas-microsoft-com:office:excel"><o:shapelayout v:ext="edit"><o:idmap v:ext="edit" data="1"/>'
           f"</o:shapelayout>{shapes}</xml>")
    properties = control_properties or {}
    controls = "".join(f'<control shapeId="{shape}" r:id="rIdC{index}" name="Check Box {index}"/>'
                       for index, shape in enumerate(properties, start=1))
    relationships = (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                     f'Id="rIdV" Type="{_RELATIONSHIPS_NS}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
                     + "".join(f'<Relationship Id="rIdC{index}" Type="{_RELATIONSHIPS_NS}/ctrlProp" '
                               f'Target="../ctrlProps/ctrlProp{index}.xml"/>' for index in range(1, len(properties) + 1))
                     + "</Relationships>")
    parts = {"xl/drawings/vmlDrawing1.vml": vml, "xl/worksheets/_rels/sheet1.xml.rels": relationships}
    for index, state in enumerate(properties.values(), start=1):
        checked = f' checked="{state}"' if state else ""
        parts[f"xl/ctrlProps/ctrlProp{index}.xml"] = ('<formControlPr xmlns="http://schemas.microsoft.com/office/'
                                                      f'spreadsheetml/2009/9/main" objectType="CheckBox"{checked}/>')

    def sheet(xml: str) -> str:
        if "xmlns:r=" not in xml:
            xml = xml.replace("<worksheet ", f'<worksheet xmlns:r="{_RELATIONSHIPS_NS}" ', 1)
        block = f"<controls>{controls}</controls>" if controls else ""
        return xml.replace("</worksheet>", f'<legacyDrawing r:id="rIdV"/>{block}</worksheet>')

    return builder.zip_with(base, edit={_SHEET: sheet}, add=parts)


class TestFormControlCheckBoxes:
    """Form-control check boxes and option buttons lost both their label and their selected state (XLSX: VML legacyDrawing,
    XLS: OBJ records). As in DOCX and HWPX they go after the table as `☒ label` / `☐ label`, in the order of their position.
    Cell comments (ObjectType="Note") are still not read here."""

    def test_xlsx_check_boxes_keep_their_labels_and_states(self):
        shapes = (_vml_control("Checkbox", "동의함", "1, 15, 4, 2, 2, 15, 5, 2", "1")
                  + _vml_control("Checkbox", "동의하지 않음", "2, 15, 4, 2, 3, 15, 5, 2", None)
                  + _vml_control("Note", "메모 글", "3, 15, 0, 2, 5, 15, 3, 2", None))

        result = convert(_workbook_with_controls(shapes), "a.xlsx")

        assert result.sections[0].text.endswith("동의 여부\n\n☒ 동의함\n\n☐ 동의하지 않음")
        assert "메모 글" not in result.text

    def test_xlsx_option_buttons_are_read_in_position_order(self):
        shapes = (_vml_control("Radio", "아니오", "3, 0, 4, 0, 4, 0, 5, 0", "0")
                  + _vml_control("Radio", "예", "1, 0, 4, 0, 2, 0, 5, 0", "1"))

        result = convert(_workbook_with_controls(shapes), "a.xlsx")

        assert result.sections[0].text.endswith("☒ 예\n\n☐ 아니오")

    def test_xlsx_check_box_state_from_control_properties(self):
        shape = _vml_control("Checkbox", "동의함", "1, 15, 4, 2, 2, 15, 5, 2", None).replace(
            "<v:shape ", '<v:shape id="_x0000_s1025" ')

        result = convert(_workbook_with_controls(shape, control_properties={"1025": "Checked"}), "a.xlsx")

        assert result.sections[0].text.endswith("☒ 동의함")

    @staticmethod
    def _xls_check_box(label: str, checked: int, object_type: int = 0x0B) -> bytes:
        """Check box (0x0B) / option button (0x0C) OBJ: ftCmo + ftCbls (its first 2 bytes are the state) + ftEnd, followed by
        the label TXO."""
        common = struct.pack("<HHHHH12x", 0x15, 0x12, object_type, 1, 0x6011)
        state = struct.pack("<HHH10x", 0x0A, 12, checked)
        obj = builder.xls_record(0x005D, common + state + bytes(4))
        box = builder.xls_text_box(label)
        return box.replace(builder.xls_object(6), obj)

    def test_xls_check_boxes_keep_their_states(self):
        controls = self._xls_check_box("동의함", 1) + self._xls_check_box("동의하지 않음", 0)
        stream = builder.xls_bytes([("동의서", [["동의 여부"]], controls)])

        result = convert(builder.ole_container({"Workbook": stream}), "a.xls")

        assert result.sections[0].text == "동의 여부\n\n☒ 동의함\n\n☐ 동의하지 않음"


# ---- A single malformed cell value ------------------------------------------------------------------------------

def _patched(edit: dict[str, object]) -> bytes:
    return builder.zip_with(_workbook([["a", "b"], [1, 2], [3, 4]]), edit=edit)


def _cell_a2(replacement: str) -> dict[str, object]:
    return {_SHEET: lambda xml: xml.replace('<c r="A2" t="n"><v>1</v></c>', replacement)}


class TestOneMalformedCellValue:
    """When the value or an attribute of one cell broke the format (out-of-range style or shared-string index, a non-numeric
    number cell, the logical value 'true', a non-ISO date), the whole document was rejected as 'corrupted'. Excel and LibreOffice
    open it. Only that cell is left as its raw text (a shared-string index as blank), with a warning."""

    @pytest.mark.parametrize(("cell", "shown"), [
        ('<c r="A2" t="n"><v>1,5</v></c>', "1,5"),
        ('<c r="A2" t="n"><v>abc</v></c>', "abc"),
        ('<c r="A2"><v>NaN</v></c>', "NaN"),
        ('<c r="A2" t="b"><v>true</v></c>', "TRUE"),
        ('<c r="A2" t="b"><v>yes</v></c>', "yes"),
        ('<c r="A2" t="d"><v>2024.01.05</v></c>', "2024.01.05"),
        ('<c r="A2" t="s"><v>57</v></c>', ""),
        ('<c r="A2" t="s"><v>x</v></c>', ""),
    ], ids=["comma", "letters", "nan", "true", "yes", "dotted-date", "sst-out-of-range", "sst-not-a-number"])
    def test_malformed_value_is_kept_as_its_text(self, load_mode, cell, shown):
        data = _patched(_cell_a2(cell))

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == f"| a | b |\n| --- | --- |\n| {shown} | 2 |\n| 3 | 4 |".replace("|  |", "| |")
        assert any("malformed cell value(s)" in warning for warning in result.warnings)

    @pytest.mark.parametrize("style", ['s="57"', 's=""', 's="x"'])
    def test_cell_style_that_does_not_exist_reads_as_general(self, load_mode, style):
        data = _patched(_cell_a2(f'<c r="A2" {style} t="n"><v>1</v></c>'))

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"

    @pytest.mark.parametrize("edit", [
        lambda xml: xml.replace("<sheetData>", '<cols><col min="0" max="0" width="5"/></cols><sheetData>'),
        lambda xml: xml.replace('<row r="2"', '<row r="2" ht="abc" customHeight="1"'),
        lambda xml: xml.replace('<row r="2"', '<row r="2" s="99" customFormat="1"'),
    ], ids=["column-zero", "row-height", "row-style"])
    def test_malformed_row_or_column_setting_is_ignored(self, edit):
        data = _patched({_SHEET: edit})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"

    @pytest.mark.parametrize("value", ["1E+400", "-1E+400"])
    @pytest.mark.parametrize("number_format", ["General", "yyyy-mm-dd", "hh:mm:ss"])
    def test_number_too_large_for_a_float_is_kept_as_its_text(self, load_mode, value, number_format):
        workbook = openpyxl.Workbook()
        workbook.active.append(["a", "b"])
        workbook.active.append([1, 2])
        workbook.active["A2"].number_format = number_format
        buffer = BytesIO()
        workbook.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={_SHEET: lambda xml: xml.replace("<v>1</v>", f"<v>{value}</v>")})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == f"| a | b |\n| --- | --- |\n| {value} | 2 |"
        assert any("malformed cell value(s)" in warning for warning in result.warnings)

    @pytest.mark.parametrize("number", ["abc", "1.5", "NaN"])
    def test_row_number_that_is_not_a_whole_number_is_ignored(self, load_mode, number):
        data = _patched({_SHEET: lambda xml: xml.replace('<row r="2"', f'<row r="{number}"')})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"

    @pytest.mark.parametrize("attribute", ["fillId", "xfId"])
    def test_style_number_too_large_for_an_integer_reads_with_the_number_formats(self, attribute):
        def damaged(xml: str) -> str:
            head, _, tail = xml.partition("<cellXfs")
            return head + "<cellXfs" + tail.replace(f'{attribute}="0"', f'{attribute}="99999999999"', 1)

        data = builder.zip_with(_workbook([["a", "b"], [1, 2]]), edit={"xl/styles.xml": damaged})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |"

    def test_well_formed_workbook_has_no_warning(self, load_mode):
        result = convert(_patched({}), "a.xlsx")

        assert result.warnings == []


# ---- SmartArt placed on a sheet ---------------------------------------------------------------------------------

_DIAGRAM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"


class TestSmartArtOnASheet:
    """SmartArt in a sheet's drawing part (dgm:relIds of a graphicFrame) was not read, so org charts and process diagrams were
    dropped whole. It is placed where it sits as the same `_charts.render_smartart` nested list as in PPTX and DOCX."""

    def test_smartart_is_read_as_a_nested_list(self):
        main = "http://schemas.openxmlformats.org/drawingml/2006/main"

        def point(model_id: int, text: str = "", kind: str = "") -> str:
            body = f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" if text else "<a:p/>"
            return f'<dgm:pt modelId="{model_id}"{kind}><dgm:t><a:bodyPr/>{body}</dgm:t></dgm:pt>'

        data_model = (f'<dgm:dataModel xmlns:dgm="{_DIAGRAM_NS}" xmlns:a="{main}"><dgm:ptLst>' + point(0, kind=' type="doc"')
                      + point(1, "사업단장") + point(2, "기획팀") + point(3, "운영팀") + "</dgm:ptLst><dgm:cxnLst>"
                      '<dgm:cxn modelId="10" srcId="0" destId="1" srcOrd="0"/><dgm:cxn modelId="11" srcId="1" destId="2" '
                      'srcOrd="0"/><dgm:cxn modelId="12" srcId="1" destId="3" srcOrd="1"/></dgm:cxnLst></dgm:dataModel>')
        frame = (f'<xdr:twoCellAnchor xmlns:xdr="{_DRAWING_NS}"><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row>'
                 '<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>6</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>15'
                 '</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr '
                 'id="3" name="다이어그램 2"/><xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm/>'
                 f'<a:graphic xmlns:a="{main}"><a:graphicData uri="{_DIAGRAM_NS}"><dgm:relIds xmlns:dgm="{_DIAGRAM_NS}" '
                 f'xmlns:r="{_RELATIONSHIPS_NS}" r:dm="rIdDm" r:lo="rIdLo" r:qs="rIdQs" r:cs="rIdCs"/></a:graphicData>'
                 "</a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor>")
        data = builder.zip_with(builder.xlsx_with_image(), edit={
            "xl/drawings/drawing1.xml": lambda xml: xml.replace("</wsDr>", frame + "</wsDr>"),
            "xl/drawings/_rels/drawing1.xml.rels": lambda xml: xml.replace(
                "</Relationships>", f'<Relationship Id="rIdDm" Type="{_RELATIONSHIPS_NS}/diagramData" '
                                    'Target="../diagrams/data1.xml"/></Relationships>'),
        }, add={"xl/diagrams/data1.xml": data_model})

        result = convert(data, "a.xlsx")

        assert "- 사업단장\n  - 기획팀\n  - 운영팀" in result.text


# ---- Date/time cells without a format (t="d") -------------------------------------------------------------------

class TestIsoDateCellsWithoutAFormat:
    """A `t="d"` cell (ISO 8601 value) without a display format dropped the time and gave only the date. When the time is not
    0, date and time are shown together."""

    @pytest.mark.parametrize(("value", "shown"), [
        ("2024-01-05T13:00:00", "2024-01-05 13:00:00"),
        ("2024-01-05 13:00:00", "2024-01-05 13:00:00"),
        ("2024-01-05T00:00:00", "2024-01-05"),
        ("2024-01-05", "2024-01-05"),
    ])
    def test_time_of_day_is_kept(self, load_mode, value, shown):
        data = _patched(_cell_a2(f'<c r="A2" t="d"><v>{value}</v></c>'))

        result = convert(data, "a.xlsx")

        assert f"| {shown} | 2 |" in result.text
