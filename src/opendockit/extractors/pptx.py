"""PPTX → per-slide Markdown.

Shapes are read by **on-screen position (top → bottom, left → right)**, not in XML order (= creation order). That is the order in which
a person reads a slide.
"""

from collections.abc import Callable
from dataclasses import dataclass
import re

from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.exc import InvalidXmlError, PackageNotFoundError
from pptx.opc.package import PartFactory
from pptx.package import Package
from pptx.parts.presentation import PresentationPart
from pptx.shapes.group import GroupShape
from pptx.shapes.placeholder import LayoutPlaceholder, PlaceholderPicture, _BaseSlidePlaceholder, _InheritsDimensions
from pptx.table import _Row

from opendockit.exceptions import CorruptedDocumentError, UnsupportedFormatError
from opendockit.extractors._shared import (
    HAND_TYPED_BULLET,
    LINK_OPEN,
    MAX_LEVEL,
    OLE_PLACEHOLDER,
    SUBSCRIPT,
    SUPERSCRIPT,
    SYMBOL_GLYPHS,
    cell_level_mark,
    comment_line,
    core_properties,
    decimal_int,
    format_number,
    is_strict_package,
    link_close,
    link_target,
    list_item_prefix,
    mark_emphasis,
    mark_script,
    mark_strike,
    merge_emphasis_marks,
    omitted_rows_warning,
    open_package,
    parse_part,
    related_loader,
    render_chart,
    render_chartex,
    render_omml,
    render_smartart,
    resolve_inline,
    resolve_scripts,
    resolve_strikes,
    symbol_font_text,
    unread_ole_warning,
)
from opendockit.markdown import TableGrid, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# Decorative placeholders repeated on every slide. Not content.
_SKIP_PLACEHOLDERS = {PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.HEADER}
_TITLE_PLACEHOLDERS = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
_BULLET_PLACEHOLDERS = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}
# Hand-typed bullets that make a line joined by a line break (a:br) inside a list-item paragraph an item one level down: '- ', '* ', '1. ',
# '1) ' (numbers up to two digits — a date line such as '2024. 3.' is not one. '12. 31. 기준' (as of Dec 31), where another 'N.' follows,
# is a date too). Other bullet characters are in `HAND_TYPED_BULLET`. Markdown does not read a '1) ' line as a list, so its indentation
# would be removed; it is emitted as '- 1) ' (same shape as `_shared.list_item_prefix`.
# Pinned by: test_office_symbols_fields_notes.py::TestPptxHandTypedLinesInsideAnItem).
_HAND_TYPED_ITEM = re.compile(r"(?:[-*]|\d{1,2}([.)]))\s(?!\s*\d{1,2}\.(?:\s|$))")
# Vertical tolerance (EMU) for treating shapes as the same row. About 0.2 cm.
_ROW_TOLERANCE = 72_000
_CHARTEX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MARKUP_COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
_VML = "urn:schemas-microsoft-com:vml"
_OFFICE = "urn:schemas-microsoft-com:office:office"
_SHAPE_CONTAINERS = {f"{{{_PRESENTATION}}}{name}" for name in ("spTree", "grpSp")}
# Run font elements. The a:sym font decides what a symbol-font character means (`_symbol_font`).
_RUN_FONT_ELEMENTS = ("sym", "latin", "ea", "cs")
# Run strikethrough (a:rPr strike). "noStrike" is not a strikethrough.
_STRIKES = {"sngStrike", "dblStrike"}
# Hidden slide (p:sld show). Booleans come in both spellings, 0/1 and false/true.
_HIDDEN = {"0", "false"}
_TRUE = {"1", "true"}
# Values allowed as a placeholder type (p:ph type). An unknown value is a ValueError the moment python-pptx builds the shape
# (`_drop_unreadable_shapes`).
_PLACEHOLDER_TYPES = {member.xml_value for member in PP_PLACEHOLDER if member.xml_value}
_SHAPE_TAGS = {f"{{{_PRESENTATION}}}{name}" for name in ("sp", "pic", "graphicFrame", "grpSp", "cxnSp")}
# Bullet kind of a paragraph (inside a:pPr / a:lvlNpPr). The first one found is the paragraph's bullet.
_BULLET_TAGS = {f"{{{_DRAWING}}}{name}" for name in ("buNone", "buAutoNum", "buChar", "buBlip")}
_VISIBLE_BULLETS = {f"{{{_DRAWING}}}{name}" for name in ("buChar", "buBlip")}
# Auto-number scheme (a:buAutoNum type) = number kind + decoration. Kinds are matched from the front; an unknown kind (Thai letters etc.)
# is Arabic digits.
_AUTO_NUMBER_KINDS = (("alphaLc", "LATIN_SMALL"), ("alphaUc", "LATIN_CAPITAL"), ("romanLc", "ROMAN_SMALL"),
                      ("romanUc", "ROMAN_CAPITAL"), ("circleNumWdBlack", "BLACK_CIRCLED_DIGIT"), ("circleNum", "CIRCLED_DIGIT"),
                      ("ea1", "IDEOGRAPH"))
_AUTO_NUMBER_DECORATIONS = (("ParenBoth", "({})"), ("ParenR", "{})"), ("Period", "{}."), ("1Minus", "{}-"),
                            ("2Minus", "-{}-"))
_BLACK_CIRCLED = "❶❷❸❹❺❻❼❽❾❿⓫⓬⓭⓮⓯⓰⓱⓲⓳⓴"
# Tolerance for horizontal position/width, and the vertical gap (EMU), for placeholders stacked in the same column. About 0.5 cm and 1 cm.
_COLUMN_TOLERANCE = 180_000
_STACK_GAP = 360_000
# A macro-enabled show (.ppsm) and a macro-enabled template (.potm) differ from .pptx only in the content type name of the main part; the
# structure is the same. python-pptx reads only the .pptx, .pptm, .ppsx and .potx names as a presentation part, so the others are
# registered with the same class (the same method as the DOCX registration of .docm, .dotx etc.; it is a process-wide registration —
# deliberate). Pinned by: test_office.py::TestOfficeReadingOrderAndListMarks::
# test_macro_enabled_show_and_template_are_read_like_a_plain_deck.
for _content_type in ("application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
                      "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"):
    PartFactory.part_type_for[_content_type] = PresentationPart


@dataclass(frozen=True, slots=True)
class _Inline:
    """What is kept inside a paragraph besides its text (`ConvertOptions.keep_links`, `keep_emphasis`): `link` gives the vetted
    address of a run's a:hlinkClick (None when links are not kept), `emphasis` says whether bold / italic runs are marked."""

    link: Callable[[object], str | None] | None
    emphasis: bool


def _inline_of(shape: object, options: ConvertOptions) -> _Inline | None:
    """Links and emphasis to keep in the text of `shape`. None when both options are off — the text is read exactly as before."""
    if not (options.keep_links or options.keep_emphasis):
        return None
    part = shape.part

    def link(click: object) -> str | None:
        # Only a link out of the deck: an external relationship of the slide. A jump to another slide (`ppaction://hlinksldjump`)
        # has an internal relationship or none, and a broken relationship id is not a link either.
        relation = click.get(f"{{{_RELATIONSHIPS}}}id")
        relationship = part.rels.get(relation) if relation else None
        if relationship is None or not relationship.is_external:
            return None
        return link_target(relationship.target_ref)

    return _Inline(link if options.keep_links else None, options.keep_emphasis)


def _reject_strict(data: bytes) -> None:
    if is_strict_package(data):
        raise UnsupportedFormatError("PowerPoint documents in the Strict Open XML format are not supported. "
                                     "Please save the file again in PowerPoint as 'PowerPoint Presentation (.pptx)'.")


def extract_pptx(data: bytes, options: ConvertOptions) -> Extraction:
    # `pptx.Presentation()` accepts only a main part whose content type name is .pptx or .pptm. Shows (.ppsx, .ppsm) and templates
    # (.potx, .potm) differ only in name and have the same structure, so the package is opened directly and only checked for being a
    # presentation part (Pinned by: test_office.py::test_slide_show_deck_is_read_like_a_plain_one).
    # If opening fails because of one part that is missing or is not XML, only that part is detached and the package is opened again
    # (`_shared.open_package`, one warning).
    # A Strict Open XML document fails here because its main part is not found. It is not corruption; the message asks to save it again
    # (`_shared.is_strict_package`).
    extraction = Extraction()
    try:
        part = open_package(Package.open, data, extraction.warnings).main_document_part
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        _reject_strict(data)
        raise CorruptedDocumentError("The PPTX document structure could not be read.") from exc
    if not isinstance(part, PresentationPart):
        _reject_strict(data)
        raise CorruptedDocumentError("The PPTX document structure could not be read.")

    # The slide list (p:sldIdLst) is walked directly so that an entry whose part is missing (KeyError) or that points at a part that is
    # not a slide (AttributeError) skips only that slide. `presentation.slides` looked up the part of every entry at construction
    # (rename_slide_parts) and raised right there, and so did iteration, so the remaining slides were lost as well
    # (Pinned by: test_office.py::TestOneBrokenPartDoesNotRejectTheDocument::test_pptx_slide_whose_part_cannot_be_read_is_skipped_with_a_warning).
    # A main part whose root is not p:presentation is not a deck (asking it for the slide list raised AttributeError. Pinned by:
    # test_audit_office.py::TestPptxSlideListEntryWithoutARelationship::test_root_that_is_not_a_presentation_gets_the_structure_error).
    if part._element.tag != f"{{{_PRESENTATION}}}presentation":
        _reject_strict(data)
        raise CorruptedDocumentError("The PPTX document structure could not be read.")
    presentation = part.presentation
    slide_ids = presentation._element.sldIdLst
    # OLE objects left as a bare placeholder with no preview picture. Their count is reported as a warning once everything is read
    unread_ole: list[object] = []
    authors = _comment_authors(part)
    for index, slide_id in enumerate(slide_ids.sldId_lst if slide_ids is not None else [], start=1):
        # An entry without r:id is an InvalidXmlError (Pinned by: test_audit_office.py::
        # TestPptxSlideListEntryWithoutARelationship::test_entry_without_a_relationship_id_skips_only_that_slide).
        try:
            slide = part.related_slide(slide_id.rId)
        except (KeyError, AttributeError, InvalidXmlError):
            extraction.warnings.append(f"Slide {index}: skipped because its part could not be read.")
            continue
        # A slide whose root is not p:sld (`p:sldX`) rejected the whole document with an AttributeError when asked for its shape list.
        # Errors of one shape or one set of notes are caught separately inside, so what reaches here is a slide that cannot be read at
        # all (Pinned by:
        # test_office_robustness.py::TestDocumentPropertiesAndSlideRoots::test_slide_with_an_odd_root_is_skipped_with_a_warning).
        try:
            section = _render_slide(slide, index, options, extraction, unread_ole, authors)
        except (AttributeError, KeyError, ValueError, InvalidXmlError):
            extraction.warnings.append(f"Slide {index}: skipped because the slide could not be read.")
            continue
        extraction.sections.append(section)
        if extraction.count(solid_length(section.text), options):
            break

    if unread_ole:
        extraction.warnings.append(unread_ole_warning(len(unread_ole)))
    extraction.metadata["slide_count"] = str(len(extraction.sections))
    extraction.metadata.update(core_properties(part.package))
    return extraction


def _unwrap_alternate_content(tree: object, part: object) -> None:
    """Replace an `mc:AlternateContent` sitting in a shape slot with the shapes inside it (only in the in-memory copy used while reading).

    PowerPoint wraps a shape that uses a newer feature, such as a text box holding an equation, in `mc:Choice` (new version) +
    `mc:Fallback` (substitute picture or shape). python-pptx does not count this wrapper as a shape, so the text was dropped entirely.
    The shapes of the new version are used; if none of them is a known shape, the fallback is used
    (Pinned by: test_office.py::TestReportedGaps::test_pptx_shape_wrapped_in_alternate_content_is_read).

    For the new chart kinds (chartex: waterfall, treemap …) the fallback (a pre-rendered picture) is used when the values cannot be
    read. The frame of the new version was taken as a known shape and the picture discarded, so the chart vanished with neither data nor
    picture. If there is no fallback either, the frame is kept and a `[Chart]` placeholder is emitted
    (Pinned by: test_office.py::TestPptxLayoutAndEmbeddedObjects::test_chartex_without_readable_data_falls_back_to_its_picture).
    """
    for alternate in list(tree.iter(f"{{{_MARKUP_COMPATIBILITY}}}AlternateContent")):
        parent = alternate.getparent()
        if parent is None or parent.tag not in _SHAPE_CONTAINERS:
            continue
        choice, fallback = _branch_shapes(alternate, "Choice"), _branch_shapes(alternate, "Fallback")
        charts = [shape for shape in choice if _has_chartex(shape)]
        is_unreadable_chart = bool(charts) and not any(_chartex_text(shape, part) for shape in charts)
        shapes = fallback if fallback and (not choice or is_unreadable_chart) else choice
        position = parent.index(alternate)
        parent.remove(alternate)
        for offset, shape in enumerate(shapes):
            parent.insert(position + offset, shape)


def _branch_shapes(alternate: object, name: str) -> list[object]:
    """Shapes in one branch (mc:Choice / mc:Fallback) of an `mc:AlternateContent`."""
    branch = alternate.find(f"{{{_MARKUP_COMPATIBILITY}}}{name}")
    return [child for child in branch if child.tag in _SHAPE_TAGS] if branch is not None else []


def _render_slide(slide: object, index: int, options: ConvertOptions, extraction: Extraction,
                  unread_ole: list[object], authors: dict[str, str]) -> Section:
    unreadable = _drop_unreadable_shapes(slide.shapes._spTree)
    _unwrap_alternate_content(slide.shapes._spTree, slide.part)

    # Only the shape used as the title is left out of the body. Leaving out every title placeholder made the text of a second title
    # placeholder vanish
    # (Pinned by: test_office.py::TestPptxStrikethroughTitlesAndHiddenSlides::test_second_title_placeholder_is_kept_as_text).
    title_shape = _title_shape(slide)
    skipped: list[object] = []
    blocks = _render_shapes(slide.shapes, options, extraction, skipped, unread_ole, exclude=title_shape)
    if unreadable or skipped:
        extraction.warnings.append(f"Slide {index}: skipped {unreadable + len(skipped)} unreadable shape(s).")

    if options.include_speaker_notes and slide.has_notes_slide:
        # Notes are outside the per-shape skip protection (`_render_shapes`), so one placeholder type in the notes or one notes part
        # rejected the whole document. Only those notes are skipped (Pinned by: test_office_robustness.py::TestPptxSpeakerNotesAreIsolated).
        try:
            notes = _speaker_notes(slide.notes_slide)
        except (AttributeError, KeyError, ValueError, InvalidXmlError):
            notes = ""
            extraction.warnings.append(f"Slide {index}: skipped the speaker notes because they could not be read.")
        if notes:
            blocks.append(notes)
    blocks.append(_slide_comments(slide.part, authors))

    title = " ".join(title_shape.text_frame.text.split()) if title_shape is not None else None
    if slide._element.get("show") in _HIDDEN:
        title = f"{title} (hidden)" if title else "(hidden)"
    return Section(SectionKind.SLIDE, index, "\n\n".join(block for block in blocks if block.strip()), title)


# Comments. The old format (p:cmLst, authors p:cmAuthorLst) and the new PowerPoint 365 format (p188:cmLst, replies p188:replyLst,
# authors p188:authorLst)
_P188 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
_PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _related_roots(part: object, name: str) -> list[object]:
    """XML roots of the parts among `part`'s relationships whose kind name is `name` (the end of the relationship type URI). External
    and broken parts are skipped."""
    roots = []

    for rel in part.rels.values():
        if rel.is_external or rel.reltype.rsplit("/", 1)[-1] != name:
            continue
        try:
            root = parse_part(rel.target_part.blob)
        except (AttributeError, KeyError, ValueError):
            continue  # comments are a bonus — one part must not make the whole slide be skipped
        if root is not None:
            roots.append(root)
    return roots


def _comment_authors(part: object) -> dict[str, str]:
    """Comment author id → name. The old format uses numbers (p:cmAuthor id), the new one GUIDs (p188:author id), so they never collide."""
    authors = {}

    for root in _related_roots(part, "commentAuthors") + _related_roots(part, "authors"):
        for author in (*root.iter(f"{{{_PRESENTATION}}}cmAuthor"), *root.iter(f"{{{_P188}}}author")):
            authors[author.get("id", "")] = author.get("name", "")
    return authors


def _slide_comments(part: object, authors: dict[str, str]) -> str:
    """Slide comments as `> Comment(author): text` / `> Reply(author): text` lines (`_shared.comment_line`, same shape as DOCX). A slide
    comment is attached to a position, not to a range of body text, so it has no target. Comments were dropped without a warning
    (Pinned by: test_office_comments.py::TestPptxComments)."""
    def body(element: object) -> str:
        paragraphs = element.iter(f"{{{_DRAWING}}}p")
        return " ".join("".join(text.text or "" for text in paragraph.iter(f"{{{_DRAWING}}}t")) for paragraph in paragraphs)

    lines = []

    for root in _related_roots(part, "comments"):
        for comment in root.iter(f"{{{_PRESENTATION}}}cm"):
            text = comment.findtext(f"{{{_PRESENTATION}}}text") or ""
            if text.strip():
                lines.append(comment_line(authors.get(comment.get("authorId", ""), ""), text))
        for comment in root.iter(f"{{{_P188}}}cm"):
            entries = [(comment, False), *((reply, True) for reply in comment.iter(f"{{{_P188}}}reply"))]
            for entry, reply in entries:
                content = entry.find(f"{{{_P188}}}txBody")
                text = body(content) if content is not None else ""
                if text.strip():
                    lines.append(comment_line(authors.get(entry.get("authorId", ""), ""), text, reply=reply))
    return "\n".join(lines)


def _speaker_notes(notes_slide: object) -> str:
    """Speaker notes block (`> Speaker notes: …`). An empty string when there are no notes.

    The level of a notes paragraph (a:pPr lvl) is shown with `cell_level_mark` ('└ '). `clean_text` merges the leading spaces of a
    quote-block line, so the levels disappeared. No numbers are attached (the accepted gap in the auto-number row of
    docs/decisions/office-lists.md. Pinned by: test_office_cells_and_notes.py::TestPptxSpeakerNoteLevels). The level is read with
    `_paragraph_level`, like the body — python-pptx's `paragraph.level` raised ValueError on 'x' and '²'. Unknown placeholder types are
    removed before iteration, as on a slide (`_drop_unreadable_shapes`).
    """
    _drop_unreadable_shapes(notes_slide.shapes._spTree)
    body = notes_slide.notes_text_frame
    # A text box placed separately on the notes page (the way LibreOffice exports notes) is notes too. Placeholders such as the slide
    # picture, page number and header are not notes text
    # (Pinned by: test_office_cells_and_notes.py::TestPptxSpeakerNoteLevels::test_text_box_on_the_notes_page_is_part_of_the_notes)
    frames = [] if body is None else [body]
    # A text box that holds nothing but fields (slide number, date) is page furniture: a deck that went through LibreOffice (a
    # converted .ppt) carries the notes-page number as such a box, and it ended the notes with a `<number>` line
    # (Pinned by: test_office_cells_and_notes.py::TestPptxSpeakerNoteLevels::test_field_only_text_box_on_the_notes_page_is_not_notes)
    frames += [shape.text_frame for shape in notes_slide.shapes
               if not shape.is_placeholder and shape.has_text_frame and not _holds_only_fields(shape._element)]
    notes = [cell_level_mark(_paragraph_level(paragraph)) + line
             for frame in frames for paragraph in frame.paragraphs for line in paragraph.text.splitlines() if line.strip()]
    return "> Speaker notes: " + "\n> ".join(notes) if notes else ""


def _holds_only_fields(shape: object) -> bool:
    """The shape's text is made of field elements only (`a:fld`), with no ordinary run that has text."""
    has_field = any(True for _ in shape.iter(f"{{{_DRAWING}}}fld"))
    has_text = any((text.text or "").strip() for run in shape.iter(f"{{{_DRAWING}}}r") for text in run.iter(f"{{{_DRAWING}}}t"))
    return has_field and not has_text


def _drop_unreadable_shapes(tree: object) -> int:
    """Detach the elements python-pptx cannot build a shape from (in the in-memory copy) and return their count.

    Ink annotations (p:contentPart — real PowerPoint writes them) rejected the whole document with an AttributeError during shape
    iteration. An unknown type or a non-numeric index on a placeholder (p:ph) is a ValueError the moment the shape is built, so only the
    attribute is removed (defaults obj and 0 — a shape is not thrown away over one value).
    Pinned by: test_office.py::TestOneBrokenShapeDoesNotRejectTheDeck.
    """
    ink = list(tree.iter(f"{{{_PRESENTATION}}}contentPart"))
    for element in ink:
        element.getparent().remove(element)

    for placeholder in tree.iter(f"{{{_PRESENTATION}}}ph"):
        if "type" in placeholder.attrib and placeholder.get("type") not in _PLACEHOLDER_TYPES:
            del placeholder.attrib["type"]
        if "idx" in placeholder.attrib and not placeholder.get("idx").isdecimal():
            del placeholder.attrib["idx"]
    return len(ink)


def _title_shape(slide: object) -> object | None:
    """Shape to use as the slide title: the first title placeholder that has text."""
    for shape in slide.shapes:
        if _placeholder_type(shape) in _TITLE_PLACEHOLDERS and shape.has_text_frame and shape.text_frame.text.strip():
            return shape
    return None


def _placeholder_type(shape: object) -> PP_PLACEHOLDER | None:
    """Placeholder type. None if the shape is not a placeholder."""
    return shape.placeholder_format.type if shape.is_placeholder else None


def _render_shapes(shapes: object, options: ConvertOptions, extraction: Extraction, skipped: list[object],
                   unread_ole: list[object], exclude: object | None = None) -> list[str]:
    """Render the shapes in reading order. A shape python-pptx raises on because of one odd attribute (ValueError, AttributeError,
    InvalidXmlError — a graphicFrame without a frame (p:xfrm) or graphic data (a:graphic), etc.) is skipped on its own and put in
    `skipped`. One shape used to reject the whole document
    (Pinned by: test_office.py::TestOneBrokenShapeDoesNotRejectTheDeck::test_graphic_frame_missing_its_graphic_is_skipped_with_one_warning)."""
    blocks: list[str] = []

    for shape in _reading_order(shapes):
        if exclude is not None and shape._element is exclude._element:
            continue
        try:
            blocks.extend(_render_shape(shape, options, extraction, skipped, unread_ole))
        except (ValueError, AttributeError, InvalidXmlError):
            skipped.append(shape)
    return blocks


def _reading_order(shapes: object) -> list[object]:
    """Top → bottom, and left → right within a row. Scanning from the top, a shape whose vertical distance from the previous shape is
    within the tolerance is in the same row.

    When vertical positions were bucketed into a grid the size of the tolerance, slightly misaligned shapes straddling a grid boundary
    made the right-hand one be read first
    (Pinned by: test_office.py::TestPptxLayoutAndEmbeddedObjects::test_shapes_misaligned_within_the_tolerance_are_read_left_to_right).
    When a row holds two or more groups of placeholders stacked tightly in the same column, they are read column by column
    (`_column_stacks`).
    Even when the top difference exceeds the tolerance, a label vertically centered to the left of a large shape of the row is in the
    same row (`_beside_row`). The shape right after a label opens the next row if it is stacked directly below a shape of the row
    (`_stacked_below`), even when its top is close to the label's — a card's value box, whose top equals that of a label attached to
    the card's title row, was pulled into that row and read as 'value → title' (Pinned by:
    test_office.py::TestPptxSideLabelsAndPictureFills::test_label_joined_to_a_row_does_not_pull_the_next_row_into_it). Dropping the
    label's top from the reference altogether split a figure sitting beside the label (same top) into a different row from the label
    (test_shapes_level_with_a_joined_label_stay_in_its_row). Narrowing the check to the shape right after the label is deliberate —
    applied to the whole row after a label, a subtitle between shapes arranged like stairs was pushed behind the value, and across
    1,236 real presentations 20 changed compared with the version without this check and several got worse. The narrowed check changes
    only 7.
    """
    rows: list[list[object]] = []
    # Row member → its box, resolved the first time the member is compared with a later shape. Each comparison resolved the
    # member's four values again, and every label that joins a row is compared with the whole row: 2,000 labels beside one box
    # took 36 s (Pinned by: test_audit_office.py::TestPptxRowOfManySideLabels::test_position_of_a_row_member_is_resolved_once).
    boxes: dict[int, tuple[int, int, int, int]] = {}
    anchor, after_label = 0, False  # top of the last shape seen, and whether that shape joined its row as a label
    for top, shape in sorted(((_offset(shape, "top"), shape) for shape in shapes), key=lambda item: item[0]):
        if rows and top - anchor <= _ROW_TOLERANCE and not (after_label and _stacked_below(shape, rows[-1], boxes)):
            rows[-1].append(shape)
            after_label = False
        elif rows and _beside_row(shape, rows[-1], boxes):
            rows[-1].append(shape)
            after_label = True
        else:
            rows.append([shape])
            after_label = False
        anchor = top
    return _column_stacks([sorted(row, key=lambda shape: _offset(shape, "left")) for row in rows])


def _member_box(member: object, boxes: dict[int, tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    """(top, left, bottom, right) of a row member, resolved once per `_reading_order` (`boxes`, keyed by the shape object)."""
    box = boxes.get(id(member))
    if box is None:
        top, left = _offset(member, "top"), _offset(member, "left")
        box = boxes[id(member)] = (top, left, top + _offset(member, "height"), left + _offset(member, "width"))
    return box


def _stacked_below(shape: object, row: list[object], boxes: dict[int, tuple[int, int, int, int]]) -> bool:
    """Whether `shape` overlaps some shape of the row horizontally and starts at or below that shape's bottom edge (within the
    tolerance) — it is the next row, stacked below."""
    top, left = _offset(shape, "top"), _offset(shape, "left")
    right = left + _offset(shape, "width")
    for member in row:
        _, member_left, member_bottom, member_right = _member_box(member, boxes)
        if min(right, member_right) - max(left, member_left) > _ROW_TOLERANCE and top >= member_bottom - _ROW_TOLERANCE:
            return True
    return False


def _beside_row(shape: object, row: list[object], boxes: dict[int, tuple[int, int, int, int]]) -> bool:
    """Whether `shape` is a label vertically centered to the left of a larger shape of the row: it lies within that shape's vertical
    extent, the vertical centers differ by no more than the tolerance (0.2 cm), and its right edge does not pass that shape's left edge
    (0.2 cm allowed). Not if it overlaps another shape of the row horizontally.

    A label box vertically centered beside a large content box has its top below the content's, so it was read after the content and
    '현황' (status) ended up looking like the title of the next row's content
    (Pinned by: test_office.py::TestPptxSideLabelsAndPictureFills::test_label_centered_beside_a_taller_box_is_read_with_it).
    Deliberately narrow: treating shapes as the same row whenever their vertical extents merely overlap (half of the shorter one, or
    each other's centers) tied a tall box to a next row that only covers its lower part and pushed it behind that row, and so on —
    more than 400 of 1,236 real presentations changed order for no reason. This rule changes 209, and in the sample most were corrected
    to 'label → value' and '● → item text'. A shape placed on the right already has the correct order (after the large shape on the
    left). Shapes that overlap horizontally are stacked above/below and are not joined
    (Pinned by: test_boxes_stacked_beside_a_tall_sidebar_keep_their_top_to_bottom_order).
    """
    top, left = _offset(shape, "top"), _offset(shape, "left")
    bottom, right = top + _offset(shape, "height"), left + _offset(shape, "width")
    is_label = False

    for member in row:
        member_top, member_left, member_bottom, member_right = _member_box(member, boxes)
        if min(right, member_right) - max(left, member_left) > _ROW_TOLERANCE:
            return False
        is_centered = abs((top + bottom) - (member_top + member_bottom)) <= 2 * _ROW_TOLERANCE  # compares twice the centers
        if member_top <= top and bottom <= member_bottom and is_centered and right <= member_left + _ROW_TOLERANCE:
            is_label = True
    return is_label


def _column_stacks(rows: list[list[object]]) -> list[object]:
    """Read the rows one after another, but read a row of placeholders that pairs up column by column with the row right below it
    column-wise (top → bottom).

    A comparison layout (two header placeholders + two body placeholders below them) was read as 'AS-IS, TO-BE, left list, right list',
    so it was impossible to tell which header a list belonged to. A pair must both be placeholders, have nearly the same left edge and
    width, and touch vertically. A grid of freely placed text boxes (a name row + a value row) stays row by row as before — it may be a
    layout meant to be read like a table, so it is not changed (deliberate. Pinned by:
    test_office.py::TestOfficeReadingOrderAndListMarks::test_comparison_layout_is_read_column_by_column,
    test_free_form_text_boxes_in_a_grid_are_still_read_row_by_row).
    """
    ordered: list[object] = []
    index = 0

    while index < len(rows):
        stack = [rows[index]]
        while index + len(stack) < len(rows) and _stacks_on(stack[-1], rows[index + len(stack)]):
            stack.append(rows[index + len(stack)])
        ordered.extend(shape for column in zip(*stack, strict=True) for shape in column)
        index += len(stack)
    return ordered


def _stacks_on(upper: list[object], lower: list[object]) -> bool:
    """Whether the placeholders of the `lower` row are stacked one per column directly below those of the `upper` row (two or more columns)."""
    if len(upper) < 2 or len(upper) != len(lower):
        return False
    for above, below in zip(upper, lower, strict=True):
        if not (above.is_placeholder and below.is_placeholder):
            return False
        gap = _offset(below, "top") - _offset(above, "top") - _offset(above, "height")
        if abs(_offset(above, "left") - _offset(below, "left")) > _COLUMN_TOLERANCE or not -_ROW_TOLERANCE <= gap <= _STACK_GAP:
            return False
        if abs(_offset(above, "width") - _offset(below, "width")) > _COLUMN_TOLERANCE:
            return False
    return True


def _offset(shape: object, axis: str) -> int:
    """Position/size of a shape (top, left, width, height, EMU). A placeholder inherits its position from the layout, and a broken
    layout relationship rejected the whole document with a KeyError. A non-numeric coordinate (`x="abc"`) is a ValueError, and a
    graphicFrame without a frame (p:xfrm) an InvalidXmlError. An unknown position counts as 0 (Pinned by:
    test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_pptx_slide_whose_layout_relationship_is_broken_is_read,
    TestOneBrokenShapeDoesNotRejectTheDeck::test_shape_with_an_odd_position_value_is_read_at_the_origin)."""
    try:
        return _effective_offset(shape, axis) or 0
    except (KeyError, ValueError, InvalidXmlError):
        return 0


# Shape position/size name → attribute of the shape element's frame (a:xfrm).
_XFRM_ATTRIBUTES = {"top": "y", "left": "x", "width": "cx", "height": "cy"}


def _effective_offset(shape: object, axis: str) -> int | None:
    """Same value as python-pptx's `shape.<axis>` (for a placeholder, the value of the placeholder it inherits from when its own frame
    has none). The inherited-from placeholder is found with `_placeholder_base` instead of `_base_placeholder`."""
    if not isinstance(shape, _InheritsDimensions):
        return getattr(shape, axis)
    own = getattr(shape._element, _XFRM_ATTRIBUTES[axis])
    if own is not None:
        return own
    base = _placeholder_base(shape)
    return _effective_offset(base, axis) if base is not None else None


def _placeholder_base(shape: object) -> object | None:
    """Placeholder that a placeholder inherits its position and bullets from (the same thing as python-pptx `_base_placeholder`): for
    one on a slide, the one with the same index in the layout; for one in a layout, the corresponding type in the master.

    python-pptx scans the placeholders of the layout/master from the start on every call. The position is asked several times per
    shape (`_reading_order`) and bullets (`_list_styles`) are inherited too, so a slide with many inheriting placeholders took the
    square of their number (1,000 took 89 seconds. Pinned by:
    test_office_scaling_and_note_order.py::TestPptxManyInheritedPlaceholders). An index → layout placeholder table is built once per
    slide part, and a type → result table once per master part, and kept on that part (it goes away with the part).
    """
    if isinstance(shape, (_BaseSlidePlaceholder, PlaceholderPicture)):
        part = shape.part
        layout_placeholders = part.__dict__.get("_converter_layout_placeholders")
        if layout_placeholders is None:
            layout_placeholders = {}
            for placeholder in part.slide_layout.placeholders:  # when several share an index, the first one, like python-pptx
                layout_placeholders.setdefault(placeholder.element.ph_idx, placeholder)
            part._converter_layout_placeholders = layout_placeholders
        return layout_placeholders.get(shape._element.ph_idx)
    if isinstance(shape, LayoutPlaceholder):
        master = shape.part.slide_master.part
        master_placeholders = master.__dict__.setdefault("_converter_master_placeholders", {})
        kind = shape._element.ph_type  # the type to look up in the master follows from this type (python-pptx's mapping table)
        if kind not in master_placeholders:
            master_placeholders[kind] = shape._base_placeholder
        return master_placeholders[kind]
    return shape._base_placeholder


def _render_shape(shape: object, options: ConvertOptions, extraction: Extraction, skipped: list[object],
                  unread_ole: list[object]) -> list[str]:
    # A shape hidden in the selection pane (p:cNvPr hidden) does not show in the slide show or in print. It is left out like DOCX hidden
    # text (w:vanish) — a draft price was read as if it were a valid value (deliberate. Pinned by: test_office.py::TestPptxHiddenShapes).
    if _is_hidden(shape) or _placeholder_type(shape) in _SKIP_PLACEHOLDERS:
        return []

    # Whether a shape is a group is decided by its class. `shape.shape_type` raised NotImplementedError on a shape without geometry
    # information and rejected the whole document
    # (Pinned by: test_office.py::test_shape_of_unrecognized_type_does_not_reject_the_deck).
    if isinstance(shape, GroupShape):
        return _render_shapes(shape.shapes, options, extraction, skipped, unread_ole)
    image = _picture_blob(shape) if options.extract_images else None
    if image:
        return [extraction.add_image(image, alt=_shape_alt(shape))]
    if getattr(shape, "has_table", False) and shape.has_table:
        return [_render_pptx_table(shape.table, extraction, _inline_of(shape, options))]
    if getattr(shape, "has_chart", False) and shape.has_chart:
        try:
            chart_part = shape.chart_part
            # A chart part is read once however many frames point at it, and its values are written at the first of them only;
            # the later frames give `[Chart] title` (the rule for ODF objects; the title is kept on the part and goes away with
            # it). 100 frames on one chart of 20,000 rows took 25 s and 590 MB (Pinned by: test_audit_office.py::
            # TestChartPartReferencedByManyFrames::test_pptx_chart_is_read_once_and_its_values_written_once).
            if "_converter_chart_title" in chart_part.__dict__:
                return [chart_part._converter_chart_title]
            chart = parse_part(chart_part.blob)
        except (KeyError, ValueError):  # broken or external relationship. One chart must not reject the whole document
            return []
        rendered = render_chart(chart, load_related=related_loader(chart_part)) if chart is not None else ""
        chart_part._converter_chart_title = rendered.split("\n", 1)[0]
        return [rendered] if chart is not None else []
    if shape.has_text_frame:
        # A shape filled with a picture (a circle holding a photo, etc.) is not a picture shape, so it was dropped without a trace.
        # The fill picture goes first, the text on the shape after it.
        fill = _picture_fill(shape) if options.extract_images else None
        markers = [extraction.add_image(fill, alt=_shape_alt(shape))] if fill else []
        as_bullets = _placeholder_type(shape) in _BULLET_PLACEHOLDERS
        return [*markers, _render_text_frame(shape.text_frame, as_bullets, _list_styles(shape), _inline_of(shape, options))]
    if _has_chartex(shape._element):
        return [_chartex_text(shape._element, shape.part) or "[Chart]"]
    if next(shape._element.iter(f"{{{_PRESENTATION}}}oleObj"), None) is not None:
        # When no preview picture can be registered (image extraction is off or there is no preview), a placeholder and a count
        # warning are left, as for HWP and HWPX. Pasted sheets and documents vanished without a trace
        # (Pinned by: test_office_chart_units_and_ole.py::TestOfficeOleObjectPlaceholders).
        preview = _ole_preview(shape) if options.extract_images else None
        if preview:
            return [extraction.add_image(preview, alt=_shape_alt(shape))]
        unread_ole.append(shape)
        return [OLE_PLACEHOLDER]
    smartart = _render_smartart(shape)
    return [smartart] if smartart else []


def _is_hidden(shape: object) -> bool:
    """Whether hidden is set in the shape's non-visual properties (the p:cNvPr of p:nvSpPr, p:nvPicPr …)."""
    properties = shape._element.find(f"*/{{{_PRESENTATION}}}cNvPr")
    return properties is not None and properties.get("hidden") in _TRUE


def _has_chartex(element: object) -> bool:
    return element.tag == f"{{{_PRESENTATION}}}graphicFrame" and next(element.iter(f"{{{_CHARTEX}}}chart"), None) is not None


def _chartex_text(element: object, part: object) -> str:
    """Chart part referenced by the frame of a new chart kind (the cx:chart inside p:graphicFrame) → "[Chart] title" + value table.
    An empty string if it cannot be read."""
    chart = next(element.iter(f"{{{_CHARTEX}}}chart"))
    try:
        chart_space = parse_part(part.related_part(chart.get(f"{{{_RELATIONSHIPS}}}id")).blob)
    except (KeyError, ValueError):  # broken or external relationship. Falls through to the fallback picture or the placeholder
        return ""
    return render_chartex(chart_space) if chart_space is not None else ""


def _ole_preview(shape: object) -> bytes | None:
    """Preview picture of an OLE object (a pasted Excel sheet or Word document). The object's content is not read — what shows on
    screen is this picture.

    The picture is in the p:pic (a:blip) inside the object or, in the old format, in the slide's VML drawing part on the shape whose id
    equals the object's spid. Pictures that cannot be decoded, such as EMF and WMF, are reported by the converter as a warning. The
    object vanished with neither a placeholder nor a warning
    (Pinned by: test_office.py::TestPptxLayoutAndEmbeddedObjects::test_ole_object_preview_picture_is_registered,
    test_ole_object_preview_in_the_vml_drawing_is_registered).
    """
    try:
        blip = next(shape._element.iter(f"{{{_DRAWING}}}blip"), None)
        if blip is not None:
            return shape.part.related_part(blip.get(f"{{{_RELATIONSHIPS}}}embed")).blob

        shape_id = next(shape._element.iter(f"{{{_PRESENTATION}}}oleObj")).get("spid")
        for relationship in shape.part.rels.values():
            if relationship.is_external or not relationship.reltype.endswith("/vmlDrawing"):
                continue
            drawing = parse_part(relationship.target_part.blob)
            for vml_shape in drawing.iter(f"{{{_VML}}}shape") if drawing is not None and shape_id else []:
                image = vml_shape.find(f"{{{_VML}}}imagedata")
                if vml_shape.get("id") == shape_id and image is not None:
                    return relationship.target_part.related_part(image.get(f"{{{_OFFICE}}}relid")).blob
    except (KeyError, ValueError):  # broken or external relationship. One object must not reject the whole document
        pass
    return None


def _render_smartart(shape: object) -> str:
    """SmartArt (diagram): the text is not in the slide XML but in the data part (ppt/diagrams/data*.xml). Rendering is shared with DOCX."""
    relation = shape._element.find(f".//{{{_DIAGRAM}}}relIds")
    if relation is None:
        return ""
    try:
        data_model = parse_part(shape.part.related_part(relation.get(f"{{{_RELATIONSHIPS}}}dm")).blob)
    except (KeyError, ValueError):  # broken or external relationship (TargetMode="External" is a ValueError). Only this SmartArt is skipped
        return ""
    return render_smartart(data_model) if data_model is not None else ""


def _render_text_frame(text_frame: object, as_bullets: bool, list_styles: list[object], inline: _Inline | None = None) -> str:
    """Paragraphs of a text box. Paragraphs of a body placeholder and lower-level paragraphs get `- `; auto-numbered (a:buAutoNum)
    paragraphs get their number.

    Numbers are counted per level within the text box. A deeper paragraph does not interrupt the numbering of a shallower level, and
    when a shallower level appears the deeper levels start over. An unnumbered paragraph of the same level (an empty one included) or a
    different number scheme restarts the count, and an empty numbered paragraph is skipped without taking a number — the same way
    LibreOffice renders PowerPoint files. Numbers were not read and so disappeared (14 of 182 real presentations). A top-level
    paragraph whose bullet was removed explicitly (a:buNone) gets no `- ` even in a body placeholder
    (Pinned by: test_office.py::TestPptxAutoNumbering).
    A top-level paragraph of a text box also gets `- ` when it has a bullet character (a:buChar) or a picture bullet (a:buBlip). It
    came out unmarked and could not be told apart from a title line (Pinned by: test_office.py::TestOfficeReadingOrderAndListMarks::
    test_text_box_paragraphs_with_a_bullet_character_or_picture_are_list_items).
    A line break inside a paragraph (a:br, Shift+Enter) is kept as a line. Joined with spaces,
    '분석 항목: 1. 수익성 비교 … 2. 안정성 비교 …' (analysis items: 1. profitability … 2. stability …) became one line (table cells and
    speaker notes already kept the lines). Lines of a list-item paragraph go through `_item_lines`; lines of a non-list paragraph stay
    as they are (Pinned by:
    test_office.py::TestOfficeLineBreaksNestedFieldsAndMetadata::test_pptx_line_breaks_in_a_text_box_paragraph_keep_their_lines).
    """
    rendered = []
    counters: dict[int, tuple[str, int]] = {}  # level → (number scheme, last number)
    for paragraph in text_frame.paragraphs:
        lines = [" ".join(line.split()) for line in _paragraph_text(paragraph, inline).split("\n")]
        lines = [line for line in lines if line]
        level = _paragraph_level(paragraph)
        bullet = _bullet(paragraph._p, level, list_styles)
        is_numbered = bullet is not None and bullet.tag == f"{{{_DRAWING}}}buAutoNum"
        if is_numbered and not lines:
            continue
        for deeper in [key for key in counters if key > level]:
            del counters[deeper]

        if is_numbered:
            scheme = bullet.get("type", "arabicPeriod")
            previous = counters.get(level)
            start = bullet.get("startAt", "1")
            number = previous[1] + 1 if previous and previous[0] == scheme else decimal_int(start, 1)
            counters[level] = (scheme, number)
            rendered.append(_item_lines(list_item_prefix(_auto_number(number, scheme), level), lines))
            continue

        counters.pop(level, None)
        if not lines:
            continue
        is_unbulleted = bullet is not None and bullet.tag == f"{{{_DRAWING}}}buNone" and level == 0
        is_bulleted = bullet is not None and bullet.tag in _VISIBLE_BULLETS
        if (as_bullets and not is_unbulleted) or is_bulleted or level > 0:
            rendered.append(_item_lines(f"{'  ' * level}- ", lines))
        else:
            rendered.append("\n".join(lines))
    return "\n".join(rendered)


def _item_lines(prefix: str, lines: list[str]) -> str:
    """Lines of a list-item paragraph (lines separated by a:br). The first line is the item; a line starting with a hand-typed bullet
    ('- ', '* ', '1. ', '1) ') is an item one level down (a line with a bullet character such as '•' and a '1) ' line get '- '
    prepended); the other lines are left as continuation lines of the line before — the same rule as DOCX
    `_DocxRenderer._list_item_lines` plus numbered lines (Pinned by: test_office.py::TestOfficeLineBreaksNestedFieldsAndMetadata::
    test_pptx_hand_typed_bullet_lines_inside_a_list_item_are_its_sub_items, test_office_symbols_fields_notes.py::
    TestPptxHandTypedLinesInsideAnItem)."""
    sub = " " * (len(prefix) - len(prefix.lstrip(" ")) + 2)

    rendered = [prefix + lines[0]]
    for line in lines[1:]:
        if item := _HAND_TYPED_ITEM.match(line):
            line = f"{sub}- {line}" if item.group(1) == ")" else sub + line
        elif HAND_TYPED_BULLET.match(line):
            line = f"{sub}- {line}"
        rendered.append(line)
    return "\n".join(rendered)


def _paragraph_level(paragraph: object) -> int:
    """Paragraph level (a:pPr lvl). PowerPoint levels are 0–8 — using the file's value as is, a single lvl produced several GB. A
    non-numeric value ('x') made python-pptx raise ValueError and rejected the whole document. The attribute text is read directly and
    such a value counts as 0 (Pinned by: test_office.py::
    TestOneOddValueDoesNotRejectTheDocument::test_pptx_odd_paragraph_level_is_clamped)."""
    properties = paragraph._p.find(f"{{{_DRAWING}}}pPr")
    return min(decimal_int(properties.get("lvl") if properties is not None else None, 0), MAX_LEVEL)


def _list_styles(shape: object) -> list[object]:
    """Per-level definitions (a:lstStyle) a paragraph inherits its bullet from: the shape itself, then, for a placeholder, the same
    placeholder in the layout and the master, in that order.

    The master's default text styles (p:txStyles) are not followed — the `- ` of a body placeholder stands for that default bullet.
    """
    styles = []
    current = shape

    for _ in range(3):
        style = current._element.find(f"{{{_PRESENTATION}}}txBody/{{{_DRAWING}}}lstStyle")
        if style is not None:
            styles.append(style)
        try:
            current = _placeholder_base(current) if current.is_placeholder else None
        # No layout, a broken relationship or an odd placeholder index. Read without inherited definitions
        except (AttributeError, KeyError, ValueError):
            current = None
        if current is None:
            break
    return styles


def _bullet(paragraph: object, level: int, list_styles: list[object]) -> object | None:
    """Bullet element of a paragraph (a:buNone, a:buAutoNum, a:buChar, a:buBlip). If the paragraph has none, it is looked up in the
    inherited level definitions."""
    properties = [paragraph.find(f"{{{_DRAWING}}}pPr"),
                  *(style.find(f"{{{_DRAWING}}}lvl{level + 1}pPr") for style in list_styles)]
    for element in properties:
        bullet = next((child for child in element if child.tag in _BULLET_TAGS), None) if element is not None else None
        if bullet is not None:
            return bullet
    return None


def _auto_number(number: int, scheme: str) -> str:
    """One auto number: 'arabicPeriod' → '3.', 'alphaLcParenR' → 'c)', 'circleNumDbPlain' → '③'."""
    shape = next((shape for prefix, shape in _AUTO_NUMBER_KINDS if scheme.startswith(prefix)), "DIGIT")
    if shape == "BLACK_CIRCLED_DIGIT":
        digits = _BLACK_CIRCLED[number - 1] if 1 <= number <= len(_BLACK_CIRCLED) else str(number)
    else:
        digits = format_number(number, shape)
    template = next((template for suffix, template in _AUTO_NUMBER_DECORATIONS if scheme.endswith(suffix)), "{}")
    return template.format(digits)


def _paragraph_text(paragraph: object, inline: _Inline | None = None) -> str:
    """Paragraph text. Like `paragraph.text`, but superscript/subscript runs (a:rPr baseline) are marked and a line break (a:br) is kept as `\\n`.

    Appending scripts as plain text turned 10⁶ into 106, and a line break in a table cell came out as a vertical tab, which the
    cleanup step removed, gluing words together
    (Pinned by: test_office.py::TestSuperscriptAndTextBoxes::test_pptx_superscript_runs_are_marked_in_text_boxes_and_table_cells).
    An equation inside a paragraph (the OMML inside a14:m) is not a run, so it was dropped without a warning. It is kept in place in
    the same notation as DOCX (` $LaTeX$ `)
    (Pinned by: test_office.py::TestPptxEquations).
    Struck-through runs (a:rPr strike) are emitted in the same notation as DOCX and HTML (`~~text~~`). As plain text, the value from
    before a correction reads as a valid value (Pinned by:
    test_office.py::TestPptxStrikethroughTitlesAndHiddenSlides::test_struck_runs_are_marked_in_text_boxes_and_table_cells).
    With `inline`, a run's hyperlink (a:hlinkClick with an external relationship) is written as `[text](url)` and bold / italic
    runs (a:rPr b / i) as `**bold**` / `*italic*` (`_shared.resolve_inline`); a paragraph that is bold throughout is left
    unmarked. Slide titles (headings) and speaker notes are read as plain text
    (Pinned by: test_inline_formatting.py::TestPptx).
    """
    text = "".join(_inline_parts(paragraph._p, inline))
    if inline is None:
        return resolve_scripts(resolve_strikes(text))
    return resolve_inline(resolve_scripts(resolve_strikes(merge_emphasis_marks(text))))


# Slide-number field text written in place of a number: PPTX saved by LibreOffice (`<number>`), PowerPoint masters/layouts (`‹#›`). It
# is left out because the section heading ('## Slide N') already gives the number. A field holding an actual number is kept
# (Pinned by: test_office_round45.py::TestSlideNumberPlaceholder).
_SLIDE_NUMBER_PLACEHOLDERS = frozenset(("<number>", "‹#›"))


def _inline_parts(container: object, inline: _Inline | None = None) -> list[str]:
    """Children of a paragraph (or of one branch of an `mc:AlternateContent` inside a paragraph) → pieces of text.

    PowerPoint wraps an equation in mc:Choice (a14:m) + mc:Fallback (text or picture for older versions) inside a paragraph as well.
    They are two renderings of the same content, so once the new version yields text the fallback is not read (the same rule as the
    shape-level `_unwrap_alternate_content`).
    """
    parts = []
    for child in container:
        name = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
        if name == "br":
            parts.append("\n")
        elif name in ("r", "fld"):
            text = "".join(node.text or "" for node in child if isinstance(node.tag, str) and node.tag == f"{{{_DRAWING}}}t")
            if name == "fld" and child.get("type") == "slidenum" and text.strip() in _SLIDE_NUMBER_PLACEHOLDERS:
                continue
            properties = child.find(f"{{{_DRAWING}}}rPr")
            plain_font = _plain_font(properties)
            text = symbol_font_text(_symbol_font(properties), text, ascii_font=plain_font, latin_font=plain_font)
            baseline = (properties.get("baseline", "0") if properties is not None else "0").rstrip("%")
            shift = decimal_int(baseline, 0, signed=True)
            if shift:
                text = mark_script(text, SUPERSCRIPT if shift > 0 else SUBSCRIPT)
            is_struck = properties is not None and properties.get("strike") in _STRIKES
            text = mark_strike(text) if is_struck else text
            if inline is not None and properties is not None:
                # The outermost mark, so a bold formula stays one span: `**H_2O**`
                if inline.emphasis:
                    text = mark_emphasis(text, properties.get("b") in _TRUE, properties.get("i") in _TRUE)
                click = properties.find(f"{{{_DRAWING}}}hlinkClick") if inline.link is not None else None
                url = inline.link(click) if click is not None else None
                if url and text:
                    text = f"{LINK_OPEN}{text}{link_close(url)}"
            parts.append(text)
        elif name == "m":  # a14:m. An equation paragraph (m:oMathPara) may hold several equations
            parts.extend(render_omml(equation) for equation in child.iter(f"{{{_MATH}}}oMath"))
        elif child.tag == f"{{{_MARKUP_COMPATIBILITY}}}AlternateContent":
            for branch in child:
                branch_parts = _inline_parts(branch, inline)
                if "".join(branch_parts).strip():
                    parts.extend(branch_parts)
                    break
    return parts


def _symbol_font(properties: object | None) -> str | None:
    """The run font (a:sym → a:latin → a:ea → a:cs typeface of a:rPr) that has a symbol-font table. PowerPoint's Insert Symbol saves
    a:sym typeface="Wingdings" + a private-use-area character (U+F0FD etc.) — with the font ignored, ☒ was removed and only □ was
    left, so the chosen option read the other way round (same table as DOCX w:sym.
    Pinned by: test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText)."""
    if properties is None:
        return None
    for name in _RUN_FONT_ELEMENTS:
        font = properties.find(f"{{{_DRAWING}}}{name}")
        typeface = font.get("typeface", "") if font is not None else ""
        if typeface.strip().lower() in SYMBOL_GLYPHS:
            return typeface
    return None


def _plain_font(properties: object | None) -> str | None:
    """Run font that draws ordinary characters (ASCII, Latin-1): the a:latin typeface (the Symbol mapping of ordinary characters in
    `symbol_font_text`).

    a:sym is not consulted (**deliberate**). PowerPoint's Insert Symbol saves a U+F0xx character + a:sym, and text typed right after it
    inherits a:sym as well but is drawn on screen in the Latin font. Following a:sym turned 'x+z = y+z for all numbers' in a real
    PowerPoint 2010 lecture deck into 'ξ+ζ = ψ+ζ φορ αλλ νυμβερσ'. a:sym is used only for U+F0xx characters (`_symbol_font`)
    (Pinned by: test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText::
    test_pptx_letters_typed_after_an_inserted_symbol_stay_letters, test_pptx_plain_letters_follow_the_latin_typeface_only)."""
    font = properties.find(f"{{{_DRAWING}}}latin") if properties is not None else None
    return font.get("typeface") if font is not None else None


def _render_pptx_table(table: object, extraction: Extraction, inline: _Inline | None = None) -> str:
    """The merge rules are the same as in the other formats (TableGrid: vertical repeats, horizontal first cell). Cells covered by a
    merge (hMerge, vMerge) are filled by the starting cell.

    Merge information is read directly from the attribute text of the cell (a:tc). python-pptx's `is_spanned` and `span_width` raised
    ValueError/InvalidXmlError on non-numeric values ('x', '²', '1.5') and on values outside true/false ('maybe') and rejected the whole
    document. An odd value counts as no merge
    (Pinned by: test_office.py::TestOneBrokenShapeDoesNotRejectTheDeck::test_table_cell_with_an_odd_merge_value_is_read_as_a_plain_cell).

    Rows are iterated over the `a:tr` list, fetched once, instead of `table.rows`. `_RowCollection` has no iterator, so the list was
    rebuilt from the start for every row and took the square of the row count
    (8,000 rows took 21 seconds, Pinned by: test_office_round43.py::TestPptxTableRows::test_many_rows_take_linear_time).
    """
    grid = TableGrid()

    for row_index, tr in enumerate(table._tbl.tr_lst):
        row = _Row(tr, table.rows)
        for col_index, cell in enumerate(row.cells):
            element = cell._tc
            if element.get("hMerge") in _TRUE or element.get("vMerge") in _TRUE:
                # A covered cell is inside the table too — the last row is not removed even when all of it is covered
                grid.reach(row_index, col_index)
                continue
            text = "\n".join(_cell_paragraph_text(paragraph, inline) for paragraph in cell.text_frame.paragraphs)
            grid.place(row_index, col_index, text, row_span=_span(element, "rowSpan"), col_span=_span(element, "gridSpan"))
    rendered = grid.render()
    if grid.omitted_rows():
        extraction.warnings.append(omitted_rows_warning(grid.omitted_rows()))
    return rendered


def _cell_paragraph_text(paragraph: object, inline: _Inline | None = None) -> str:
    """Text of a table-cell paragraph. A lower level (a:pPr lvl) gets the same `cell_level_mark` ('└ ') in front as DOCX and HWP cell
    lists. Leading spaces inside a cell are removed when the table is built, so sub-items were flattened (Pinned by:
    test_office_cells_and_notes.py::TestPptxTableCellLevels). Bullet characters and numbers are not attached (cell text has always
    been read without bullets)."""
    text = _paragraph_text(paragraph, inline)
    return cell_level_mark(_paragraph_level(paragraph)) + text if text.strip() else text


def _span(cell: object, name: str) -> int:
    """Span count of a cell (rowSpan, gridSpan). 1 if it is not a decimal number or is 0. A count larger than the table is clipped
    to the table's extent by `TableGrid`."""
    return decimal_int(cell.get(name), 1) or 1


def _picture_blob(shape: object) -> bytes | None:
    """Image bytes of a picture shape or picture placeholder. A picture that is only linked has no file, so None."""
    try:
        return shape.image.blob
    except Exception:  # noqa: BLE001 — a shape that is not a picture (AttributeError), a broken relationship (KeyError), a picture
        # shape without picture data (python-pptx InvalidXmlError), etc. Failing to read one picture must not reject the whole document
        # (Pinned by: test_office.py::test_picture_shape_without_image_data_does_not_reject_the_deck).
        return None


def _picture_fill(shape: object) -> bytes | None:
    """Image bytes of a shape's fill picture (p:spPr/a:blipFill). The same direction as DOCX registering a:blip as a picture
    (Pinned by: test_office.py::TestPptxSideLabelsAndPictureFills::test_shape_filled_with_a_picture_is_registered_as_a_picture).

    A tiled fill (a:tile) is a pattern/texture and is not registered (deliberate. Pinned by:
    test_tiled_picture_fill_is_a_texture_and_is_not_registered). Small pictures and pictures used many times (logos) are removed as
    decoration by the converter's picture filter. The slide background (p:bg) is not a shape and is not looked at.
    """
    fill = shape._element.find(f"{{{_PRESENTATION}}}spPr/{{{_DRAWING}}}blipFill")
    if fill is None or fill.find(f"{{{_DRAWING}}}tile") is not None:
        return None
    blip = fill.find(f"{{{_DRAWING}}}blip")
    relationship_id = blip.get(f"{{{_RELATIONSHIPS}}}embed") if blip is not None else None
    if not relationship_id:
        return None
    try:
        return shape.part.related_part(relationship_id).blob
    except (KeyError, ValueError):  # broken relationship or external (linked) picture. One picture must not reject the whole document
        return None


def _shape_alt(shape: object) -> str | None:
    properties = next(shape._element.iter("{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr"), None)
    if properties is None:
        return None
    return properties.get("descr") or None
