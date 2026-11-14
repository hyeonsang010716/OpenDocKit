"""The definitions a DOCX body refers to: paragraph styles with their outline levels and numbering, and the numbering
definitions with the number format, start and indent of each level.
"""

from dataclasses import dataclass

from lxml import etree

from opendockit.extractors._docx_runs import _int_attr, _int_value, _is_on, _w
from opendockit.extractors._lists import DOCX_NUMBER_SHAPES, NumberLevel, start_number
from opendockit.extractors._package import decimal_int, parse_part


@dataclass(slots=True)
class _Style:
    style_id: str
    name: str
    outline_level: int | None
    num_id: str | None
    ilvl: int
    based_on: str | None
    indent: dict[str, int]  # left indent written directly on the style (`_indent_values`)
    size: int | None  # font size written directly on the style (w:sz, half-points). The paragraph size follows basedOn (`_paragraph_size`)
    default: bool  # the default paragraph style (w:default), used by paragraphs with no paragraph style set


# Number level definition w:lvl → (numFmt, lvlText, start number, legal-style numbering w:isLgl, restart w:lvlRestart — None means the
# default). `_level_definition`
type _LevelDefinition = tuple[str, str, int, bool, int | None]


@dataclass(slots=True)
class _NumberList:
    """One numbered list (w:num). Its level definitions are those of the number definition (w:abstractNum) it points at."""

    abstract_id: str
    levels: dict[int, _LevelDefinition]  # level → level definition. Includes levels replaced by w:lvlOverride/w:lvl
    style_levels: dict[str, int]  # paragraph style id → the level bound to that style (w:lvl/w:pStyle)
    restarts: dict[int, int]  # start-number overrides (the w:startOverride of w:lvlOverride): level → start number
    indents: dict[int, dict[str, int]]  # level → left indent of that level definition (w:lvl/w:pPr/w:ind) (`_indent_values`)


def _outline_to_heading(level: int) -> int | None:
    """w:outlineLvl starts at 0, and 9 is body text."""
    return min(level + 1, 6) if 0 <= level < 9 else None


def _raw_part_root(part: object, kind: str) -> etree._Element | None:
    """Root of the styles or numbering part (`kind`) parsed from its bytes, None when it is not XML. A part declared with a generic
    content type (application/xml) comes back from python-docx as a plain `Part` with no parsed tree, and asking it for its styles
    or element raised AttributeError, which rejected the whole document. It is read the way the footnotes and comments parts are
    (Pinned by: test_audit_office.py::TestDocxStylesAndNumberingWithAGenericContentType)."""
    related = next((rel.target_part for rel in part.rels.values() if not rel.is_external and rel.reltype.endswith(f"/{kind}")), None)
    blob = getattr(related, "blob", None)
    return parse_part(blob) if blob else None


def _styles_root(document: object) -> etree._Element | None:
    try:
        return document.styles.element
    except AttributeError:
        return _raw_part_root(document.part, "styles")


def _load_styles(document: object) -> dict[str, _Style]:
    styles: dict[str, _Style] = {}
    root = _styles_root(document)
    for style in root.iterchildren(_w("style")) if root is not None else ():
        style_id = style.get(_w("styleId"))
        if not style_id:
            continue
        name = style.find(_w("name"))
        outline = style.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        num_pr = style.find(f"{_w('pPr')}/{_w('numPr')}")
        num_id = num_pr.find(_w("numId")) if num_pr is not None else None
        based_on = style.find(_w("basedOn"))
        styles[style_id] = _Style(
            style_id=style_id,
            name=name.get(_w("val"), "") if name is not None else "",
            outline_level=_int_attr(outline, 9) if outline is not None else None,
            num_id=num_id.get(_w("val")) if num_id is not None else None,
            ilvl=_int_attr(num_pr.find(_w("ilvl")) if num_pr is not None else None, 0),
            based_on=based_on.get(_w("val")) if based_on is not None else None,
            indent=_indent_values(style.find(f"{_w('pPr')}/{_w('ind')}")),
            size=_int_attr(style.find(f"{_w('rPr')}/{_w('sz')}"), 0) or None,
            default=style.get(_w("type")) == "paragraph" and style.get(_w("default")) in ("1", "true", "on"),
        )
    return styles


def _load_numbering(document: object) -> dict[str, _NumberList]:
    """numId → numbered list. Used to tell bullets from numbered lists and to build number strings such as "제%1조".

    A number definition that points at a numbering style (w:numStyleLink) has empty level definitions. It is read as the number
    definition that defines that style (w:styleLink) instead — left as it was, it became bullets
    (Pinned by: test_office.py::TestDocxNumbering::test_definition_linked_to_a_numbering_style_uses_that_style_levels).
    The start-number override of a w:num (w:lvlOverride/w:startOverride) is kept in `_NumberList.restarts`
    (Pinned by: test_office.py::TestDocxObjects::test_numbering_uses_start_and_level_text).
    When a w:lvlOverride holds a whole level definition (w:lvl), the look of that level (number text, format) changes for that
    list only. With only startOverride read, '제3조' came out as '3.'. The start number of the definition (w:start) does not restart
    the count — only startOverride does. Restarting on a definition alone turned 'B', 'C', '4' of a file saved by Word (Tika
    testWORD_override_list_numbering.docx 'Test 4') into 'A', 'B', '3'
    (Pinned by: test_office.py::TestDocxNumbering::test_level_override_with_its_own_definition_replaces_the_level,
    test_level_override_restarts_only_with_a_start_override).
    """
    try:
        numbering = document.part.numbering_part.element
    except (KeyError, NotImplementedError):
        return {}
    except AttributeError:
        numbering = _raw_part_root(document.part, "numbering")
        if numbering is None:
            return {}

    abstract_levels: dict[str, dict[int, _LevelDefinition]] = {}
    abstract_indents: dict[str, dict[int, dict[str, int]]] = {}
    style_levels: dict[str, dict[str, int]] = {}
    style_definitions: dict[str, str] = {}  # numbering style name → the abstractNumId that defines that style
    style_links: dict[str, str] = {}  # abstractNumId → the numbering style name it points at
    for abstract in numbering.iterchildren(_w("abstractNum")):
        abstract_id = abstract.get(_w("abstractNumId"), "")
        levels, linked_styles, indents = {}, {}, {}
        for level in abstract.iterchildren(_w("lvl")):
            index = _int_value(level.get(_w("ilvl")), 0)
            levels[index] = _level_definition(level, index)
            indents[index] = _indent_values(level.find(f"{_w('pPr')}/{_w('ind')}"))
            style = level.find(_w("pStyle"))
            if style is not None:
                linked_styles[style.get(_w("val"), "")] = index
        abstract_levels[abstract_id], style_levels[abstract_id], abstract_indents[abstract_id] = levels, linked_styles, indents
        definition, link = abstract.find(_w("styleLink")), abstract.find(_w("numStyleLink"))
        if definition is not None:
            style_definitions[definition.get(_w("val"), "")] = abstract_id
        if link is not None:
            style_links[abstract_id] = link.get(_w("val"), "")

    lists = {}
    for num in numbering.iterchildren(_w("num")):
        abstract_ref = num.find(_w("abstractNumId"))
        if abstract_ref is None:
            continue
        abstract_id = abstract_ref.get(_w("val"), "")
        abstract_id = style_definitions.get(style_links.get(abstract_id, ""), abstract_id)
        levels, restarts, indents = abstract_levels.get(abstract_id, {}), {}, abstract_indents.get(abstract_id, {})
        for override in num.iterchildren(_w("lvlOverride")):
            index = _int_value(override.get(_w("ilvl")), -1)
            if not 0 <= index <= 8:
                continue
            level, start = override.find(_w("lvl")), override.find(_w("startOverride"))
            if level is not None:
                levels = {**levels, index: _level_definition(level, index)}
                indents = {**indents, index: _indent_values(level.find(f"{_w('pPr')}/{_w('ind')}"))}
            if start is not None:
                restarts[index] = start_number(_int_attr(start, 1))
        lists[num.get(_w("numId"), "")] = _NumberList(abstract_id, levels, style_levels.get(abstract_id, {}), restarts, indents)
    return lists


def _level_definition(level: etree._Element, index: int) -> _LevelDefinition:
    """w:lvl → (numFmt, lvlText, start number, legal-style numbering w:isLgl, restart w:lvlRestart). Shared by number definitions
    (w:abstractNum) and the level overrides of a list (w:lvlOverride).

    w:lvlRestart was not read, so the count restarted from 1 whenever a higher level appeared — '제N조' (Article N) running on across
    chapters was '제1조' in every chapter. The value is the deepest higher level that restarts the count (from 1); 0 means never
    restart. A missing or unreadable value is the default (the level right above)
    (Pinned by: test_office_revisions_and_numbering.py::TestDocxLevelRestart)."""
    number_format, text, restart = level.find(_w("numFmt")), level.find(_w("lvlText")), level.find(_w("lvlRestart"))
    restart_after = _int_attr(restart, -1) if restart is not None else -1
    return (number_format.get(_w("val"), "decimal") if number_format is not None else "decimal",
            text.get(_w("val"), "") if text is not None else f"%{index + 1}.",
            start_number(_int_attr(level.find(_w("start")), 1)),
            _is_on(level.find(_w("isLgl"))),
            restart_after if 0 <= restart_after <= index else None)


def _indent_values(indent: etree._Element | None) -> dict[str, int]:
    """Left indent of a w:ind: "left" (w:left / w:start, twips) and "chars" (w:leftChars / w:startChars, 1/100 character). Only what
    is written is included — each attribute is overridden in the order paragraph → number level → style
    (`_DocxRenderer._paragraph_indent`)."""
    values: dict[str, int] = {}
    if indent is None:
        return values
    for key, names in (("left", ("left", "start")), ("chars", ("leftChars", "startChars"))):
        value = next((indent.get(_w(name)) for name in names if indent.get(_w(name)) is not None), None)
        number = decimal_int(value, None, signed=True)
        if number is not None:
            values[key] = number
    return values


def _number_levels(levels: dict[int, _LevelDefinition]) -> list[NumberLevel]:
    """Number definitions of levels 0–8. A missing level is "%n." in Arabic digits. An unknown number format is written in Arabic
    digits too."""
    defined = (levels.get(index, ("decimal", f"%{index + 1}.", 1, False, None)) for index in range(9))
    return [NumberLevel(text, DOCX_NUMBER_SHAPES.get(number_format, "DIGIT"), start, legal, restart_after)
            for number_format, text, start, legal, restart_after in defined]
