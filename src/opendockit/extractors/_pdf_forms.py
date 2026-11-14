"""What is not in the page content: the values of a fillable form (AcroForm), free-text annotations and attached files."""


from bisect import bisect_left, bisect_right
import math

from pdfminer.pdftypes import (
    resolve1,
)
from pdfminer.utils import apply_matrix_pt

from opendockit.extractors._pdf_model import _info_text, _Line


_ATTACHMENT_TREE_NODES = 10_000  # number of nodes to look at in the attachment name tree
# Candidate body lines a page may look at in all while finding the labels of its FreeText annotations (`_LinesBeside`).
_LABEL_SEARCH_WORK = 2_000_000


# Form field flags (/Ff): password field (bit 14), radio (bit 16), push button (bit 17). Maximum number of levels to follow
# `/Parent` upwards.
_FIELD_PASSWORD = 1 << 13
_FIELD_RADIO = 1 << 15
_FIELD_PUSH_BUTTON = 1 << 16
_FIELD_PARENT_DEPTH = 8


def _form_values(page: object, lines: list[_Line]) -> list[str]:
    """Return the values entered in the page's form fields (AcroForm widget annotations) as 'name: value' lines (top to bottom, left
    to right).

    The values of a fillable form are not in the page content but in the annotation's `/V`, so they do not appear in the character
    extraction of pdfminer or pdfium. Only the labels remained and the values were dropped without a warning (Pinned by:
    test_pdf.py::TestFormFields::test_filled_form_values_are_appended_to_their_page).
    - The name is `/TU` (the name shown to the user), or `/T` when absent. Field attributes are inherited from `/Parent` (radio
      groups).
    - Text and choice fields (`/Tx`, `/Ch`) are included only when they have a value, checkboxes as on ☑ / off ☐, and for radio
      buttons only the value of the button that is on. A checkbox field with two or more widgets ('agree / do not agree' under the
      same name) contributes only the export value of the widget that is on, like a radio group — as a ☐·☑ pair the information
      about which one was chosen was lost (Pinned by: test_pdf_form_fields.py::TestGroupedFormFields).
      Push buttons, signatures and password fields are not included.
    - The values are placed at the end of the page instead of being inserted next to their labels (deliberate): fields sit inside
      tables as well as beside body lines, and inserting them at a guessed position unsettles the table and reading-order decisions.
      The field name serves as the label.
    - Text written with a viewer's 'add text' tool (Acrobat Fill & Sign, macOS Preview) is the `/Contents` of a FreeText annotation
      and was dropped for the same reason. It has no name, so the body line (`lines`) immediately to its left on the same row is
      attached as the label ('Name: Hong Gildong', `_free_text`). Hidden annotations (Hidden in `/F`) are not included (Pinned by:
      test_pdf_form_fields.py::TestFreeTextAnnotations).
    pdfplumber's `page.annots` is not used — while resolving annotations all the way down it raised on dictionaries that refer to each
    other (XFA form PDF).
    """
    try:
        placed = []
        beside: _LinesBeside | None = None  # built on the first FreeText annotation
        for reference in resolve1(page.page_obj.annots) or []:
            # One annotation that cannot be read (a `/Rect` holding something that is not a number) is skipped on its own — it
            # used to drop the values of every other field of the page. The numbers of `/Rect` may be indirect objects
            # (Pinned by: test_audit_pdf_head.py::TestOneUnreadableAnnotation).
            try:
                widget = resolve1(reference)
                subtype = _name_of(widget.get("Subtype")) if isinstance(widget, dict) else None
                rect = resolve1(widget.get("Rect")) if subtype in ("Widget", "FreeText") else None
                if not isinstance(rect, list) or len(rect) != 4:
                    continue
                x0, x1 = sorted(float(resolve1(value)) for value in rect[0::2])
                low, high = sorted(float(resolve1(value)) for value in rect[1::2])
                if subtype == "FreeText" and beside is None:
                    beside = _LinesBeside(lines)
                text = _field_text(widget) if subtype == "Widget" else _free_text(page, widget, (x0, low, x1, high), beside)
                if text:
                    placed.append((-high, x0, text))
            except Exception:  # noqa: BLE001 — the other annotations of the page are still read
                continue
        return [text for _, _, text in sorted(placed)]
    except Exception:  # noqa: BLE001 — the page body is still read even if the annotations cannot be parsed
        return []


def _free_text(page: object, annotation: dict, rect: tuple[float, float, float, float], lines: "_LinesBeside") -> str | None:
    """Text of a FreeText annotation. Among the body lines on the same row (vertical overlap of at least half the height of the
    shorter one) that end before the left edge of the annotation box, the nearest one is attached as the label ('Name:' →
    'Name: value'). None for a hidden annotation (`/F` bit 2).

    The annotation box is in PDF user space; it is moved into the coordinates of the page's lines the way pdfminer and pdfplumber
    move characters (`_page_box`). Measured from the paper's top edge, the box missed its label on every page whose MediaBox does
    not start at 0,0 or that is rotated (Pinned by: test_audit_pdf_text.py::TestFreeTextLabelsOnShiftedAndRotatedPages)."""
    flags = resolve1(annotation.get("F"))
    value = " ".join(_field_string(resolve1(annotation.get("Contents"))).split())
    if not value or (isinstance(flags, int) and flags & 2):
        return None
    box = _page_box(page, rect)
    label = lines.label(*box) if box is not None else None
    if label is None:
        return value
    return f"{label.text.rstrip(': ')}: {value}"


def _page_box(page: object, rect: tuple[float, float, float, float]) -> tuple[float, float, float] | None:
    """(left, top, bottom) of the user-space box `rect` (x0, y0, x1, y1) in the coordinates of the page's characters: pdfminer's
    page matrix (MediaBox origin and `/Rotate`), then pdfplumber's flip with its MediaBox offset. None if the page box cannot be
    read."""
    box = page.page_obj.mediabox
    if not (isinstance(box, list | tuple) and len(box) == 4 and all(isinstance(value, int | float) for value in box)):
        return None
    x0, y0, x1, y1 = (float(value) for value in box)
    rotate = page.page_obj.rotate
    matrix = ((0, -1, 1, 0, -y0, x1) if rotate == 90 else (-1, 0, 0, -1, x1, y1) if rotate == 180
              else (0, 1, -1, 0, y1, -x0) if rotate == 270 else (1, 0, 0, 1, -x0, -y0))
    corners = [apply_matrix_pt(matrix, point) for point in ((rect[0], rect[1]), (rect[2], rect[3]))]
    left, top = float(page.mediabox[0]), float(page.mediabox[1])
    height = float(page.height)
    return (min(x for x, _ in corners) + left, height - max(y for _, y in corners) + top,
            height - min(y for _, y in corners) + top)


class _LinesBeside:
    """Index of a page's body lines by top edge, for finding the label of a FreeText annotation (`_free_text`). `label` gives
    exactly what scanning every line gives (the first line, in page order, with the largest right edge among those beside the
    box) — until the page has looked at `_LABEL_SEARCH_WORK` candidate lines in all; after that the remaining annotations get
    no label (their values still come out, without 'label: ').

    Every annotation scanned every line, so a 38KB PDF with 200,000 FreeText annotations over 1,800 lines took 52 seconds inside
    one page, where the time budget never looks. The index alone does not help when the boxes are as tall as the page (every
    line is a candidate), hence the work bound; a page of real annotations looks at a few lines each (Pinned by:
    test_audit_pdf_text.py::TestManyFreeTextAnnotations). Lines taller than `_TALL` pt or with coordinates that are not finite
    numbers are kept aside and always looked at."""

    _TALL = 64.0

    def __init__(self, lines: list[_Line]) -> None:
        self._lines = lines
        self._work = _LABEL_SEARCH_WORK
        short = []
        self._tall = []
        for index, line in enumerate(lines):
            if math.isfinite(line.top) and math.isfinite(line.bottom) and line.bottom - line.top <= self._TALL:
                short.append((line.top, index))
            else:
                self._tall.append(index)
        short.sort()
        self._tops = [top for top, _ in short]
        self._order = [index for _, index in short]

    def label(self, x0: float, top: float, bottom: float) -> _Line | None:
        def beside(line: _Line) -> bool:
            overlap = min(line.bottom, bottom) - max(line.top, top)
            return line.x1 <= x0 + 1 and overlap >= min(line.height, bottom - top) / 2

        # A line beside the box overlaps it (overlap ≥ 0), so it starts at most at the box bottom and, being short, at most
        # `_TALL` above the box top
        start, end = bisect_left(self._tops, top - self._TALL), bisect_right(self._tops, bottom)
        if end - start + len(self._tall) > self._work:
            self._work = 0
            return None
        self._work -= end - start + len(self._tall)
        candidates = sorted([*self._order[start:end], *self._tall])
        return max((self._lines[index] for index in candidates if beside(self._lines[index])),
                   key=lambda line: line.x1, default=None)


def _field_text(widget: dict) -> str | None:
    """'name: value' of one form widget. None if there is no value to include."""
    kind, flags = _name_of(_inherited(widget, "FT")), _inherited(widget, "Ff")
    flags = flags if isinstance(flags, int) else 0
    if kind == "Btn":
        state = _name_of(widget.get("AS")) or _name_of(_inherited(widget, "V")) or "Off"
        grouped = flags & _FIELD_RADIO or _widget_index(widget) is not None
        if flags & _FIELD_PUSH_BUTTON or (grouped and state == "Off"):
            return None
        value = _export_value(widget, state) if grouped else "☐" if state == "Off" else "☑"
    elif kind == "Tx" and not flags & _FIELD_PASSWORD:
        value = _field_string(_inherited(widget, "V"))
    elif kind == "Ch":
        value = _choice_text(widget)
    else:
        return None

    name = _field_string(_inherited(widget, "TU")) or _field_string(_inherited(widget, "T"))
    if not value:
        return None
    return f"{name}: {value}" if name else value


def _choice_text(widget: dict) -> str:
    """Displayed text of the value chosen in a choice field (`/Ch`). `/V` is the export value, so it is mapped through the
    [export value, displayed text] pairs of `/Opt` (single-text entries stay as they are). It used to come out as 'Region: 02'
    (Pinned by: test_pdf_form_fields.py::TestChoiceFieldOptions)."""
    options = _inherited(widget, "Opt")
    shown: dict[str, str] = {}

    for option in options if isinstance(options, list) else []:
        pair = resolve1(option)
        if isinstance(pair, list) and len(pair) == 2:
            shown.setdefault(_field_string(resolve1(pair[0])), _field_string(resolve1(pair[1])))

    selected = _inherited(widget, "V")
    texts = (_field_string(resolve1(item)) for item in (selected if isinstance(selected, list) else [selected]))
    return ", ".join(shown.get(text) or text for text in texts if text)


def _widget_index(widget: dict) -> int | None:
    """Index of the widget among the several `/Kids` of its field (`/Parent`). None if it is not a widget of a field with two or more
    widgets."""
    parent = resolve1(widget.get("Parent"))
    kids = resolve1(parent.get("Kids")) if isinstance(parent, dict) and widget.get("T") is None else None
    if not isinstance(kids, list) or len(kids) < 2:
        return None
    return next((index for index, kid in enumerate(kids) if resolve1(kid) is widget), None)


def _export_value(widget: dict, state: str) -> str:
    """Export value of a button or checkbox that is on: if the field has `/Opt`, the entry at the widget's position (`/Kids` order,
    or the ordinal state name '/1' when it is not found), otherwise the state name. Generators that use `/Opt` write the state name
    as an ordinal, which gave 'grade: 1' (Pinned by: test_pdf_form_fields.py::TestGroupedFormFields)."""
    options = _inherited(widget, "Opt")
    index = _widget_index(widget)
    if index is None and state.isdecimal():
        index = int(state)
    if isinstance(options, list) and index is not None and 0 <= index < len(options):
        return _field_string(resolve1(options[index])) or state
    try:
        return _info_text(state.encode("latin-1"))  # bytes of the name object (a state name written in EUC-KR or UTF-8)
    except UnicodeEncodeError:
        return state  # a name pdfminer has already decoded to text


def _inherited(widget: dict, key: str) -> object:
    """Field attribute. If the widget does not have it, follow `/Parent` upwards to find it (up to `_FIELD_PARENT_DEPTH` levels, to
    guard against documents that refer to each other)."""
    node: object = widget
    for _ in range(_FIELD_PARENT_DEPTH):
        if not isinstance(node, dict):
            break
        if node.get(key) is not None:
            return resolve1(node[key])
        node = resolve1(node.get("Parent"))
    return None


def _name_of(value: object) -> str | None:
    """Name of a PDF name object (`/Widget`). None if it is not a name."""
    name = getattr(resolve1(value), "name", None)
    return name.decode("latin-1") if isinstance(name, bytes) else name


def _field_string(value: object) -> str:
    """Field name or value as one line of text. A choice field with several selections is joined with commas."""
    if isinstance(value, list):
        return ", ".join(part for part in (_field_string(resolve1(item)) for item in value) if part)
    text = _info_text(value) if isinstance(value, bytes) else _name_of(value) or ""
    return " ".join(text.split())


def _annotated_attachments(page: object, found: dict[int, str]) -> None:
    """Add the attachments referenced by the page's FileAttachment annotations to `found` (file specification id → name)."""
    try:
        for reference in resolve1(page.annots) or []:
            annotation = resolve1(reference)
            if isinstance(annotation, dict) and _name_of(annotation.get("Subtype")) == "FileAttachment":
                _add_attachment(resolve1(annotation.get("FS")), None, found)
    except Exception:  # noqa: BLE001 — the page body is still read even if the annotations cannot be parsed
        return


def _named_attachments(catalog: dict, found: dict[int, str]) -> None:
    """Add the attachments in the document catalog's `/Names /EmbeddedFiles` name tree to `found` (following `/Kids`, up to
    `_ATTACHMENT_TREE_NODES` nodes, visiting a reference that comes back only once).

    Attachments are not page content and were dropped without a warning. Their names and count are reported like the attachments of
    an .eml (Pinned by: test_pdf_attachments.py::TestEmbeddedFiles). The attachments themselves are not read (deliberate) — their
    formats vary, and they are converted when uploaded separately.
    """
    try:
        names = resolve1(catalog.get("Names"))
        pending = [resolve1(names.get("EmbeddedFiles"))] if isinstance(names, dict) else []
        seen: set[int] = set()
        while pending and len(seen) < _ATTACHMENT_TREE_NODES:
            node = pending.pop()
            if not isinstance(node, dict) or id(node) in seen:
                continue
            seen.add(id(node))
            pairs = resolve1(node.get("Names"))
            if isinstance(pairs, list):
                for key, value in zip(pairs[::2], pairs[1::2], strict=False):
                    _add_attachment(resolve1(value), resolve1(key), found)
            kids = resolve1(node.get("Kids"))
            if isinstance(kids, list):
                pending.extend(resolve1(kid) for kid in kids)
    except Exception:  # noqa: BLE001 — the body is still read even if the name tree cannot be parsed
        return


def _add_attachment(spec: object, key: object, found: dict[int, str]) -> None:
    """Put one file specification into `found` with its name (`/UF` → `/F` → name tree key)."""
    if isinstance(spec, dict):
        name = next((text for text in (_field_string(resolve1(spec.get(field))) for field in ("UF", "F")) if text), "")
        found.setdefault(id(spec), name or _field_string(key) or "unnamed")
    elif isinstance(spec, bytes):
        found.setdefault(id(spec), _field_string(spec) or "unnamed")  # a specification that is just a file name


def _has_xfa(pdf: object) -> bool:
    """Whether this is an XFA form (`/XFA` in the document catalog's `/AcroForm`)."""
    try:
        form = resolve1(pdf.doc.catalog.get("AcroForm"))
        return isinstance(form, dict) and form.get("XFA") is not None
    except Exception:  # noqa: BLE001 — if the catalog cannot be parsed, treat it as not a form
        return False
