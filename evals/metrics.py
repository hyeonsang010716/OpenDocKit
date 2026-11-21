"""Structure metrics for a converted document against a reference Markdown: reading order, tables, headings.

The three metrics are the ones the public PDF benchmark `opendataloader-bench` (Apache-2.0) defines, written here from
their definitions with the standard library, lxml and BeautifulSoup only, so that a score computed here can be set
beside the scores that benchmark publishes for other converters (`tests/test_evals_metrics.py` pins the definitions;
`evals/README.md` says how closely the published scores are reproduced).

- NID, reading order: `2 * LCS / (len(reference) + len(result))` over the characters of the whole text, tables written
  as HTML. NID-S is the same with the tables taken out.
- TEDS, tables: one minus the tree edit distance between the tables of each text, all under one root, over the larger
  number of elements. A cell renamed costs the edit distance of its text over the longer text. TEDS-S ignores the cell text.
- MHS, headings: one minus the tree edit distance between the two outlines — the headings in order, each holding the
  text under it as one node — over the larger number of nodes. MHS-S ignores the text.

One more is OpenDocKit's own and stays out of `overall`, which is the benchmark's:

- Words: `2 * shared / (words of the reference + words of the result)`, the words being the runs of letters and digits
  of each text and `shared` those the two have in common, each as often as both have it. NID compares characters and
  hardly sees a space: a word a line break cut in two ('산림 청과' for '산림청과') costs it one character in thousands,
  and costs this metric two wrong words and a missing one.

A metric is `None` when the reference has nothing to measure it by (no text, no table, no heading), and TEDS also when
the tables are too large to compare (`MAX_TABLE_ELEMENTS`).
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape, unescape
import re

from bs4 import BeautifulSoup
from lxml import html as lxml_html


try:  # the same two lengths, some hundred times faster; without it the definitions below are used (a development dependency)
    from rapidfuzz.distance import LCSseq as _fast_lcs
    from rapidfuzz.distance import Levenshtein as _fast_edit
except ImportError:  # pragma: no cover - the fallback is what the tests of the definitions run
    _fast_lcs = _fast_edit = None


# Tables of more elements than this on either side are not measured: the tree edit distance compares every pair of them,
# and a sheet of 30,000 cells saved as a document took hours. No document of the benchmark comes near it.
MAX_TABLE_ELEMENTS = 4000
_SPACES = re.compile(r"\s+")
_HTML_TABLE = re.compile(r"<table[^>]*?>.*?</table>", re.IGNORECASE | re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BREAK = re.compile(r"<br\s*/?>")


@dataclass(frozen=True, slots=True)
class Scores:
    """The scores of one document. `overall` is the mean of the NID, TEDS and MHS that could be measured."""

    nid: float | None
    nid_s: float | None
    teds: float | None
    teds_s: float | None
    mhs: float | None
    mhs_s: float | None
    words: float | None = None

    @property
    def overall(self) -> float | None:
        measured = [value for value in (self.nid, self.teds, self.mhs) if value is not None]
        return sum(measured) / len(measured) if measured else None

    def as_dict(self) -> dict[str, float | None]:
        return {"overall": self.overall, "nid": self.nid, "nid_s": self.nid_s, "teds": self.teds, "teds_s": self.teds_s,
                "mhs": self.mhs, "mhs_s": self.mhs_s, "words": self.words}


def score(reference: str, result: str) -> Scores:
    """Score a converted `result` against the `reference` Markdown of the same document."""
    reference, result = tables_as_html(reference), tables_as_html(result)
    return Scores(*reading_order(reference, result), *tables(reference, result), *headings(reference, result),
                  words(reference, result))


# Sequences ----------------------------------------------------------------------------------------------------------

def _positions(text: str) -> dict[str, int]:
    """For each character of `text`, the positions it stands at as the bits of an integer."""
    masks: dict[str, int] = {}
    for index, character in enumerate(text):
        masks[character] = masks.get(character, 0) | (1 << index)
    return masks


def lcs_length(first: str, second: str, *, fast: bool = True) -> int:
    """The length of the longest common subsequence, a row of the table at a time in one integer (Hyyrö 2004)."""
    if not first or not second:
        return 0
    if fast and _fast_lcs is not None:
        return _fast_lcs.similarity(first, second)
    if len(first) > len(second):
        first, second = second, first
    masks, full = _positions(first), (1 << len(first)) - 1
    row = full
    for character in second:
        match = row & masks.get(character, 0)
        row = ((row + match) | (row - match)) & full
    return len(first) - row.bit_count()


def edit_distance(first: str, second: str, *, fast: bool = True) -> int:
    """The Levenshtein distance, a column of the table at a time in two integers (Myers 1999, as Hyyrö 2003 writes it)."""
    if first == second:
        return 0
    if not first or not second:
        return len(first) or len(second)
    if fast and _fast_edit is not None:
        return _fast_edit.distance(first, second)
    masks, full, last = _positions(first), (1 << len(first)) - 1, 1 << (len(first) - 1)
    up, down, distance = full, 0, len(first)
    for character in second:
        equal = masks.get(character, 0) | down
        diagonal = (((equal & up) + up) ^ up) | equal
        right = (down | ~(diagonal | up)) & full
        left = diagonal & up
        if right & last:
            distance += 1
        if left & last:
            distance -= 1
        right = ((right << 1) | 1) & full
        left = (left << 1) & full
        up = (left | ~(diagonal | right)) & full
        down = right & diagonal
    return distance


def _text_cost(first: str, second: str) -> float:
    """What it costs to turn one text into the other, from 0 (the same) to 1."""
    if first == second:
        return 0.0
    return edit_distance(first, second) / max(len(first), len(second), 1)


def _squeeze(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


# Trees --------------------------------------------------------------------------------------------------------------

@dataclass(slots=True, eq=False)
class Node:
    """A node of an ordered tree: a table, a row, a cell; a document, a heading, the text under it."""

    tag: str
    text: str = ""
    spans: tuple[int, int] = (1, 1)
    children: list["Node"] = field(default_factory=list)

    def size(self) -> int:
        total, stack = 0, [self]
        while stack:
            node = stack.pop()
            total += 1
            stack.extend(node.children)
        return total


def _post_order(root: Node) -> tuple[list[Node], list[int], list[int]]:
    """The nodes in post-order, the leftmost leaf under each, and the key roots (Zhang and Shasha 1989)."""
    nodes: list[Node] = []
    leftmost: list[int] = []
    stack: list[tuple[Node, int, int]] = [(root, 0, -1)]  # node, next child, index of its leftmost leaf
    while stack:
        node, child, first = stack.pop()
        if child < len(node.children):
            stack.append((node, child + 1, first))
            stack.append((node.children[child], 0, -1))
            continue
        index = len(nodes)
        nodes.append(node)
        leftmost.append(first if first >= 0 else index)
        if stack:
            parent, next_child, parent_first = stack.pop()
            stack.append((parent, next_child, leftmost[index] if parent_first < 0 else parent_first))
    highest: dict[int, int] = {}
    for index, leaf in enumerate(leftmost):
        highest[leaf] = index
    return nodes, leftmost, sorted(highest.values())


def tree_edit_distance(first: Node, second: Node, rename: Callable[[Node, Node], float]) -> float:
    """The least cost of turning one ordered tree into the other: 1 to add or take out a node, `rename` to relabel one."""
    nodes_a, left_a, keys_a = _post_order(first)
    nodes_b, left_b, keys_b = _post_order(second)
    distance = [[0.0] * len(nodes_b) for _ in nodes_a]
    for key_a in keys_a:
        start_a = left_a[key_a]
        rows = key_a - start_a + 2
        for key_b in keys_b:
            start_b = left_b[key_b]
            columns = key_b - start_b + 2
            forest = [[0.0] * columns for _ in range(rows)]
            for row in range(1, rows):
                forest[row][0] = forest[row - 1][0] + 1
            top = forest[0]
            for column in range(1, columns):
                top[column] = top[column - 1] + 1
            for row in range(1, rows):
                index_a = start_a + row - 1
                whole_a = left_a[index_a] == start_a
                node_a, current, above = nodes_a[index_a], forest[row], forest[row - 1]
                for column in range(1, columns):
                    index_b = start_b + column - 1
                    best = min(above[column], current[column - 1]) + 1
                    if whole_a and left_b[index_b] == start_b:
                        relabelled = above[column - 1] + rename(node_a, nodes_b[index_b])
                        if relabelled < best:
                            best = relabelled
                        distance[index_a][index_b] = best
                    else:
                        split = forest[left_a[index_a] - start_a][left_b[index_b] - start_b] + distance[index_a][index_b]
                        if split < best:
                            best = split
                    current[column] = best
    return distance[-1][-1]


# Tables -------------------------------------------------------------------------------------------------------------

def _row_cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line or "|" not in line:
        return None
    cells = line.split("|")
    if line.startswith("|"):
        cells = cells[1:]
    if cells and line.endswith("|"):
        cells = cells[:-1]
    return [cell.strip() for cell in cells] or None


def _is_rule(cells: list[str]) -> bool:
    return bool(cells) and all(cell.replace(" ", "") and set(cell.replace(" ", "")) <= {"-", ":"} for cell in cells)


def _fit(cells: list[str], width: int) -> list[str]:
    if len(cells) == width:
        return cells
    if len(cells) == 3 and width > 3:  # a middle title over several columns
        return [cells[0]] + [cells[1]] * (width - 2) + [cells[2]]
    return (cells + [""] * (width - len(cells)))[:width]


def tables_as_html(markdown: str) -> str:
    """Write each pipe table of `markdown` as one line of HTML, so both kinds of table are compared alike."""
    lines = markdown.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(lines):
        head = _row_cells(lines[index])
        rule = _row_cells(lines[index + 1]) if head and index + 1 < len(lines) else None
        if not head or not rule or not _is_rule(rule):
            out.append(lines[index])
            index += 1
            continue
        width = max(len(head), len(rule))
        head = _fit(head, width)
        body: list[list[str]] = []
        after = index + 2
        while after < len(lines) and (cells := _row_cells(lines[after])):
            body.append(_fit(cells, width))
            after += 1
        if body and not any(head):
            head = body.pop(0)
        rows = "<tr>" + "".join(f"<th>{escape(cell)}</th>" for cell in head) + "</tr>"
        rows += "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in body)
        ending = lines[index][len(lines[index].rstrip("\r\n")):]
        out.append(f"<table>{rows}</table>{ending}")
        index = after
    return "".join(out)


def _span(element: object, name: str) -> int:
    try:
        return int(element.get(name, "1"))  # type: ignore[attr-defined]
    except ValueError:
        return 1


def _cell_tokens(element: object, parts: list[str], *, outer: bool) -> None:
    """The text of a cell with the tags inside it written out, as the reference metric reads a cell."""
    if not outer:
        parts.append(f"<{element.tag}>")  # type: ignore[attr-defined]
    parts.append(element.text or "")  # type: ignore[attr-defined]
    for child in element:  # type: ignore[attr-defined]
        if isinstance(child.tag, str):
            _cell_tokens(child, parts, outer=False)
    if not outer:
        parts.append(f"</{element.tag}>")  # type: ignore[attr-defined]
        if element.tag != "td":  # type: ignore[attr-defined]
            parts.append(element.tail or "")  # type: ignore[attr-defined]


def _table_tree(element: object, *, with_text: bool) -> Node:
    tag = "td" if element.tag == "th" else element.tag  # type: ignore[attr-defined]
    if tag == "td":
        parts: list[str] = []
        if with_text:
            _cell_tokens(element, parts, outer=True)
        return Node("td", _squeeze(_BREAK.sub("\n", unescape("".join(parts)))), (_span(element, "colspan"), _span(element, "rowspan")))
    return Node(tag, children=[_table_tree(child, with_text=with_text) for child in element if isinstance(child.tag, str)])  # type: ignore[attr-defined]


def _table_forest(text: str) -> object | None:
    """Every table of `text` under one root, so a document's tables are compared together and in order.

    The reference metric gets there by wrapping the tables twice and letting the HTML parser nest them in the outer
    table; the same string is built here, so that the same parser gives the same tree.
    """
    found = [str(table) for table in BeautifulSoup(text, "html.parser").find_all("table")]
    if not found:
        return None
    source = "<html><body><table><html><body>\n" + "\n".join(found) + "\n</body></html></table></body></html>"
    for wrapper in ("<thead>", "</thead>", "<tbody>", "</tbody>"):
        source = source.replace(wrapper, "")
    parsed = lxml_html.fromstring(source, parser=lxml_html.HTMLParser(remove_comments=True, encoding="utf-8"))
    roots = parsed.xpath("body/table")
    return roots[0] if roots else None


def _rename_cell(first: Node, second: Node) -> float:
    if first.tag != second.tag or first.spans != second.spans:
        return 1.0
    return _text_cost(first.text, second.text) if first.tag == "td" else 0.0


def tables(reference: str, result: str) -> tuple[float | None, float | None]:
    """TEDS and TEDS-S of the tables of each text taken together (both with their pipe tables already written as HTML)."""
    expected = _table_forest(reference)
    if expected is None:
        return None, None
    found = _table_forest(result)
    if found is None:
        return 0.0, 0.0
    # The elements under the table, the tags inside its cells among them, as the reference metric counts them.
    elements = max(len(expected.xpath(".//*")), len(found.xpath(".//*")), 1)  # type: ignore[attr-defined]
    if elements > MAX_TABLE_ELEMENTS:
        return None, None
    scores = []
    for with_text in (True, False):
        distance = tree_edit_distance(_table_tree(found, with_text=with_text), _table_tree(expected, with_text=with_text), _rename_cell)
        scores.append(1.0 - distance / elements)
    return scores[0], scores[1]


# Reading order ------------------------------------------------------------------------------------------------------

def _ratio(first: str, second: str) -> float:
    total = len(first) + len(second)
    return 2 * lcs_length(first, second) / total if total else 1.0


def reading_order(reference: str, result: str) -> tuple[float | None, float | None]:
    """NID and NID-S (both texts with their pipe tables already written as HTML)."""
    whole = _squeeze(reference)
    if not whole:
        return None, None
    return (_ratio(whole, _squeeze(result)),
            _ratio(_squeeze(_HTML_TABLE.sub(" ", reference)), _squeeze(_HTML_TABLE.sub(" ", result))))


# Words --------------------------------------------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]*>")
_WORD = re.compile(r"[^\W_]+")
# The marks of emphasis, strikethrough and code, which stand inside a word as often as around one ('**장관**은')
_INLINE_MARKS = re.compile(r"\*+|~~|`")


def _words_of(text: str) -> Counter[str]:
    """The words of a text: its runs of letters and digits, the tags of its tables taken for spaces and its inline marks
    for nothing — '**장관**은' is the word '장관은', as it is in a text written without emphasis."""
    return Counter(_WORD.findall(_INLINE_MARKS.sub("", unescape(_TAG.sub(" ", text)))))


def words(reference: str, result: str) -> float | None:
    """How far the two texts are made of the same words, whatever their order (both with their tables written as HTML)."""
    wanted = _words_of(reference)
    if not wanted:
        return None
    found = _words_of(result)
    return 2 * sum((wanted & found).values()) / (sum(wanted.values()) + sum(found.values()))


# Headings -----------------------------------------------------------------------------------------------------------

def outline(markdown: str) -> Node:
    """The headings of `markdown` in order, every level alike, each holding the text up to the next as one node."""
    root = Node("document")
    holder, pending = root, []

    def flush() -> None:
        text = _squeeze(" ".join(pending))
        if text:
            holder.children.append(Node("content", text))
        pending.clear()

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            holder = Node("heading", _squeeze(match.group(2)))
            root.children.append(holder)
        elif line.strip():
            pending.append(_squeeze(line))
    flush()
    return root


def headings(reference: str, result: str) -> tuple[float | None, float | None]:
    """MHS and MHS-S (both texts with their pipe tables already written as HTML)."""
    expected = outline(reference)
    if not any(child.tag == "heading" for child in expected.children):
        return None, None
    found = outline(result)
    if not any(child.tag == "heading" for child in found.children):
        return 0.0, 0.0
    nodes = max(expected.size(), found.size(), 1)
    costs: dict[tuple[int, int], float] = {}

    def with_text(first: Node, second: Node) -> float:
        if first.tag != second.tag:
            return 1.0
        key = (id(first), id(second))
        if key not in costs:
            costs[key] = _text_cost(first.text, second.text)
        return costs[key]

    def structure(first: Node, second: Node) -> float:
        return 0.0 if first.tag == second.tag else 1.0

    return tuple(min(1.0, max(0.0, 1.0 - tree_edit_distance(expected, found, rename) / nodes))  # type: ignore[return-value]
                 for rename in (with_text, structure))
