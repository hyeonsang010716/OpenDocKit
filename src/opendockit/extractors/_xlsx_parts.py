"""The parts of an XLSX package the converter reads itself, beside the cells openpyxl reads: the drawings of a sheet
(pictures, charts, SmartArt, text boxes), comments and their authors, form controls, pictures placed in cells, and the
relationships that lead to them.
"""

from collections import Counter
from collections.abc import Callable, Iterator
import html
import posixpath
import re
import zipfile

from lxml import etree
from openpyxl.utils.cell import coordinate_to_tuple

from opendockit.extractors._charts import package_loader, render_chart, render_chartex, render_smartart
from opendockit.extractors._openpyxl_reading import _READING, _CellPicture
from opendockit.extractors._package import _XML_PARSER, decimal_int, parse_part
from opendockit.extractors._sheet_layout import (
    _CHECKED,
    _UNCHECKED,
    _CellComment,
    _SheetRows,
)
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction


_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


_DETACHED_PARTS_WARNING = "Pictures and charts of a sheet that have no relationship information were left out."


_SPREADSHEET_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_STRICT_SPREADSHEET_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


def _workbook_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    """The workbook part the package relationships point to (usually `xl/workbook.xml`). Even under another name
    (`xl/workbook2.xml`) openpyxl follows this relationship to read it, so the resource checks must look at the same part
    (Pinned by: test_detect.py::TestRenamedMainPart)."""
    if "_rels/.rels" in names:
        try:
            root = etree.fromstring(archive.read("_rels/.rels"), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            return "xl/workbook.xml"
        for relationship in root.iter(_RELATIONSHIPS):
            if relationship.get("Type") == _OFFICE_DOCUMENT:
                return relationship.get("Target", "").lstrip("/")
    return "xl/workbook.xml"


_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"
_ANCHORS = frozenset(f"{{{_DRAWING}}}{name}" for name in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"))
_TEXT_BODY = f"{{{_DRAWING}}}txBody"
_ALTERNATE_CONTENT = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
_CHART_RENDERERS = {"{http://schemas.openxmlformats.org/drawingml/2006/chart}chart": render_chart,
                    "{http://schemas.microsoft.com/office/drawing/2014/chartex}chart": render_chartex}
# Prefix `_drawing_items` adds when it puts a chart kind and relationship id in the place of a picture relationship id
_CHART_REFERENCE = "\0chart:"
# SmartArt frame of a drawing part (dgm:relIds inside a graphicFrame). The data model part is the r:dm relationship. Passed on in the
# same slot as charts (`_chart_text`).
_DIAGRAM_RELATIONS = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds"
_DIAGRAM_DATA = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm"


def _drawing_blocks(archive: zipfile.ZipFile, names: set[str], sheet_path: str, pictures: dict[str, bytes],
                    extraction: Extraction, options: ConvertOptions, row_at: _SheetRows | None = None) -> list[str]:
    """Turn the pictures (chart images, screenshots), text boxes, shape text, charts and SmartArt of the sheet's drawing part, and
    the form controls of the VML drawing (`_form_controls`), into blocks ordered by where they are placed (top→bottom, left→right).
    They are not put inside the cell grid but after the table.

    Deliberate: even on a full load the openpyxl picture list (`sheet._images`) is not used; as when streaming, they are read
    directly from the ZIP (sheet → drawing → picture file relationships). openpyxl does not read text boxes and discards EMF/WMF
    pictures, so notes in a text box next to a table were dropped without a warning and vector pictures disappeared, on a full load
    only, with neither a placeholder nor a warning (Pinned by: test_spreadsheet.py::TestSheetDrawings,
    test_image.py::test_xlsx_pictures_are_extracted_even_when_streamed). The same picture file is kept in `pictures` and
    decompressed only once.
    Text box text counts toward the character limit, and processing stops there when it is exceeded. Cell comments
    (`_xlsx_comments`) are handled separately, and hyperlink addresses are not read.

    In a sheet with two or more pictures, each picture is preceded by the cell it is placed on and the text of that row
    (`(cell C2: 제품A)`) — the pictures of a list with a photo per row followed the table as nothing but `[Image]`, so it was
    impossible to tell which row a picture belonged to. A single picture (logo, stamp) gets none because its position carries no
    meaning, and when the same picture file is placed as many times as the repeated-image threshold
    (`repeated_image_threshold`) it gets none because it is removed as a repeated image (only the cell address remains). Known
    accepted gap: the cell mark in front of a picture removed for being small or decorative remains
    (Pinned by: test_spreadsheet_notes_pictures_and_times.py::TestFloatingPictureCells).
    """
    # (position, text, picture/chart relationship id, its relationships)
    items: list[tuple[tuple[int, int] | None, str, str, dict[str, str]]] = []
    for drawing in _related(archive, names, sheet_path, kind="/drawing").values():
        if drawing not in names:
            continue
        try:
            root = etree.fromstring(archive.read(drawing), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            continue  # openpyxl cannot read it either
        media = _part_relations(archive, names, drawing)
        items.extend((position, text, embed, media) for position, text, embed in _drawing_items(root, None))
    items.extend((position, text, "", {}) for position, text in _form_controls(archive, names, sheet_path))

    # Picture path → number of times placed on the sheet. Recounting the list for every picture took time quadratic in the number
    # of anchors (200,000 anchors: 40 seconds; Pinned by:
    # test_spreadsheet_notes_pictures_and_times.py::TestFloatingPictureCells::test_many_anchors_take_linear_time)
    placed = Counter(media[embed] for position, text, embed, media in items
                     if not text and not embed.startswith(_CHART_REFERENCE) and media.get(embed) in names)
    blocks: list[str] = []
    for position, text, embed, media in sorted(items, key=lambda item: item[0] or (0, 0)):
        target = embed
        if embed.startswith(_CHART_REFERENCE):
            kind, _, target = embed.removeprefix(_CHART_REFERENCE).partition("\0")
            path = media.get(target, "")
            text = _once_per_part(("chart", kind, path), lambda kind=kind, path=path: _chart_text(archive, names, kind, path))

        # Pictures and charts whose target part is missing are left out and reported. openpyxl does not follow drawing
        # relationships while reading (`_RELATIONS_LEFT_UNREAD`, Pinned by: test_spreadsheet.py::TestMissingPartRelationships)
        if target and not text and media.get(target) not in names and _DETACHED_PARTS_WARNING not in extraction.warnings:
            extraction.warnings.append(_DETACHED_PARTS_WARNING)

        if text:
            blocks.append(text)
            if extraction.count(solid_length(text), options):
                return blocks
        elif options.extract_images and media.get(embed) in names:
            path = media[embed]
            if path not in pictures:
                pictures[path] = archive.read(path)
            marker = extraction.add_image(pictures[path])
            labeled = (row_at is not None and position is not None and placed.total() > 1
                       and placed[path] < options.repeated_image_threshold)
            blocks.append(f"({row_at.place(position[0] + 1, position[1] + 1)}) {marker}" if labeled else marker)
    return blocks


_THREADED_COMMENTS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"


def _xlsx_people(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    """Threaded comment author id → name. Found in the workbook's person list (persons). Read once per workbook — reading it per
    sheet re-parsed the workbook relationships file, which has as many entries as there are sheets, and was quadratic in the number
    of sheets (3,200 sheets: 8.9 seconds; Pinned by:
    test_office_round43.py::TestManySheets::test_workbook_relationships_are_not_read_again_for_every_sheet)."""
    people = {}
    for path in _related(archive, names, _workbook_path(archive, names), kind="/person").values():
        root = parse_part(archive.read(path)) if path in names else None
        for person in root.iter(f"{{{_THREADED_COMMENTS}}}person") if root is not None else ():
            people[person.get("id", "")] = person.get("displayName", "")
    return people


def _xlsx_comments(archive: zipfile.ZipFile, names: set[str], sheet_path: str,
                   people: dict[str, str]) -> tuple[list[_CellComment], bool]:
    """Cell comments of a sheet. Excel 365 threaded comments (threadedComments + the workbook's persons, `_xlsx_people`) are read
    together with their replies, and the legacy-format copy on the same cell (author `tc={id}`, body a '[Threaded comment] …'
    notice) is left out. Legacy comments (comments.xml) are an author list + cell addresses.
    A corrupted part only has its own comments skipped and is reported through the second value (whether there was a corrupted
    part) — comments used to be dropped without a warning (Pinned by: test_office_round44.py::TestBrokenCommentPart)."""
    found: list[_CellComment] = []
    threaded_cells: set[tuple[int, int]] = set()
    damaged = False

    # Threaded comments: one with a parent is a reply to that comment
    for path in _related(archive, names, sheet_path, kind="/threadedComment").values():
        root = parse_part(archive.read(path)) if path in names else None
        damaged = damaged or (path in names and root is None)
        threads: dict[str, _CellComment] = {}
        for comment in root.iter(f"{{{_THREADED_COMMENTS}}}threadedComment") if root is not None else ():
            cell = _cell_address(comment.get("ref", ""))
            text = comment.findtext(f"{{{_THREADED_COMMENTS}}}text") or ""
            author = people.get(comment.get("personId", ""), "")
            parent = threads.get(comment.get("parentId") or "")
            if parent is not None:
                parent[4].append((author, text))
            elif cell is not None:
                threads[comment.get("id", "")] = (*cell, author, text, [])
                threaded_cells.add(cell)
        found.extend(threads.values())

    # Legacy comments. Copies on cells that have a threaded comment are left out
    for path in _related(archive, names, sheet_path, kind="/comments").values():
        root = parse_part(archive.read(path)) if path in names else None
        if root is None:
            damaged = damaged or path in names
            continue
        authors = [" ".join("".join(author.itertext()).split()) for author in root.iter(f"{{{_SPREADSHEET_MAIN}}}author")]
        for comment in root.iter(f"{{{_SPREADSHEET_MAIN}}}comment"):
            cell = _cell_address(comment.get("ref", ""))
            author_id = decimal_int(comment.get("authorId", ""), -1)
            author = authors[author_id] if 0 <= author_id < len(authors) else ""
            if cell is None or cell in threaded_cells or author.startswith("tc="):
                continue
            body = comment.find(f"{{{_SPREADSHEET_MAIN}}}text")
            text = "".join(node.text or "" for node in body.iter(f"{{{_SPREADSHEET_MAIN}}}t")) if body is not None else ""
            found.append((*cell, author, text, []))

    return found, damaged


def _cell_address(reference: str) -> tuple[int, int] | None:
    """`E5` → (5, 5). A range (`E5:F6`) gives its top-left cell. None if it is not an address."""
    try:
        row, column = coordinate_to_tuple(reference.split(":")[0].replace("$", "").strip().upper())
    except (ValueError, TypeError, IndexError):
        return None
    return row, column


# Form controls placed in the sheet's VML drawing (legacyDrawing) that have a selected state (check boxes, option buttons). Cell
# comments (Note) are handled separately through comments.xml (`_xlsx_comments`); buttons and lists are not read.
_VML = "urn:schemas-microsoft-com:vml"
_VML_OFFICE = "urn:schemas-microsoft-com:office:office"
_VML_EXCEL = "urn:schemas-microsoft-com:office:excel"
_FORM_CONTROL_TYPES = frozenset(("checkbox", "radio"))


# Excel's VML sometimes uses unclosed tags (<br>) as HTML does. Broken parts are skipped while reading.
_VML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
# Unclosed void HTML tags. For every unmatched closing tag the parser pushed elements one level, so x:ClientData ended up inside
# v:textbox and the check box was lost.
# `<` is not allowed in the attribute part — each `<br` continuing without a `>` was scanned to the end, and a 6KB document took a
# minute
_VML_VOID_TAG = re.compile(rb"<((?:[\w.-]+:)?(?:br|hr|img|wbr))\b([^<>]*?)(?<!/)>", re.IGNORECASE)
# The inside of a label text box (v:textbox) is an HTML fragment (`<br></br>`, unclosed `<p>`, `&nbsp;`), so reading it as XML shifts
# the shape structure. The tags are stripped and only the text is kept
_VML_TEXT_BOX_OPEN = re.compile(rb"<(?:[\w.-]+:)?textbox\b[^<>]*>", re.IGNORECASE)
_VML_TEXT_BOX_CLOSE = re.compile(rb"</(?:[\w.-]+:)?textbox\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(rb"<[^<>]*>")
_HTML_NAMED_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]{0,31});")
_XML_ENTITY_NAMES = frozenset((b"amp", b"lt", b"gt", b"quot", b"apos"))
_VML_SHAPE_NUMBER = re.compile(r"_s(\d+)$")
_CONTROL_ELEMENT = re.compile(rb"<(?:[\w.-]+:)?control\b[^<>]*>")
_CONTROL_SHAPE_ID = re.compile(rb"\sshapeId\s*=\s*[\"']\s*(\d+)")
_CONTROL_RELATIONSHIP = re.compile(rb"\s[\w.-]+:id\s*=\s*[\"']([^\"']*)")


def _form_controls(archive: zipfile.ZipFile, names: set[str], sheet_path: str) -> list[tuple[tuple[int, int], str]]:
    """Form control check boxes and option buttons → (row/column where placed, '☒ label' / '☐ label').

    The label and the state are in the sheet's VML drawing (x:ClientData ObjectType="Checkbox"/"Radio", state x:Checked). Excel 2010
    and later also write the same state in the control properties part (checked="Checked" in ctrlProps/ctrlProp*.xml) and link it
    to the VML shape through the sheet's `<control shapeId=…>`. If either of the two says selected, it is ☒. Both the label and the
    state were dropped, so it was impossible to tell which of '동의함' (agree) / '동의하지 않음' (do not agree) had been chosen on a
    consent form (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestFormControlCheckBoxes).
    """
    controls: list[tuple[tuple[int, int], str]] = []
    checked_shapes = _checked_control_shapes(archive, names, sheet_path)
    for vml in _related(archive, names, sheet_path, kind="/vmlDrawing").values():
        root = _vml_root(archive.read(vml)) if vml in names else None
        for shape in root.iter(f"{{{_VML}}}shape") if root is not None else ():
            data = shape.find(f"{{{_VML_EXCEL}}}ClientData")
            if data is None or (data.get("ObjectType") or "").lower() not in _FORM_CONTROL_TYPES:
                continue
            box = shape.find(f"{{{_VML}}}textbox")
            label = " ".join("".join(box.itertext()).split()) if box is not None else ""
            number = next((match.group(1) for name in (f"{{{_VML_OFFICE}}}spid", "id")
                           if (match := _VML_SHAPE_NUMBER.search(shape.get(name) or ""))), None)
            checked = (data.findtext(f"{{{_VML_EXCEL}}}Checked") or "").strip() == "1" or number in checked_shapes
            anchor = [part.strip() for part in (data.findtext(f"{{{_VML_EXCEL}}}Anchor") or "").split(",")]
            position = (decimal_int(anchor[2], 0), decimal_int(anchor[0], 0)) if len(anchor) > 2 else (0, 0)
            controls.append((position, f"{_CHECKED if checked else _UNCHECKED} {label}".strip()))
    return controls


def _vml_root(xml: bytes) -> etree._Element | None:
    """Root of the VML drawing part. Unclosed `<br>` and the like are closed before reading. None if it cannot be read (empty part,
    garbage bytes) — check boxes are an extra, so the document is not rejected because of them
    (Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedFormControlDrawing)."""
    try:
        return etree.fromstring(_VML_VOID_TAG.sub(rb"<\1\2/>", _plain_text_boxes(xml)), parser=_VML_PARSER)
    except etree.XMLSyntaxError:
        return None


def _plain_text_boxes(xml: bytes) -> bytes:
    """Strip the HTML tags inside VML label text boxes and turn HTML named character references (`&nbsp;`) into numeric references.
    If a closing text box tag is missing, everything after that point is left as is
    (Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedFormControlDrawing::test_html_shaped_label_keeps_both_check_boxes)."""
    parts, position = [], 0

    for opening in _VML_TEXT_BOX_OPEN.finditer(xml):
        if opening.start() < position:
            continue
        closing = _VML_TEXT_BOX_CLOSE.search(xml, opening.end())
        if closing is None:
            break
        inner = _HTML_TAG.sub(b"", xml[opening.end():closing.start()])
        parts += [xml[position:opening.end()], _HTML_NAMED_ENTITY.sub(_numeric_entity, inner)]
        position = closing.start()
    parts.append(xml[position:])
    return b"".join(parts)


def _numeric_entity(match: re.Match[bytes]) -> bytes:
    name = match.group(1)
    if name in _XML_ENTITY_NAMES:
        return match.group()
    text = html.unescape(match.group().decode("ascii"))
    return match.group() if text.startswith("&") else "".join(f"&#{ord(character)};" for character in text).encode()


def _checked_control_shapes(archive: zipfile.ZipFile, names: set[str], sheet_path: str) -> set[str]:
    """VML shape numbers (shapeId) of the controls whose control properties part says selected (checked="Checked"). The sheet XML
    is scanned only for sheets that have control properties relationships (`<control>` sits near the end of the sheet, but the
    sheet is not parsed as XML again; only the elements are searched for)."""
    properties = _related(archive, names, sheet_path, kind="/ctrlProp")
    if not properties or sheet_path not in names:
        return set()

    def says_checked(path: str) -> bool:
        root = parse_part(archive.read(path))
        return root is not None and root.get("checked") == "Checked"

    checked = set()
    for element in _CONTROL_ELEMENT.finditer(archive.read(sheet_path)):
        shape, relationship = _CONTROL_SHAPE_ID.search(element.group()), _CONTROL_RELATIONSHIP.search(element.group())
        path = properties.get(relationship.group(1).decode("utf-8", errors="replace")) if relationship else None
        if shape and path in names and _once_per_part(("checked", path), lambda path=path: says_checked(path)):
            checked.add(shape.group(1).decode())
    return checked


def _once_per_part[T](key: tuple[str, ...], read: Callable[[], T]) -> T:
    """What `read` works out from a part, worked out once per workbook (`key`: what is read, and the part path). A part was read
    and parsed again for every reference to it: 1,000 `<control>` elements pointing at one 2 MB control properties part (a 7 KB
    file) took 2.2 s, and so did a chart placed many times. The results live in the reading state of this conversion
    (`_CellReading`); outside one, nothing is kept (Pinned by: test_audit_spreadsheet.py::TestPartsReferencedManyTimes)."""
    reading = _READING.get()
    if reading is None:
        return read()
    if key not in reading.part_results:
        reading.part_results[key] = read()
    return reading.part_results[key]


def _drawing_items(element: etree._Element,
                   position: tuple[int, int] | None) -> Iterator[tuple[tuple[int, int] | None, str, str]]:
    """Turn the text box/shape text and picture references of drawing XML into (row/column where placed, text, picture relationship
    id). Group shapes are descended into as well.

    For `mc:AlternateContent`, mc:Choice is read first and mc:Fallback only when that yields nothing (they are alternative
    representations of the same shape, so reading both would emit it twice — same rule as PPTX).
    """
    for child in element:
        if child.tag in _ANCHORS:
            yield from _drawing_items(child, _anchor_position(child))
        elif child.tag == _ALTERNATE_CONTENT:
            for branch in child:
                items = list(_drawing_items(branch, position))
                if items:
                    yield from items
                    break
        elif child.tag == _TEXT_BODY:
            text = _text_body(child)
            if text:
                yield position, text, ""
        elif child.tag == _BLIP:
            yield position, "", child.get(_EMBED, "")
        elif child.tag in _CHART_RENDERERS:
            yield position, "", f"{_CHART_REFERENCE}{child.tag}\0{child.get(_RELATIONSHIP_ID, '')}"
        elif child.tag == _DIAGRAM_RELATIONS:
            yield position, "", f"{_CHART_REFERENCE}{child.tag}\0{child.get(_DIAGRAM_DATA, '')}"
        elif isinstance(child.tag, str):
            yield from _drawing_items(child, position)


def _chart_text(archive: zipfile.ZipFile, names: set[str], kind: str, path: str) -> str:
    """A chart placed on a sheet → "[Chart] title" + a table of the stored values (the same `_charts.render_chart` as PPTX/DOCX).
    For a chart that stores no values (one made by openpyxl), the header-only table is dropped and only the title is kept.

    Charts disappeared without a trace, so a dashboard sheet consisting only of charts was left with just the sheet title. The
    source table of a chart is often on a hidden sheet, so the values stored in the chart are carried over
    (Pinned by: test_spreadsheet.py::TestSheetCharts). Text boxes inside a chart (c:userShapes — '(단위: 억원)') are found through
    the chart part's relationships (same as DOCX/PPTX/HWPX. Pinned by: test_core_spreadsheet.py::TestSheetChartUserShapes).
    """
    root = parse_part(archive.read(path)) if path in names else None
    if root is None:
        return ""
    if kind == _DIAGRAM_RELATIONS:
        # SmartArt placed on a sheet (organisation chart, process diagram) becomes the same nested list as in PPTX/DOCX (Pinned by:
        # test_spreadsheet_cell_values_and_controls.py::TestSmartArtOnASheet)
        return render_smartart(root)
    if _CHART_RENDERERS[kind] is render_chart:
        rendered = render_chart(root, load_related=package_loader(lambda name: archive.read(name) if name in names else None,
                                                                  path))
    else:
        rendered = _CHART_RENDERERS[kind](root)
    parts = rendered.split("\n\n")
    return "\n\n".join(part for part in parts if not (part.startswith("|") and part.count("\n") < 2))


def _anchor_position(anchor: etree._Element) -> tuple[int, int] | None:
    """Top-left cell of a drawing object (row, column, 0-based). An object not tied to a cell (absoluteAnchor) gives None — it is
    treated as being at the very top."""
    try:
        return int(anchor.findtext(f"{{{_DRAWING}}}from/{{{_DRAWING}}}row")), int(anchor.findtext(f"{{{_DRAWING}}}from/{{{_DRAWING}}}col"))
    except (TypeError, ValueError):
        return None


def _text_body(body: etree._Element) -> str:
    """Text of a text box or shape. One line per paragraph; a line break inside a paragraph (a:br) is a line break too."""
    lines = []
    for paragraph in body.iter(f"{{{_DRAWING_MAIN}}}p"):
        pieces = [(node.text or "") if node.tag == f"{{{_DRAWING_MAIN}}}t" else "\n"
                  for node in paragraph.iter(f"{{{_DRAWING_MAIN}}}t", f"{{{_DRAWING_MAIN}}}br")]
        lines.append("".join(pieces).strip())
    return "\n".join(line for line in lines if line)


_RICH_DATA = "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
_RICH_VALUE_RELS = "http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel"
_RICH_PICTURE_KEY = "_rvRel:LocalImageIdentifier"


def _rich_values(archive: zipfile.ZipFile, names: set[str]) -> dict[str, object]:
    """A cell's vm (value metadata index, 1-based) → the picture of a 'Place in Cell' picture cell (`_CellPicture`) or the displayed
    text of a data type cell (_DisplayString).

    Excel 365 writes these cells in the sheet XML as `t="e" vm="N"` + `#VALUE!` and keeps the actual value in parts linked through
    the workbook's relationships: value metadata (metadata.xml: vm → rvb index of XLRICHVALUE) → rich value (rv in
    rdrichvalue.xml, key order of the structure in rdrichvaluestructure.xml) → picture number → relationship in richValueRel.xml →
    picture file. If a part is missing or corrupted the result is an empty dict (the cell is blank + a warning,
    `_tolerant_parse_cell`).
    Known accepted gap: web pictures (_webimage) and other structures are not read
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestPicturesAndDataTypesPlacedInCells).
    """
    workbook_path = _workbook_path(archive, names)
    parts = {}
    for kind in ("/sheetMetadata", "/rdRichValue", "/rdRichValueStructure", "/richValueRel"):
        targets = [path for path in _related(archive, names, workbook_path, kind=kind).values() if path in names]
        parts[kind] = targets[0] if targets else None
    if parts["/sheetMetadata"] is None or parts["/rdRichValue"] is None:
        return {}

    try:
        metadata = etree.fromstring(archive.read(parts["/sheetMetadata"]), parser=_XML_PARSER)
        values = etree.fromstring(archive.read(parts["/rdRichValue"]), parser=_XML_PARSER)
        structures = (etree.fromstring(archive.read(parts["/rdRichValueStructure"]), parser=_XML_PARSER)
                      if parts["/rdRichValueStructure"] else None)
        relations = (etree.fromstring(archive.read(parts["/richValueRel"]), parser=_XML_PARSER)
                     if parts["/richValueRel"] else None)
    except etree.XMLSyntaxError:
        return {}

    # For each rich value, a picture (picture number → relationship → file) or the displayed text. The structure (list of key names)
    # and the values (v) are paired by order
    keys = [[key.get("n", "") for key in structure.iterchildren(f"{{{_RICH_DATA}}}k")]
            for structure in (structures.iterchildren(f"{{{_RICH_DATA}}}s") if structures is not None else ())]
    # Relationships that cannot be read are treated as absent (`_part_relations`): the pictures are left blank with the warning
    # and the data types are kept. They rejected the whole workbook
    # (Pinned by: test_audit_spreadsheet.py::TestDamagedRichValueRelationships).
    targets = _part_relations(archive, names, parts["/richValueRel"]) if relations is not None else {}
    pictures = [targets.get(relation.get(_RELATIONSHIP_ID, ""), "")
                for relation in (relations.iterchildren(f"{{{_RICH_VALUE_RELS}}}rel") if relations is not None else ())]
    rich: list[object] = []
    for value in values.iterchildren(f"{{{_RICH_DATA}}}rv"):
        structure = _list_item(keys, value.get("s")) or []
        fields = dict(zip(structure, (item.text or "" for item in value.iterchildren(f"{{{_RICH_DATA}}}v")), strict=False))
        picture = _list_item(pictures, fields.get(_RICH_PICTURE_KEY))
        rich.append(_CellPicture(picture, fields.get("Text") or None) if picture else fields.get("_DisplayString"))

    # The cell's vm-th value metadata → metadata type (1-based) and the future metadata index of that type → rich value index of rvb
    main = f"{{{_SPREADSHEET_MAIN}}}"
    types = [None, *(kind.get("name") for kind in metadata.iterfind(f"{main}metadataTypes/{main}metadataType"))]
    blocks = {future.get("name"): [next((item.get("i") for item in block.iter(f"{{{_RICH_DATA}}}rvb")), None)
                                   for block in future.iterchildren(f"{main}bk")]
              for future in metadata.iterchildren(f"{main}futureMetadata")}
    found: dict[str, object] = {}
    for number, block in enumerate(metadata.iterfind(f"{main}valueMetadata/{main}bk"), start=1):
        record = block.find(f"{main}rc")
        if record is None:
            continue
        index = _list_item(blocks.get(_list_item(types, record.get("t")), []), record.get("v"))
        value = _list_item(rich, index)
        if value is not None:
            found[str(number)] = value
    return found


def _list_item[T](items: list[T], index: str | None) -> T | None:
    """`items[int(index)]`. None if the number is not decimal (including a very long number) or is out of range."""
    number = decimal_int(index.strip() if index is not None else None, None)
    return items[number] if number is not None and number < len(items) else None


def _cell_picture_text(picture: _CellPicture, archive: zipfile.ZipFile, names: set[str], pictures: dict[str, bytes],
                       extraction: Extraction, options: ConvertOptions) -> str:
    """A picture in a cell → the placeholder to put in that cell. Blank if pictures are not extracted by the options or the picture
    file is missing. The same picture file is decompressed only once."""
    if not options.extract_images or picture.path not in names:
        return ""
    if picture.path not in pictures:
        pictures[picture.path] = archive.read(picture.path)
    return extraction.add_image(pictures[picture.path], alt=picture.alt)


def _picture_reference_bytes(archive: zipfile.ZipFile) -> int:
    """The amount an openpyxl full load actually decompresses because of pictures: for every picture reference (`r:embed`) in the
    drawing XML the sheets point to through relationships, add the uncompressed size of that picture file.

    Counted the way openpyxl does it, by following the sheet relationships and parsing the XML. Counting with regular expressions
    or fixed paths could be evaded with single quotes, whitespace, character references or drawing files at other paths.
    """
    names = set(archive.namelist())
    sizes = {info.filename: info.file_size for info in archive.infolist()}
    sheets = {name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")}
    sheets |= {path for path in _related(archive, names, _workbook_path(archive, names)).values() if path.endswith(".xml")}

    total = 0
    for sheet in sheets & names:
        # Parts the workbook points to that are not sheets are looked at too, and openpyxl never reads their relationships: ones
        # that cannot be read count nothing (Pinned by: test_audit_spreadsheet.py::TestDamagedRichValueRelationships)
        for part in _part_relations(archive, names, sheet).values():
            if part == sheet or part not in names or not part.endswith(".xml"):
                continue
            media = _part_relations(archive, names, part)
            try:
                root = etree.fromstring(archive.read(part), parser=_XML_PARSER)
            except etree.XMLSyntaxError:
                continue  # openpyxl cannot read it either
            total += sum(sizes.get(media.get(element.get(_EMBED), ""), 0) for element in root.iter()
                         if element.get(_EMBED) is not None)
    return total


def _related(archive: zipfile.ZipFile, names: set[str], source: str, kind: str | None = None) -> dict[str, str]:
    """Relationship id of the source part → target part path. With `kind`, only relationships whose Type ends with it."""
    folder, name = posixpath.split(source)
    rels_path = f"{folder}/_rels/{name}.rels"
    if rels_path not in names:
        return {}

    root = etree.fromstring(archive.read(rels_path), parser=_XML_PARSER)
    targets = {}
    for relationship in root.iter(_RELATIONSHIPS):
        if kind is not None and not relationship.get("Type", "").endswith(kind):
            continue
        target = relationship.get("Target", "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        targets[relationship.get("Id", "")] = path
    return targets


def _part_relations(archive: zipfile.ZipFile, names: set[str], part: str) -> dict[str, str]:
    """Relationships of a part only the converter reads, such as a drawing — if the relationships file is corrupted they are treated
    as absent. Only that part's pictures and charts are lost (`_drawing_blocks` warns; text boxes remain) and the whole workbook is
    not rejected. Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedDrawingRelationships"""
    try:
        return _related(archive, names, part)
    except etree.XMLSyntaxError:
        return {}
