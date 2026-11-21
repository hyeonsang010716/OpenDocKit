"""The structure metrics of `evals/metrics.py` and the baseline comparison of `evals/run.py`."""

import random

import pytest

from evals.metrics import Node, edit_distance, lcs_length, outline, score, tables_as_html, tree_edit_distance
from evals.run import compare


def _slow_lcs(first: str, second: str) -> int:
    row = [0] * (len(second) + 1)
    for a in first:
        previous = 0
        for index, b in enumerate(second, 1):
            previous, row[index] = row[index], previous + 1 if a == b else max(row[index], row[index - 1])
    return row[-1]


def _slow_edit_distance(first: str, second: str) -> int:
    row = list(range(len(second) + 1))
    for i, a in enumerate(first, 1):
        previous, row[0] = row[0], i
        for j, b in enumerate(second, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (a != b))
    return row[-1]


class TestSequences:
    def test_bit_parallel_lengths_equal_the_table_method(self):
        rng = random.Random(7)
        for _ in range(300):
            first = "".join(rng.choice("abc가나 ") for _ in range(rng.randrange(0, 90)))
            second = "".join(rng.choice("abc가나 ") for _ in range(rng.randrange(0, 90)))
            assert lcs_length(first, second) == _slow_lcs(first, second)
            assert edit_distance(first, second) == _slow_edit_distance(first, second)

    def test_known_distances(self):
        assert edit_distance("kitten", "sitting") == 3
        assert lcs_length("AGGTAB", "GXTXAYB") == 4
        assert edit_distance("", "abc") == 3 and lcs_length("", "abc") == 0


class TestTreeEditDistance:
    @staticmethod
    def _tree(spec: tuple) -> Node:
        return Node(spec[0], children=[TestTreeEditDistance._tree(child) for child in spec[1:]])

    @staticmethod
    def _by_label(first: Node, second: Node) -> float:
        return 0.0 if first.tag == second.tag else 1.0

    def test_the_example_of_zhang_and_shasha(self):
        first = self._tree(("f", ("d", ("a",), ("c", ("b",))), ("e",)))
        second = self._tree(("f", ("c", ("d", ("a",), ("b",))), ("e",)))
        assert tree_edit_distance(first, second, self._by_label) == 2

    def test_a_missing_subtree_costs_its_nodes(self):
        whole = self._tree(("table", ("tr", ("td",), ("td",)), ("tr", ("td",), ("td",))))
        part = self._tree(("table", ("tr", ("td",), ("td",))))
        assert tree_edit_distance(whole, part, self._by_label) == 3
        assert tree_edit_distance(whole, whole, self._by_label) == 0


class TestTablesAsHtml:
    def test_a_pipe_table_becomes_one_html_line(self):
        text = "Before\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nAfter\n"
        assert tables_as_html(text) == "Before\n\n<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>\n\nAfter\n"

    def test_an_empty_header_row_gives_way_to_the_first_row(self):
        assert tables_as_html("|  |  |\n|---|---|\n| A | B |\n| 1 | 2 |") == "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>\n"

    def test_a_line_with_a_bar_and_no_rule_under_it_stays(self):
        assert tables_as_html("a | b\nc | d\n") == "a | b\nc | d\n"


class TestScore:
    REFERENCE = "# Report\n\nFirst paragraph.\n\n## Results\n\n| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 2 |\n\nLast paragraph.\n"

    def test_the_same_text_scores_one(self):
        scores = score(self.REFERENCE, self.REFERENCE)
        assert scores.as_dict() == {"overall": 1.0, "nid": 1.0, "nid_s": 1.0, "teds": 1.0, "teds_s": 1.0, "mhs": 1.0, "mhs_s": 1.0}

    def test_an_html_table_scores_as_the_pipe_table_it_equals(self):
        as_html = self.REFERENCE.replace(
            "| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 2 |",
            "<table><tr><th>Name</th><th>Value</th></tr><tr><td>alpha</td><td>1</td></tr><tr><td>beta</td><td>2</td></tr></table>")
        assert score(self.REFERENCE, as_html).teds == 1.0

    def test_headings_written_as_paragraphs_cost_the_heading_score_only(self):
        flat = self.REFERENCE.replace("# Report", "Report").replace("## Results", "Results")
        scores = score(self.REFERENCE, flat)
        assert scores.mhs == 0.0 and scores.teds == 1.0
        assert scores.nid is not None and scores.nid > 0.95

    def test_a_missing_heading_and_an_extra_one_both_cost(self):
        missing = score(self.REFERENCE, self.REFERENCE.replace("## Results", "Results"))
        extra = score(self.REFERENCE, self.REFERENCE.replace("Last paragraph.", "## Last paragraph."))
        assert missing.mhs is not None and extra.mhs is not None
        assert 0 < missing.mhs < 1 and 0 < extra.mhs < 1

    def test_a_table_written_as_lines_scores_no_table(self):
        lines = self.REFERENCE.replace("| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 2 |", "Name Value\n\nalpha 1\n\nbeta 2")
        scores = score(self.REFERENCE, lines)
        assert (scores.teds, scores.teds_s) == (0.0, 0.0)

    def test_a_wrong_cell_costs_text_and_not_structure(self):
        scores = score(self.REFERENCE, self.REFERENCE.replace("| beta | 2 |", "| beta | 9 |"))
        assert scores.teds_s == 1.0
        # nine elements under the root that holds the tables: the table, three rows, six cells — one cell wholly wrong
        assert scores.teds == pytest.approx(1 - 1 / 10)

    def test_two_tables_are_compared_together(self):
        second = "\n| X |\n|---|\n| y |\n"
        scores = score(self.REFERENCE + second, self.REFERENCE)
        assert scores.teds is not None and 0.5 < scores.teds < 1.0

    def test_paragraphs_in_another_order_cost_reading_order(self):
        swapped = "# Report\n\nLast paragraph.\n\n## Results\n\n| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 2 |\n\nFirst paragraph.\n"
        scores = score(self.REFERENCE, swapped)
        assert scores.nid is not None and scores.nid < 1.0 and scores.teds == 1.0

    def test_what_the_reference_lacks_is_not_measured(self):
        scores = score("Only a paragraph.\n", "# Title\n\nOnly a paragraph.\n")
        assert scores.teds is None and scores.mhs is None
        assert scores.overall == scores.nid
        assert score("", "text").as_dict()["overall"] is None

    def test_the_outline_holds_the_text_under_each_heading_as_one_node(self):
        root = outline("intro\n\n# A\n\none\n\ntwo\n\n### B\n")
        assert [(child.tag, child.text) for child in root.children] == [("content", "intro"), ("heading", "A"), ("heading", "B")]
        assert [(child.tag, child.text) for child in root.children[1].children] == [("content", "one two")]


class TestCompare:
    @staticmethod
    def _card(mhs: float, document: float) -> dict:
        means = {metric: {"mean": 0.9, "documents": 2} for metric in ("overall", "nid", "nid_s", "teds", "teds_s", "mhs_s")}
        means["mhs"] = {"mean": mhs, "documents": 2}
        scores = {"overall": 0.9, "nid": 0.9, "nid_s": 0.9, "teds": None, "teds_s": None, "mhs": document, "mhs_s": 0.9}
        return {"means": means, "documents": {"a": scores, "b": dict(scores, mhs=0.5)}}

    def test_a_mean_that_fell_fails_and_names_the_document(self):
        lines, fell = compare(self._card(0.6, 0.7), self._card(0.7, 0.9), tolerance=0.002)
        assert fell
        assert any("FELL" in line and line.startswith("mhs ") for line in lines)
        assert any(line.strip().startswith("a ") and "mhs -0.200" in line for line in lines)

    def test_a_mean_within_the_tolerance_or_higher_passes(self):
        assert not compare(self._card(0.699, 0.9), self._card(0.7, 0.9), tolerance=0.002)[1]
        lines, fell = compare(self._card(0.8, 0.95), self._card(0.7, 0.9), tolerance=0.002)
        assert not fell and any("mhs +0.050" in line for line in lines)
