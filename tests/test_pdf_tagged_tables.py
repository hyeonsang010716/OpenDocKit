"""Tables a tagged PDF states in its structure tree (`_pdf_tagged_tables.py`): read from the tree where the page agrees
with it, and from the page as before where it does not."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_tagged_tables
from tests.test_pdf import _raw_pdf


_PLAIN = ConvertOptions(page_markers=False)
_INTRO = "The offices that answered the survey are listed below with the year each of them was opened."
_AFTER = "The list was closed at the end of April and has not been changed since then."

# A structure element: (type, kids) or (type, kids, attributes); a kid is another element or the number of a piece of text
Element = tuple


def _tagged_pdf(pieces: list[tuple[float, float, str]], tree: list[Element] | None, *, pages: int = 1,
                page_of: dict[int, int] | None = None, drawing: bytes = b"", loop: bool = False, roles: bytes = b"") -> bytes:
    """A PDF whose text is `pieces` (x, y from the top, text), each drawn as marked content with its index as the id, and
    whose structure tree is `tree` under one Document element — no tree at all for None. `page_of` puts a piece on another
    page than the first; `drawing` is added to the content of the first page; `loop` makes the Document a kid of itself; `roles` are the
    entries of a role map."""
    page_of = page_of or {}
    objects: dict[int, bytes] = {}
    page_ids = [3 + 2 * number for number in range(pages)]
    font = 3 + 2 * pages
    for number, page_id in enumerate(page_ids):
        content = drawing if number == 0 else b""
        for index, (x, y, text) in enumerate(pieces):
            if page_of.get(index, 0) == number:
                escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
                content += b"/Span <</MCID %d>> BDC BT /F1 10 Tf %.1f %.1f Td (%s) Tj ET EMC\n" % (index, x, 842 - y, escaped)
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /StructParents %d "
                            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (page_id + 1, number, font))
        objects[page_id + 1] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
    objects[2] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (pages, b" ".join(b"%d 0 R" % page_id for page_id in page_ids))
    objects[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    if tree is None:
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        return _raw_pdf(objects)
    root, document = font + 1, font + 2
    numbers = iter(range(document + 1, document + 100_000))

    def written(element: Element) -> int:
        kind, kids, attributes = (*element, {})[:3]
        number = next(numbers)
        refs = []
        for kid in kids:
            if isinstance(kid, int):
                refs.append(b"<< /Type /MCR /Pg %d 0 R /MCID %d >>" % (page_ids[page_of.get(kid, 0)], kid))
            else:
                refs.append(b"%d 0 R" % written(kid))
        extra = b" /A << /O /Table %s >>" % b" ".join(b"/%s %d" % (key.encode(), value) for key, value in attributes.items()) \
            if attributes else b""
        objects[number] = b"<< /Type /StructElem /S /%s /K [%s]%s >>" % (kind.encode(), b" ".join(refs), extra)
        return number

    kids = [b"%d 0 R" % written(element) for element in tree] + ([b"%d 0 R" % document] if loop else [])
    objects[document] = b"<< /Type /StructElem /S /Document /P %d 0 R /K [%s] >>" % (root, b" ".join(kids))
    objects[root] = b"<< /Type /StructTreeRoot /K [%d 0 R] /RoleMap << %s >> >>" % (document, roles)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot %d 0 R /MarkInfo << /Marked true >> >>" % root
    return _raw_pdf(objects)


def _table(rows: list[list[list[int] | tuple]]) -> Element:
    """A Table element of rows of cells; a cell is the pieces of its one paragraph, or (pieces, attributes)."""
    cells = []
    for row in rows:
        cells.append(("TR", [("TD", [("P", cell[0])], cell[1]) if isinstance(cell, tuple) else ("TD", [("P", cell)]) for cell in row]))
    return ("Table", cells)


# Two columns without a rule, the second cell of one row on two lines: no search of the page takes two columns
# (`_pdf_gutter_tables._MIN_COLUMNS`), so only the tree makes a table of it
_PIECES = [(72, 100, _INTRO),
           (72, 140, "Office"), (300, 140, "Opened"),
           (72, 154, "Phnom Penh"), (300, 154, "2009, after two years in a "), (300, 166, "rented room"),
           (72, 180, "Battambang"), (300, 180, "2012"),
           (72, 194, "Siem Reap"), (300, 194, "2015"),
           (72, 240, _AFTER)]
_ROWS = [[[1], [2]], [[3], [4, 5]], [[6], [7]], [[8], [9]]]
_TREE = [("P", [0]), _table(_ROWS), ("P", [10])]
_READ = ("| Office | Opened |\n| --- | --- |\n| Phnom Penh | 2009, after two years in a rented room |\n"
         "| Battambang | 2012 |\n| Siem Reap | 2015 |")


def _text(data: bytes) -> str:
    return convert(data, "a.pdf", _PLAIN).text


class TestTablesFromTheTree:
    def test_a_table_no_search_takes_is_read_from_its_tags(self):
        assert "|" not in _text(_tagged_pdf(_PIECES, None))

        text = _text(_tagged_pdf(_PIECES, _TREE))

        assert text == f"{_INTRO}\n\n{_READ}\n\n{_AFTER}"

    def test_a_table_laid_out_from_right_to_left_is_read_in_the_order_of_its_tree(self):
        """The first cell of a row of an Arabic or Hebrew table stands on the right. Its cells did not 'stand side by
        side in their order', so the tags were not believed and the table was left to the page."""
        rows = [[[2], [1]], [[4, 5], [3]], [[7], [6]], [[9], [8]]]  # each row from its right cell to its left one

        text = _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])]))

        assert text.splitlines()[2:5] == ["| Opened | Office |", "| --- | --- |", "| 2009, after two years in a rented room | Phnom Penh |"]

    def test_rows_that_run_both_ways_are_not_believed(self):
        rows = [[[1], [2]], [[4, 5], [3]], [[6], [7]], [[9], [8]]]

        assert "|" not in _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])]))

    def test_a_cell_that_spans_rows_or_columns(self):
        pieces = [(72, 100, _INTRO), (72, 140, "Region"), (200, 140, "Office"), (330, 140, "Opened"),
                  (72, 154, "North"), (200, 154, "Battambang"), (330, 154, "2012"),
                  (200, 168, "Siem Reap"), (330, 168, "2015"),
                  (72, 182, "Closed offices are not listed"), (72, 230, _AFTER)]
        rows = [[[1], [2], [3]], [([4], {"RowSpan": 2}), [5], [6]], [[7], [8]], [([9], {"ColSpan": 3})]]

        text = _text(_tagged_pdf(pieces, [("P", [0]), _table(rows), ("P", [10])]))

        # a value that spans rows is written on each of them, as in every format; the row that spans all columns keeps
        # the table HTML (`docs/decisions/core.md`)
        assert "North" in text and text.count("North") == 2
        assert "Battambang" in text and "Siem Reap" in text and "Closed offices are not listed" in text
        assert text.startswith(_INTRO) and text.endswith(_AFTER)

    def test_rows_on_two_pages_are_a_table_on_each(self):
        pieces = [*_PIECES[:8], (72, 100, "Siem Reap"), (300, 100, "2015"), (72, 114, "Kampot"), (300, 114, "2019"),
                  (72, 160, _AFTER)]
        rows = [[[1], [2]], [[3], [4, 5]], [[6], [7]], [[8], [9]], [[10], [11]]]
        data = _tagged_pdf(pieces, [("P", [0]), _table(rows), ("P", [12])], pages=2, page_of={8: 1, 9: 1, 10: 1, 11: 1, 12: 1})

        text = _text(data)

        assert "| Battambang | 2012 |" in text and "| Siem Reap | 2015 |" in text and "| Kampot | 2019 |" in text

    def test_a_word_broken_by_a_narrow_cell_is_one_word_and_a_line_the_writer_ended_is_not(self):
        """The cells end a line broken at a space with that space ('two years in a '); 'Differen' runs to the end of its
        column without one, and 'Name' stops short of it."""
        pieces = [(72, 100, _INTRO), (72, 140, "Differen"), (72, 152, "ce in the "), (72, 164, "number"), (300, 140, "Name"),
                  (300, 152, "Title of the office"),
                  (72, 180, "Minus 4"), (300, 180, "Head, since two years in a "), (300, 192, "rented room"),
                  (72, 240, _AFTER)]
        rows = [[[1, 2, 3], [4, 5]], [[6], [7, 8]]]

        text = _text(_tagged_pdf(pieces, [("P", [0]), _table(rows), ("P", [9])]))

        assert "| Difference in the number | Name Title of the office |" in text


class TestThePageMustAgree:
    """A tag is what the writing program says. Where the page does not show what it says, the page is read as before."""

    def _untagged(self, pieces: list[tuple[float, float, str]] = _PIECES) -> str:
        return _text(_tagged_pdf(pieces, None))

    def test_cells_of_a_row_that_do_not_stand_side_by_side(self):
        rows = [[[1], [2]], [[3], [6]], [[4, 5], [7]], [[8], [9]]]  # 'Phnom Penh' beside 'Battambang', which stands under it

        assert _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])])) == self._untagged()

    def test_a_line_of_the_page_inside_the_table_that_no_cell_holds(self):
        rows = [[[1], [2]], [[3], [4, 5]], [[8], [9]]]  # the row of Battambang is not in the tree

        assert _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])])) == self._untagged()

    def test_a_table_of_one_row_or_one_column(self):
        assert _text(_tagged_pdf(_PIECES, [_table([[[1], [2]]])])) == self._untagged()
        assert _text(_tagged_pdf(_PIECES, [_table([[[1]], [[3]], [[6]]])])) == self._untagged()

    def test_a_table_in_a_cell(self):
        inner = _table([[[4], [5]]])
        tree = [("Table", [("TR", [("TD", [("P", [1])]), ("TD", [("P", [2])])]), ("TR", [("TD", [("P", [3])]), ("TD", [inner])])])]

        assert _text(_tagged_pdf(_PIECES, tree)) == self._untagged()

    def test_a_table_found_by_its_rules_is_read_from_its_rules(self):
        """The same grid with every rule drawn: the tags say one row less, and the rules are believed."""
        lines = b"".join(b"60 %d m 500 %d l S\n" % (y, y) for y in (842 - 128, 842 - 144, 842 - 170, 842 - 184, 842 - 198))
        lines += b"".join(b"%d %d m %d %d l S\n" % (x, 842 - 128, x, 842 - 198) for x in (60, 290, 500))
        rows = [[[1], [2]], [[3], [4, 5]], [[6], [7]]]

        tagged = _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])], drawing=lines))

        assert tagged == _text(_tagged_pdf(_PIECES, None, drawing=lines)) and "| Siem Reap | 2015 |" in tagged


class TestTheTree:
    def test_a_tree_that_holds_itself_ends(self):
        assert _text(_tagged_pdf(_PIECES, _TREE, loop=True)) == f"{_INTRO}\n\n{_READ}\n\n{_AFTER}"

    def test_a_tree_past_the_bounds_is_no_tree(self, monkeypatch):
        monkeypatch.setattr(_pdf_tagged_tables, "_MAX_ELEMENTS", 5)

        assert "|" not in _text(_tagged_pdf(_PIECES, _TREE))

    @pytest.mark.parametrize("span", [201, 10**9])
    def test_a_span_no_table_has_leaves_the_table_to_the_page(self, span):
        rows = [[[1], [2]], [([3], {"RowSpan": span}), [4, 5]], [[6], [7]], [[8], [9]]]

        assert "|" not in _text(_tagged_pdf(_PIECES, [("P", [0]), _table(rows), ("P", [10])]))

    def test_a_role_map_names_the_standard_types(self):
        """A program's own names for its elements ('Grid', 'Line', 'Box') are read as what the map says they are, and a
        name the map leads round in a circle is no type."""
        tree = [("P", [0]), ("Grid", [("Line", [("Box", [("P", cell)]) for cell in row]) for row in _ROWS]), ("P", [10])]
        roles = b"/Grid /Table /Line /Row /Row /TR /Box /TD /Loop /Pool /Pool /Loop"

        assert _READ in _text(_tagged_pdf(_PIECES, tree, roles=roles))
        assert "|" not in _text(_tagged_pdf(_PIECES, [("Loop", tree[1][1])], roles=roles))


class TestParagraphsOfACell:
    def test_the_items_of_a_list_and_the_paragraphs_of_a_cell_are_its_lines(self):
        """An item is its label and its text on one line; the line of a paragraph the cell broke is joined to it."""
        pieces = [(72, 100, _INTRO), (72, 140, "Who"), (200, 140, "Must file"),
                  (72, 154, "Single"), (200, 154, "File when any of these apply to the year "), (200, 166, "you file for."),
                  (200, 178, "1."), (215, 178, "Income over the limit"), (200, 190, "2."), (215, 190, "Tax was withheld"),
                  (72, 204, "Married"), (200, 204, "Always"), (72, 250, _AFTER)]
        items = ("L", [("LI", [("Lbl", [6]), ("LBody", [("P", [7])])]), ("LI", [("Lbl", [8]), ("LBody", [("P", [9])])])])
        table = ("Table", [("TR", [("TH", [("P", [1])]), ("TH", [("P", [2])])]),
                           ("TR", [("TD", [("P", [3])]), ("TD", [("P", [4, 5]), items])]),
                           ("TR", [("TD", [("P", [10])]), ("TD", [("P", [11])])])])

        text = _text(_tagged_pdf(pieces, [("P", [0]), table, ("P", [12])]))

        assert ("| Single | File when any of these apply to the year you file for.<br>1. Income over the limit<br>"
                "2. Tax was withheld |") in text
        assert "| Married | Always |" in text
