"""Build test documents in memory, so that no binary fixtures have to live in the repository."""

from collections.abc import Callable
from datetime import datetime
from io import BytesIO
import struct
import zipfile
import zlib

from docx import Document
from docx.oxml import parse_xml
from docx.shared import Inches as DocxInches
import openpyxl
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches


def docx_bytes() -> bytes:
    document = Document()
    document.core_properties.title = "분기 보고서"
    document.add_heading("1장 개요", level=1)
    document.add_paragraph("본문   첫 문단입니다.")
    document.add_heading("1.1 세부", level=2)
    document.add_paragraph("사과", style="List Bullet")
    document.add_paragraph("배", style="List Bullet")
    document.add_paragraph("첫째", style="List Number")
    document.add_paragraph("둘째", style="List Number")

    table = document.add_table(rows=3, cols=3)
    for row_index, values in enumerate([["분류", "항목", "값"], ["과일", "사과", "10"], ["", "배", "20"]]):
        for col_index, value in enumerate(values):
            table.cell(row_index, col_index).text = value
    table.cell(1, 0).merge(table.cell(2, 0))
    table.cell(1, 0).text = "과일"

    equation = document.add_paragraph("공식: ")
    equation._p.append(parse_xml(
        '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        "<m:f><m:num><m:r><m:t>a</m:t></m:r></m:num><m:den><m:r><m:t>b</m:t></m:r></m:den></m:f>"
        "<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>"
        "</m:oMath>"
    ))
    document.add_paragraph("끝 | 파이프")

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pptx_bytes() -> bytes:
    presentation = Presentation()
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    title_slide.shapes.title.text = "사업 계획"
    title_slide.placeholders[1].text = "2026년 상반기"

    bullet_slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    bullet_slide.shapes.title.text = "핵심 목표"
    body = bullet_slide.placeholders[1].text_frame
    body.text = "매출 성장"
    sub = body.add_paragraph()
    sub.text = "신규 고객 확보"
    sub.level = 1
    bullet_slide.notes_slide.notes_text_frame.text = "여기서 수치를 강조"

    table_slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    table_slide.shapes.title.text = "실적"
    table = table_slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for (row, col), value in {(0, 0): "지역", (0, 1): "매출", (1, 0): "서울", (1, 1): "100"}.items():
        table.cell(row, col).text = value
    chart_data = CategoryChartData()
    chart_data.categories = ["1분기", "2분기"]
    chart_data.add_series("매출", (10.5, 20))
    table_slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(4), Inches(2), chart_data)

    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "매출"
    sheet.append(["2026년 매출 현황", None, None, None])
    sheet.merge_cells("A1:D1")
    sheet.append(["지역", "지점", "금액", "마감일"])
    sheet.append(["서울", "강남", 1200.0, datetime(2026, 1, 31)])
    sheet.append([None, "종로", 0.1 + 0.2, datetime(2026, 2, 1, 9, 30)])
    sheet.merge_cells("A3:A4")
    sheet.append([None, None, None, None])
    sheet.append(["부산", "서면", True, None])

    hidden = workbook.create_sheet("비밀")
    hidden.append(["숨김"])
    hidden.sheet_state = "hidden"

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(pages: int = 3, two_columns: bool = False) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    font = "HYSMyeongJo-Medium"
    width, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle("테스트 문서")

    for number in range(1, pages + 1):
        pdf.setFont(font, 9)
        pdf.drawString(72, height - 30, "사내 보고서 머리글")
        pdf.drawString(width / 2, 30, f"- {number} -")

        pdf.setFont(font, 20)
        pdf.drawString(72, height - 100, f"제{number}장 개요")
        pdf.setFont(font, 11)
        y = height - 140
        for line in ("첫 문단의 첫 줄입니다", "첫 문단의 둘째 줄입니다"):
            pdf.drawString(72, y, line)
            y -= 14
        y -= 30
        pdf.drawString(72, y, "둘째 문단입니다")

        if number == 1:
            _draw_grid_table(pdf, font, top=height - 300)
        if two_columns and number == 2:
            for index in range(5):
                pdf.drawString(72, 400 - index * 14, f"왼쪽 단 {index + 1}번째 줄 내용이 여기에 있습니다")
                pdf.drawString(320, 400 - index * 14, f"오른쪽 단 {index + 1}번째 줄 내용이 여기에 있습니다")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _draw_grid_table(pdf: object, font: str, top: float) -> None:
    rows = [["품목", "수량", "단가"], ["연필", "3", "500"], ["공책", "2", "1200"]]
    left, col_width, row_height = 72, 100, 20
    for index in range(len(rows) + 1):
        pdf.line(left, top - index * row_height, left + col_width * 3, top - index * row_height)
    for index in range(4):
        pdf.line(left + index * col_width, top, left + index * col_width, top - row_height * len(rows))
    pdf.setFont(font, 10)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            pdf.drawString(left + col_index * col_width + 5, top - (row_index + 1) * row_height + 6, value)


_HWPX_NS = (
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'
)


def hwpx_bytes(section_body: str | None = None, *, encrypted: bool = False) -> bytes:
    body = section_body if section_body is not None else (
        '<hp:p styleIDRef="2"><hp:run><hp:t>1. 사업 개요</hp:t></hp:run></hp:p>'
        '<hp:p styleIDRef="0"><hp:run><hp:t>본문<hp:tab/>탭과 각주</hp:t>'
        '<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run><hp:t>각주 내용</hp:t></hp:run></hp:p></hp:subList>'
        "</hp:footNote></hp:ctrl><hp:t>입니다.</hp:t></hp:run></hp:p>"
        '<hp:p><hp:run><hp:tbl rowCnt="3" colCnt="2">'
        "<hp:tr>" + _hwpx_cell(0, 0, "구분") + _hwpx_cell(0, 1, "내용") + "</hp:tr>"
        "<hp:tr>" + _hwpx_cell(1, 0, "일정", row_span=2) + _hwpx_cell(1, 1, "3월") + "</hp:tr>"
        "<hp:tr>" + _hwpx_cell(2, 1, "4월") + "</hp:tr>"
        "</hp:tbl><hp:t/></hp:run></hp:p>"
        "<hp:p><hp:run><hp:rect><hp:drawText><hp:subList><hp:p><hp:run><hp:t>글상자 안 글</hp:t></hp:run></hp:p>"
        "</hp:subList></hp:drawText></hp:rect></hp:run></hp:p>"
        "<hp:p><hp:run><hp:pic><hp:shapeComment>그림입니다. 원본 그림의 이름: a.png</hp:shapeComment></hp:pic>"
        "</hp:run></hp:p>"
    )
    header = (
        f'<?xml version="1.0" encoding="UTF-8"?><hh:head {_HWPX_NS}><hh:refList><hh:styles>'
        '<hh:style id="0" name="바탕글"/><hh:style id="2" name="개요 1"/>'
        "</hh:styles></hh:refList></hh:head>"
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/">'
        '<opf:metadata><opf:title>한글 문서</opf:title></opf:metadata>'
        '<opf:manifest><opf:item id="section0" href="Contents/section0.xml"/></opf:manifest>'
        '<opf:spine><opf:itemref idref="section0"/></opf:spine></opf:package>'
    )
    manifest = "<manifest><file-entry><encryption-data/></file-entry></manifest>" if encrypted else "<manifest/>"

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("META-INF/manifest.xml", manifest)
        archive.writestr("Contents/header.xml", header)
        archive.writestr("Contents/content.hpf", content)
        archive.writestr("Contents/section0.xml",
                         f'<?xml version="1.0" encoding="UTF-8"?><hs:sec {_HWPX_NS}>{body}</hs:sec>')
    return buffer.getvalue()


def _hwpx_cell(row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1) -> str:
    return (
        f"<hp:tc><hp:subList><hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p></hp:subList>"
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="{col_span}" rowSpan="{row_span}"/></hp:tc>'
    )


def zip_with(data: bytes, edit: dict[str, Callable[[str], str]] | None = None,
             add: dict[str, str] | None = None) -> bytes:
    """Return a copy of a ZIP-based document with part XML edited (`edit`: name → transform function) or new parts added (`add`).

    Used to insert structures that python-docx / python-pptx cannot create (table rows wrapped in a content control, endnotes,
    macro-enabled document types).
    """
    edit, add = edit or {}, add or {}
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            if name in edit:
                changed = edit[name](content.decode("utf-8"))
                assert changed != content.decode("utf-8"), f"{name}: nothing changed"
                content = changed.encode("utf-8")
            target.writestr(name, content)
        for name, content in add.items():
            target.writestr(name, content)
    return buffer.getvalue()


# ---- HWP 5.x record streams ------------------------------------------------------------------------------------

def hwp_record(tag: int, level: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload


def hwp_text(*parts: str | tuple[int, str]) -> bytes:
    """PARA_TEXT payload. A string is characters; a (code, control ID) tuple is an 8-WCHAR extended control."""
    payload = b""
    for part in parts:
        if isinstance(part, tuple):
            code, control = part
            payload += struct.pack("<H", code) + control[::-1].encode("latin-1") + b"\x00" * 8 + struct.pack("<H", code)
        else:
            payload += part.encode("utf-16-le")
    return payload + struct.pack("<H", 13)


def hwp_para_header(style_id: int = 0) -> bytes:
    return struct.pack("<IIHB", 0, 0, 0, style_id) + b"\x00" * 11


def hwp_ctrl(control: str) -> bytes:
    return control[::-1].encode("latin-1") + b"\x00" * 4


def hwp_cell(row: int, col: int, *, row_span: int = 1, col_span: int = 1) -> bytes:
    return struct.pack("<hHI", 1, 0, 0) + struct.pack("<HHHH", col, row, col_span, row_span) + b"\x00" * 18


def hwp_style(name: str) -> bytes:
    return struct.pack("<H", len(name)) + name.encode("utf-16-le") + struct.pack("<H", 0) + b"\x00" * 8


def hwp_file_header(flags: int = 0) -> bytes:
    return b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000300, flags) + b"\x00" * 216


# ---- XLS (BIFF8) record streams ---------------------------------------------------------------------------------

def xls_record(record_id: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HH", record_id, len(payload)) + payload


def xls_bytes(sheets: list[tuple[str, list[list[str]], bytes]], globals_extra: bytes = b"") -> bytes:
    """BIFF8 workbook stream. `sheets` is (sheet name, rows of text cells, records to append after the cells).

    The stream is not wrapped in an OLE container — xlrd reads that too. Format detection (`convert`) treats only OLE as XLS,
    so tests either call `extract_xls` directly or wrap the stream with `ole_container`.
    """
    def begin(kind: int) -> bytes:
        return xls_record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 6))

    def text(value: str, length_format: str = "<H") -> bytes:
        return struct.pack(length_format, len(value)) + b"\x01" + value.encode("utf-16-le")

    bodies = []
    for _, rows, extra in sheets:
        cells = b"".join(xls_record(0x0204, struct.pack("<HHH", row_index, col_index, 0) + text(value))
                         for row_index, row in enumerate(rows) for col_index, value in enumerate(row))
        dimensions = xls_record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, max(map(len, rows)), 0))
        bodies.append(begin(0x0010) + dimensions + cells + extra + xls_record(0x000A))

    head = begin(0x0005) + xls_record(0x0042, struct.pack("<H", 1200))
    position = len(head) + sum(4 + 8 + 2 * len(name) for name, _, _ in sheets) + len(globals_extra) + 4
    bound_sheets = b""
    for (name, _, _), body in zip(sheets, bodies, strict=True):
        bound_sheets += xls_record(0x0085, struct.pack("<IBB", position, 0, 0) + text(name, "<B"))
        position += len(body)
    return head + bound_sheets + globals_extra + xls_record(0x000A) + b"".join(bodies)


_OLE_END_OF_CHAIN, _OLE_FAT_SECTOR, _OLE_FREE, _OLE_NO_STREAM = 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF, 0xFFFFFFFF


def ole_container(streams: dict[str, bytes]) -> bytes:
    """Minimal OLE2 (CFB v3, 512-byte sectors) container holding the streams. `convert` detects XLS from a `Workbook` / `Book` stream.

    As the spec requires, streams under 4,096 bytes go into the mini stream (64-byte mini sectors, mini FAT chain) and larger
    ones into regular sectors. Sector layout is [FAT][directory][mini FAT][mini stream][large streams]. Directory entries link
    the first stream as the child of Root Entry and the rest as right siblings.
    """
    sector = 512

    def pad(body: bytes, unit: int) -> bytes:
        return body + b"\x00" * (-len(body) % unit)

    # Mini stream and mini FAT chain
    mini_chunks: list[bytes] = []
    mini_fat: list[int] = []
    mini_starts: dict[str, int] = {}
    for name, body in streams.items():
        if len(body) < 4096:
            start = sum(len(chunk) for chunk in mini_chunks) // 64
            padded = pad(body, 64)
            mini_starts[name] = start
            mini_fat += [*range(start + 1, start + len(padded) // 64), _OLE_END_OF_CHAIN]
            mini_chunks.append(padded)
    mini_stream = b"".join(mini_chunks)

    # Regular sector layout and FAT chain
    big = {name: pad(body, sector) for name, body in streams.items() if len(body) >= 4096}
    directory_sectors = -(-(1 + len(streams)) * 128 // sector)
    mini_fat_sectors = -(-len(mini_fat) * 4 // sector)
    mini_stream_sectors = len(pad(mini_stream, sector)) // sector
    fat: list[int] = [_OLE_FAT_SECTOR]

    def chain(count: int) -> int:
        start = len(fat)
        fat.extend([*range(start + 1, start + count), _OLE_END_OF_CHAIN] if count else [])
        return start if count else _OLE_END_OF_CHAIN

    directory_start = chain(directory_sectors)
    mini_fat_start = chain(mini_fat_sectors)
    mini_stream_start = chain(mini_stream_sectors)
    big_starts = {name: chain(len(body) // sector) for name, body in big.items()}
    assert len(fat) <= sector // 4, "only sizes that fit in a single FAT sector are supported"
    fat_bytes = b"".join(struct.pack("<I", entry) for entry in fat)
    fat_bytes += struct.pack("<I", _OLE_FREE) * ((sector - len(fat_bytes)) // 4)

    # Directory
    def entry(name: str, kind: int, child: int, right: int, start: int, size: int) -> bytes:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        return (raw.ljust(64, b"\x00") + struct.pack("<HBBIII", len(raw), kind, 1, _OLE_NO_STREAM, right, child)
                + bytes(36) + struct.pack("<II", start, size) + bytes(4))

    names = list(streams)
    entries = [entry("Root Entry", 5, 1 if names else _OLE_NO_STREAM, _OLE_NO_STREAM, mini_stream_start, len(mini_stream))]
    for index, name in enumerate(names):
        right = index + 2 if index + 1 < len(names) else _OLE_NO_STREAM
        start = big_starts[name] if name in big_starts else mini_starts[name]
        entries.append(entry(name, 2, _OLE_NO_STREAM, right, start, len(streams[name])))
    directory = pad(b"".join(entries), sector)

    # Header (512 bytes): signature, CLSID, minor version 0x3E, major version 3, byte order, sector size 2^9, mini sector 2^6,
    # FAT sector count, directory start, mini stream cutoff 4096, mini FAT start/count, DIFAT start/count, 109 DIFAT entries
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16)
              + struct.pack("<HHHHH6xIIIIIIIII", 0x3E, 3, 0xFFFE, 9, 6, 0, 1, directory_start, 0, 4096,
                            mini_fat_start, mini_fat_sectors, _OLE_END_OF_CHAIN, 0)
              + struct.pack("<I", 0) + struct.pack("<I", _OLE_FREE) * 108)
    mini_fat_bytes = pad(b"".join(struct.pack("<I", entry) for entry in mini_fat), sector)
    return header + fat_bytes + directory + mini_fat_bytes + pad(mini_stream, sector) + b"".join(big.values())


def escher_record(record_type: int, payload: bytes = b"", *, version: int = 0, instance: int = 0) -> bytes:
    """One Office drawing (Escher) record. Container records have version 0xF."""
    return struct.pack("<HHI", version | (instance << 4), record_type, len(payload)) + payload


def xls_text_box(text: str, *, object_type: int = 6, unicode: bool = True) -> bytes:
    """Record group for one text box: MSODRAWING, OBJ, MSODRAWING, TXO, CONTINUE (text), CONTINUE (formatting).
    object_type 0x19 is a comment."""
    encoded = b"\x01" + text.encode("utf-16-le") if unicode else b"\x00" + text.encode("latin-1")
    return (xls_record(0x00EC, escher_record(0xF004, version=0xF)) + xls_object(object_type)
            + xls_record(0x00EC, escher_record(0xF00D))
            + xls_record(0x01B6, struct.pack("<HH6xHHI", 0x0212, 0, len(text), 16, 0))
            + xls_record(0x003C, encoded) + xls_record(0x003C, bytes(16)))


def xls_object(object_type: int) -> bytes:
    """OBJ record (common object data ftCmo only). 6 text box, 8 picture, 0x19 comment."""
    return xls_record(0x005D, struct.pack("<HHHHH12x", 0x15, 0x12, object_type, 1, 0x6011) + bytes(4))


def xls_picture(blip_index: int) -> bytes:
    """One picture object: MSODRAWING carrying the picture index (pib) + OBJ."""
    options = escher_record(0xF00B, struct.pack("<HI", 0x4104, blip_index), version=3, instance=1)
    shape = escher_record(0xF00A, bytes(8), version=2)
    return xls_record(0x00EC, escher_record(0xF004, shape + options, version=0xF)) + xls_object(8)


def xls_png_blip(picture: bytes) -> bytes:
    """PNG picture record to put in the picture store."""
    return escher_record(0xF01E, bytes(16) + b"\xff" + picture, instance=0x6E0)


def xls_emf_blip(metafile: bytes) -> bytes:
    """EMF picture record to put in the picture store (deflate-compressed)."""
    packed = zlib.compress(metafile)
    return escher_record(0xF01A, bytes(16) + struct.pack("<I24xIBB", len(metafile), len(packed), 0, 0xFE) + packed, instance=0x3D4)


def xls_picture_store(*blips: bytes) -> bytes:
    """Workbook-global picture store (MSODRAWINGGROUP). `blips` are picture records made by `xls_png_blip` / `xls_emf_blip`."""
    entries = b""
    for blip in blips:
        header = struct.pack("<BB16sHIIIBBBB", 6, 6, bytes(16), 0xFF, len(blip), 1, 0, 0, 0, 0, 0)
        entries += escher_record(0xF007, header + blip, version=2, instance=6)
    store = escher_record(0xF001, entries, version=0xF, instance=len(blips))
    return xls_record(0x00EB, escher_record(0xF000, store, version=0xF))


# ---- Images ----------------------------------------------------------------------------------------------------

def png_bytes(width: int = 200, height: int = 120, *, seed: int = 0, image_format: str = "PNG",
              mode: str = "RGB") -> bytes:
    """Image whose pixels differ per seed (so that duplicate detection tells the images apart)."""
    image = Image.new(mode, (width, height), "white" if mode != "RGBA" else (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((5 + seed % 20, 5, width // 2 + seed, height - 5), fill=(30 + seed * 7 % 200, 90, 160))
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def docx_with_images(*, logo_count: int = 0) -> bytes:
    document = Document()
    document.add_paragraph("차트 앞 문단입니다.")
    document.add_picture(BytesIO(png_bytes(seed=1)), width=DocxInches(2))
    docpr = document.inline_shapes[0]._inline.docPr
    docpr.set("descr", "분기별 매출 차트")
    paragraph = document.add_paragraph("아이콘 ")
    paragraph.add_run().add_picture(BytesIO(png_bytes(16, 16, seed=2)))
    for _ in range(logo_count):
        document.add_paragraph("페이지").add_run().add_picture(BytesIO(png_bytes(seed=9)), width=DocxInches(1))

    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "사진"
    table.cell(0, 1).paragraphs[0].add_run().add_picture(BytesIO(png_bytes(seed=3)), width=DocxInches(1))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pptx_with_image() -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "구성도"
    slide.shapes.add_picture(BytesIO(png_bytes(seed=4)), Inches(1), Inches(2))
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def xlsx_with_image() -> bytes:
    from openpyxl.drawing.image import Image as SheetImage

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["항목", "값"])
    sheet.append(["a", 1])
    sheet.add_image(SheetImage(BytesIO(png_bytes(seed=5))), "D2")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def hwpx_with_images() -> bytes:
    body = (
        '<hp:p><hp:run><hp:t>그림 앞 문단</hp:t></hp:run></hp:p>'
        '<hp:p><hp:run><hp:pic><hp:shapeComment>그림입니다. 원본 그림의 이름: a.png</hp:shapeComment>'
        '<hc:img xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
        '<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
        + _hwpx_cell(0, 0, "도면")
        + '<hp:tc><hp:subList><hp:p><hp:run><hp:pic>'
        '<hc:img xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" binaryItemIDRef="image2"/></hp:pic>'
        '</hp:run></hp:p></hp:subList><hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
        "</hp:tr></hp:tbl></hp:run></hp:p>"
    )
    data = hwpx_bytes(body)
    source = zipfile.ZipFile(BytesIO(data))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in source.namelist():
            content = source.read(name)
            if name == "Contents/content.hpf":
                content = content.replace(
                    b"</opf:manifest>",
                    b'<opf:item id="image1" href="BinData/image1.png"/><opf:item id="image2" href="BinData/image2.bmp"/>'
                    b"</opf:manifest>",
                )
            archive.writestr(name, content)
        archive.writestr("BinData/image1.png", png_bytes(seed=6))
        archive.writestr("BinData/image2.bmp", png_bytes(seed=7, image_format="BMP"))
    return buffer.getvalue()


def pdf_with_images() -> bytes:
    """Page 1: text + picture + small icon; page 2: a scanned image page with no text."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    width, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("HYSMyeongJo-Medium", 11)
    pdf.drawString(72, height - 100, "그림 위 문단입니다")
    pdf.drawImage(ImageReader(BytesIO(png_bytes(400, 240, seed=8))), 72, height - 380, width=200, height=120)
    pdf.drawImage(ImageReader(BytesIO(png_bytes(40, 40, seed=10))), 400, height - 110, width=12, height=12)
    pdf.drawString(72, height - 450, "그림 아래 문단입니다")
    pdf.showPage()
    pdf.drawImage(ImageReader(BytesIO(png_bytes(600, 800, seed=11))), 0, 0, width=width, height=height)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def pdf_with_font_runs(lines: list[str]) -> bytes:
    """PDF that draws the text of one baseline as separate runs per font (mimicking Cairo-style generators): for each line all
    Hangul/symbol runs are drawn first in the Korean font, and the digit runs afterwards in another font. Each run's x is
    computed to match its visible position."""
    import re

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    korean, latin, size = "HYSMyeongJo-Medium", "Helvetica", 11
    _, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for number, line in enumerate(lines):
        x, runs = 72.0, []
        for run in re.findall(r"\d+|\D+", line):
            font = latin if run[0].isdigit() else korean
            runs.append((x, run, font))
            x += pdfmetrics.stringWidth(run, font, size)
        for font in (korean, latin):
            pdf.setFont(font, size)
            for x, run, run_font in runs:
                if run_font == font:
                    pdf.drawString(x, height - 100 - number * 16, run)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def pdf_scanned_in_strips(pages: list[list[str]], *, strips: int = 20, gap: float = 0.0) -> bytes:
    """PDF whose scanner stored each page as horizontal strips: on every page `strips` distinct image strips cover the page from
    top to bottom (`gap` pt between strips), with text-layer lines laid on top."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    width, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index, lines in enumerate(pages):
        strip_height = height / strips
        for strip in range(strips):
            picture = ImageReader(BytesIO(png_bytes(1240, 88, seed=100 + index * strips + strip)))
            pdf.drawImage(picture, 0, height - (strip + 1) * strip_height + gap / 2, width=width, height=strip_height - gap)
        pdf.setFont("HYSMyeongJo-Medium", 10)
        for row, line in enumerate(lines):
            pdf.drawString(72, height - 100 - row * 18, line)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def pdf_two_column_story(make_story: Callable[[str], list], *, header: Callable[[str], list] | None = None) -> bytes:
    """Two-column A4 PDF flowed with reportlab platypus. `make_story(font name)` builds the list of flowables for the columns
    (the Korean CID font HYSMyeongJo-Medium is registered beforehand). With `header`, a page-wide single-column frame is placed
    at the top of the first page and its flowables are flowed first."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import BaseDocTemplate, Frame, FrameBreak, NextPageTemplate, PageTemplate

    font = "HYSMyeongJo-Medium"
    pdfmetrics.registerFont(UnicodeCIDFont(font))
    width, height = A4
    column = (width - 120) / 2
    two = PageTemplate("two", [Frame(50, 50, column, height - 100), Frame(70 + column, 50, column, height - 100)])
    buffer = BytesIO()
    document = BaseDocTemplate(buffer, pagesize=A4)
    story = make_story(font)
    if header is None:
        document.addPageTemplates([two])
    else:
        top = PageTemplate("top", [Frame(50, height - 170, width - 100, 120), Frame(50, 50, column, height - 220),
                                   Frame(70 + column, 50, column, height - 220)])
        document.addPageTemplates([top, two])
        story = [*header(font), FrameBreak(), NextPageTemplate("two"), *story]
    document.build(story)
    return buffer.getvalue()


def raw_biff_xls(rows: list[list[str | float]], *, encoding: str = "cp949", codepage: int | None = None,
                 bof: tuple[int, int, int] = (0x0809, 0x0000, 0x0010)) -> bytes:
    """BIFF worksheet stream not wrapped in OLE (PHP xlsBOF-style "Excel download"). `bof` is (BOF record type, version,
    substream type).

    Text cells are 8-bit LABEL (0x0204, bytes in `encoding`), numeric cells are NUMBER (0x0203). Passing `codepage` writes a
    CODEPAGE record (such files usually do not have one).
    """
    record_id, version, kind = bof
    size = {0x0009: 4, 0x0209: 6, 0x0409: 6}.get(record_id, 8)  # BOF length for BIFF2, BIFF3, BIFF4, BIFF5 and later
    stream = xls_record(record_id, struct.pack("<HH", version, kind).ljust(size, b"\x00"))
    if codepage is not None:
        stream += xls_record(0x0042, struct.pack("<H", codepage))
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if isinstance(value, str):
                raw = value.encode(encoding)
                stream += xls_record(0x0204, struct.pack("<HHHH", row_index, col_index, 0, len(raw)) + raw)
            else:
                stream += xls_record(0x0203, struct.pack("<HHHd", row_index, col_index, 0, value))
    return stream + xls_record(0x000A)


def hwpx_with_picture(section_body: str, *, header_extra: str = "") -> bytes:
    """HWPX with one picture (manifest id `image1`) in BinData. The hc:img in `section_body` refers to that id.
    `header_extra` is inserted at the end of hh:refList in header.xml (paragraph numbering / paragraph shape definitions)."""
    edit = {"Contents/content.hpf": lambda xml: xml.replace(
        "</opf:manifest>", '<opf:item id="image1" href="BinData/image1.png"/></opf:manifest>')}
    if header_extra:
        edit["Contents/header.xml"] = lambda xml: xml.replace("</hh:refList>", header_extra + "</hh:refList>")
    return zip_with(hwpx_bytes(section_body), edit=edit, add={"BinData/image1.png": png_bytes(seed=11)})
