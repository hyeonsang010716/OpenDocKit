"""Which page of the page tree pdfium opens at each of its page numbers, so that a page the drawn-object estimate refused is never
opened by pdfium under another number.

pdfminer and pdfium number the pages of an odd page tree differently. pdfminer leaves out a `/Kids` entry that is no page (a
reference to a missing object, a dictionary of another kind) and a page listed twice; pdfium keeps a number for each of them,
lists a page as often as it is listed and pads to the root's `/Count`. With a missing object listed first, every pdfium number
was one off, so the page after a refused one was opened as that page: a form drawing itself twice on page 6 of 12 took 3GB in 3.7
seconds (Pinned by: test_pdf_resource_limits.py::TestPagesPdfiumNumbersOtherwise).
"""

from collections.abc import Callable
import ctypes

from pdfminer.pdftypes import PDFObjRef, resolve1
import pypdfium2.raw as pdfium_c

from opendockit.exceptions import DocumentTooLargeError
from opendockit.extractors._pdf_model import _PDFIUM_LOCK
from opendockit.extractors._pdf_pdfminer import _PAGE_TREE_LEVELS, _name


# Page numbers of pdfium worked out at most (a page tree of more entries, or a `/Count` above this, is not followed: such a
# document is treated as one whose numbering cannot be matched).
_PAGES_FOLLOWED = 100_000


class _TreePage:
    """A page dictionary of the page tree as `_page_drawing` reads a page (`page_obj.attrs`, `.resources`, `.raw`)."""

    __slots__ = ("attrs", "raw", "resources")

    def __init__(self, raw: dict, inherited: object) -> None:
        """`inherited`: the resources inherited along the path to the page, used when it has none of its own."""
        self.attrs, self.raw = raw, raw
        self.resources = _resolved(raw["Resources"]) if "Resources" in raw else inherited

    @property
    def page_obj(self) -> "_TreePage":
        return self


def _resolved(value: object) -> object:
    try:
        return resolve1(value)
    except DocumentTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — what pdfium cannot resolve is no page either
        return None


def _pdfium_slots(root: object) -> list[tuple[int | None, dict | None, object]] | None:
    """(object number, page dictionary or None, resources inherited along the path) for each page number pdfium gives, worked
    out as pdfium walks the tree (checked against pypdfium2 4.30 and 5.x on every odd entry the tests build): an entry that is no
    dictionary takes a number it cannot open; a dictionary with `/Kids` is a node — gone through when they are an array (left out
    when they are not, or when the node is one of the nodes it lies inside, or deeper than `_PAGE_TREE_LEVELS`), whatever its
    `/Type`; any other dictionary takes a number (one of `/Type /Pages` is opened by pdfium 4.30 as an empty page, not by 5.x —
    `_opened_by_some`); a page listed twice takes two. The numbers are padded with ones it cannot open, or cut, to the root's
    `/Count` when that is a positive integer. None when the tree has more than `_PAGES_FOLLOWED` entries."""
    root = _resolved(root)
    if not isinstance(root, dict):
        return []
    slots: list[tuple[int | None, dict | None, object]] = []
    kids = _resolved(root.get("Kids"))
    stack = [(iter(kids if isinstance(kids, list) else ()), root.get("Resources"), id(root))]
    inside = {id(root)}
    while stack:
        if len(slots) > _PAGES_FOLLOWED:
            return None
        entries, inherited, node = stack[-1]
        entry = next(entries, stack)
        if entry is stack:
            inside.discard(node)
            stack.pop()
            continue
        number = entry.objid if isinstance(entry, PDFObjRef) else None
        kid = _resolved(entry)
        if not isinstance(kid, dict):
            slots.append((number, None, None))
        elif "Kids" in kid:
            nested = _resolved(kid["Kids"])
            if isinstance(nested, list) and id(kid) not in inside and len(stack) < _PAGE_TREE_LEVELS:
                inside.add(id(kid))
                stack.append((iter(nested), kid.get("Resources", inherited), id(kid)))
        else:
            slots.append((number, kid, inherited))
    count = _resolved(root.get("Count"))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        if count > _PAGES_FOLLOWED:
            return None
        slots = slots[:count] + [(None, None, None)] * (count - len(slots))
    return slots


def _opened_by_some(page: dict | None) -> bool:
    """Whether pdfium opens the dictionary at its number in some versions only: one of `/Type /Pages` without `/Kids`."""
    return page is not None and _name(page.get("Type")) == "Pages"


def _reachable_pages(root: object) -> list[tuple[dict, object]] | None:
    """Every dictionary without `/Kids` reachable from the root through `/Kids` of any dictionary, each once with the resources
    of one path to it: more than pdfium can open at any number. None past `_PAGES_FOLLOWED`."""
    pending, seen, found = [(root, None)], set(), []
    while pending:
        node, inherited = pending.pop()
        node = _resolved(node)
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        if len(seen) > _PAGES_FOLLOWED:
            return None
        if "Kids" in node:
            nested = _resolved(node["Kids"])
            if isinstance(nested, list):
                pending.extend((kid, node.get("Resources", inherited)) for kid in nested)
        else:
            found.append((node, inherited))
    return found


class _PdfiumPages:
    """pdfium's number for each page pdfminer reads, and whether pdfium may open any page at all.

    The numbering worked out (`_pdfium_slots`) is used when it has as many numbers as pdfium has pages and pdfium can open exactly
    the numbers it gives a dictionary (asked with `FPDF_GetPageSizeByIndexF`, which reads the page dictionary but not its content;
    a dictionary only some versions open is not asked about): a page pdfminer reads is then opened at the first number of its
    object, and a page pdfium does not number is not opened. Otherwise every page dictionary reachable through `/Kids` is estimated
    first (`_reachable_pages`): when none is past the bounds, pages are opened at pdfminer's numbers as before; when one is, pdfium
    could open it under any number, so pdfium opens no page (`closed`): the pages are read by pdfminer alone, within the bounds of
    the drawn-object estimate, and a document whose page list pdfminer could not build has none read. Pinned by:
    test_pdf_resource_limits.py::TestPagesPdfiumNumbersOtherwise."""

    __slots__ = ("checked", "closed", "numbers", "slots")

    def __init__(self, root: object, document: object, past: Callable[[_TreePage], bool]) -> None:
        """`past`: whether drawing a page is past the bounds (`_page_drawing`)."""
        self.slots = _pdfium_slots(root)
        self.checked = self.slots is not None and _numbers_match(self.slots, document)
        self.closed = False
        self.numbers: dict[int, int] = {}
        if self.checked:
            for index, (number, page, _) in enumerate(self.slots):
                if number is not None and page is not None and not _opened_by_some(page):
                    self.numbers.setdefault(number, index)
            return
        reachable = _reachable_pages(root)
        self.closed = reachable is None or any(past(_TreePage(page, _resolved(inherited))) for page, inherited in reachable)

    def index(self, number: int | None, fallback: int) -> int | None:
        """pdfium's number of the page of object `number` that pdfminer numbers `fallback` (counted from 0); None when pdfium must
        not open it."""
        if self.closed:
            return None
        if not self.checked:
            return fallback
        return self.numbers.get(number) if number is not None else None

    def refused(self, index: int, past: Callable[[_TreePage], bool]) -> bool:
        """Whether pdfium must not open its page `index` (for a document whose page list pdfminer could not build)."""
        if self.closed:
            return True
        if not self.checked or index >= len(self.slots):
            return False
        _, page, inherited = self.slots[index]
        return page is not None and past(_TreePage(page, _resolved(inherited)))


def _numbers_match(slots: list[tuple[int | None, dict | None, object]], document: object) -> bool:
    """Whether pdfium has as many pages as `slots` and can open exactly those that hold a page dictionary."""
    size = pdfium_c.FS_SIZEF()
    with _PDFIUM_LOCK:
        if len(document) != len(slots):
            return False
        return all(_opened_by_some(page)
                   or bool(pdfium_c.FPDF_GetPageSizeByIndexF(document.raw, index, ctypes.byref(size))) == (page is not None)
                   for index, (_, page, _) in enumerate(slots))
