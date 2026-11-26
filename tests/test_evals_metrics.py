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
            # the definitions written here, and the library used in their place when it is installed
            for fast in (False, True):
                assert lcs_length(first, second, fast=fast) == _slow_lcs(first, second)
                assert edit_distance(first, second, fast=fast) == _slow_edit_distance(first, second)

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
        assert scores.as_dict() == {"overall": 1.0, "nid": 1.0, "nid_s": 1.0, "teds": 1.0, "teds_s": 1.0, "mhs": 1.0, "mhs_s": 1.0, "words": 1.0}

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


class TestWords:
    def test_a_word_cut_in_two_costs_words_and_hardly_any_reading_order(self):
        reference = "산림청과 지방정부, 유관기관 등 산지관리 업무 담당자 약 340명이 참석한 가운데"
        scores = score(reference, "산림\n청과 지방정부, 유관기관 등 산지관리 업무 담당자 약 340명이 참석한 가\n운데")
        assert scores.nid > 0.97
        assert scores.words == pytest.approx(2 * 9 / (11 + 13))

    def test_markup_and_order_are_not_words(self):
        assert score("# Title\n\nfirst second\n\n| a | b |\n|---|---|\n| 1 | 2 |", "second first **Title** a b 1 2").words == 1.0

    def test_emphasis_inside_a_word_does_not_cut_it(self):
        """The HWPX of a release marks '장관' bold and the PDF marks nothing: the same words."""
        assert score("**장관**은 `안전`을 ~~최우선~~으로 *강조*했다", "장관은 안전을 최우선으로 강조했다").words == 1.0

    def test_a_reference_without_words_is_not_measured(self):
        assert score("---", "text").words is None


class TestLimitsOfTheMeasure:
    def test_tables_too_large_to_compare_are_not_measured(self, monkeypatch):
        """The tree edit distance compares every pair of elements: past the bound the table score is left out, the rest stays."""
        from evals import metrics

        table = "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 6
        assert score(table, table).teds == 1.0
        monkeypatch.setattr(metrics, "MAX_TABLE_ELEMENTS", 10)
        scores = score(table, table)
        assert scores.teds is None and scores.teds_s is None and scores.nid == 1.0


class TestCompare:
    @staticmethod
    def _card(mhs: float, document: float) -> dict:
        means = {metric: {"mean": 0.9, "documents": 2} for metric in ("overall", "nid", "nid_s", "teds", "teds_s", "mhs_s", "words")}
        means["mhs"] = {"mean": mhs, "documents": 2}
        scores = {"overall": 0.9, "nid": 0.9, "nid_s": 0.9, "teds": None, "teds_s": None, "mhs": document, "mhs_s": 0.9, "words": 0.9}
        return {"means": means, "documents": {"a": scores, "b": dict(scores, mhs=0.5)}}

    def test_a_mean_that_fell_fails_and_names_the_document(self):
        lines, fell = compare(self._card(0.6, 0.7), self._card(0.7, 0.9), tolerance=0.002)
        assert fell
        assert any("FELL" in line and line.startswith("mhs ") for line in lines)
        assert any(line.strip().startswith("a ") and "mhs -0.200" in line for line in lines)

    def test_a_metric_left_out_of_the_gate_is_reported_and_does_not_fail(self):
        lines, fell = compare(self._card(0.6, 0.7), self._card(0.7, 0.9), tolerance=0.002, gate=("nid", "teds"))
        assert not fell and any("fell (not gated)" in line for line in lines)

    def test_a_mean_within_the_tolerance_or_higher_passes(self):
        assert not compare(self._card(0.699, 0.9), self._card(0.7, 0.9), tolerance=0.002)[1]
        lines, fell = compare(self._card(0.8, 0.95), self._card(0.7, 0.9), tolerance=0.002)
        assert not fell and any("mhs +0.050" in line for line in lines)


    def test_a_baseline_written_before_a_metric_was_added_is_compared_by_the_others(self):
        old = self._card(0.7, 0.9)
        del old["means"]["words"]
        for scores in old["documents"].values():
            del scores["words"]
        lines, fell = compare(self._card(0.7, 0.9), old, tolerance=0.002)
        assert not fell and not any(line.startswith("words") for line in lines)


    def test_a_text_that_changed_without_a_score_moving_is_named(self):
        old, new = self._card(0.7, 0.9), self._card(0.7, 0.9)
        old["texts"], new["texts"] = {"a": "1" * 12, "b": "2" * 12}, {"a": "1" * 12, "b": "3" * 12}

        lines, fell = compare(new, old, tolerance=0.002)

        assert not fell
        assert "documents whose text changed: 1 of 2 (1 without a score that moved)" in lines and "  b" in lines
        # a baseline written before the texts were kept says nothing of them
        del old["texts"]
        assert not any("text changed" in line for line in compare(new, old, tolerance=0.002)[0])

    @staticmethod
    def _scored(**documents: float) -> dict:
        """A scorecard whose means are those of its documents, each scored the same on every metric."""
        from evals.run import METRICS, _means

        scored = {name: dict.fromkeys(METRICS, value) for name, value in documents.items()}
        return {"means": _means(scored), "documents": scored}

    def test_a_document_of_the_baseline_not_in_the_run_is_left_out_of_both_means(self):
        """korea.kr stopped serving two releases of the Korean pairs: fetched again, the corpus had 58 of 60, and the mean
        of the 58 rose against that of the 60 with the reader unchanged. A fall of the same size would have been hidden."""
        kept = {f"d{index}": 0.8 for index in range(10)}
        old = self._scored(**kept, gone=0.2)
        lines, fell = compare(self._scored(**kept), old, tolerance=0.002)
        assert not fell
        assert "documents of the baseline not in this run: 1 (gone)" in lines
        assert "the means below are those of the 10 documents in both" in lines
        assert any(line.startswith("nid ") and line.endswith("+0.0000") for line in lines)

        # the documents that are left fell, and the one that is gone would have hidden it
        lines, fell = compare(self._scored(**dict(kept, d0=0.75)), old, tolerance=0.002)
        assert fell and any(line.startswith("nid ") and "FELL" in line for line in lines)

    def test_a_document_of_the_run_not_in_the_baseline_is_left_out_of_both_means(self):
        lines, fell = compare(self._scored(a=0.9, b=0.8, new=0.1), self._scored(a=0.9, b=0.8), tolerance=0.002)
        assert not fell
        assert "documents of this run not in the baseline: 1 (new)" in lines
        assert any(line.startswith("nid ") and line.endswith("+0.0000") for line in lines)

    def test_a_run_that_lacks_much_of_the_baseline_fails(self):
        """Compared on what is left, a run of other documents, or of a fetch that broke, would pass."""
        old = self._scored(**{f"d{index}": 0.9 for index in range(20)})
        assert not compare(self._scored(**{f"d{index}": 0.9 for index in range(18)}), old, tolerance=0.002)[1]  # 2 of 20

        for card in (self._scored(**{f"d{index}": 0.9 for index in range(17)}), self._scored(x=0.9, y=0.9)):
            lines, fell = compare(card, old, tolerance=0.002)
            assert fell and any(line.startswith("too few documents of the baseline in this run") for line in lines)

    def test_a_baseline_written_again_names_the_documents_it_drops(self, tmp_path, capsys):
        from evals.run import main

        gold, documents = tmp_path / "gold", tmp_path / "documents"
        gold.mkdir(), documents.mkdir()
        for name in ("a", "b"):
            (gold / f"{name}.md").write_text("# Title\n\nSome text.\n", encoding="utf-8")
            (documents / f"{name}.md").write_text("# Title\n\nSome text.\n", encoding="utf-8")
        baseline = tmp_path / "baseline.json"
        assert main(["--gold", str(gold), "--predictions", str(documents), "--baseline", str(baseline), "--update-baseline"]) == 0
        assert "not in the new one" not in capsys.readouterr().out
        (documents / "b.md").unlink()
        (gold / "b.md").unlink()

        assert main(["--gold", str(gold), "--predictions", str(documents), "--baseline", str(baseline), "--update-baseline"]) == 0
        assert "documents of the old baseline not in the new one: 1 (b)" in capsys.readouterr().out

    def test_the_same_documents_are_compared_by_the_means_written(self):
        """With the same documents on both sides the baseline's means are the ones compared, as they were written."""
        old = self._scored(a=0.9, b=0.8)
        old["means"]["nid"]["mean"] = 0.95
        lines, fell = compare(self._scored(a=0.9, b=0.8), old, tolerance=0.002)
        assert fell and not any("not in" in line for line in lines)


class TestRun:
    def test_documents_are_scored_against_markdown_and_against_documents(self, tmp_path):
        from evals.run import run

        documents, gold, twins = tmp_path / "documents", tmp_path / "gold", tmp_path / "twins"
        for folder in (documents, gold, twins):
            folder.mkdir()
        (documents / "a.md").write_text("# Title\n\nBody text of the document.\n", encoding="utf-8")
        (documents / "a.txt").write_text("Title\n\nBody text of the document.\n", encoding="utf-8")
        (gold / "a.md").write_text("# Title\n\nBody text of the document.\n", encoding="utf-8")
        (twins / "a.html").write_text("<h1>Title</h1><p>Body text of the document.</p>", encoding="utf-8")

        card = run(gold, documents, None, None)
        against_markdown = card["documents"]["a"]
        assert len(card["texts"]["a"]) == 12 and card["texts"] == run(gold, documents, None, None)["texts"]
        against_a_document = run(twins, documents, None, tmp_path / "out")["documents"]["a"]

        # the .txt is the document converted; the .md beside it is never taken for one
        assert against_markdown["mhs"] == 0.0 and against_markdown["nid"] > 0.9
        assert against_a_document == against_markdown
        assert (tmp_path / "out" / "a.md").read_text(encoding="utf-8").startswith("Title")

    def test_plain_converts_without_emphasis_and_links(self, tmp_path):
        """For a reference written without them (`make_twins.py`)."""
        from evals.run import run

        documents, gold = tmp_path / "documents", tmp_path / "gold"
        documents.mkdir()
        gold.mkdir()
        (documents / "a.html").write_text('<p>Read <b>this</b> at <a href="https://example.com/x">the site</a>.</p>', encoding="utf-8")
        (gold / "a.md").write_text("Read this at the site.\n", encoding="utf-8")

        assert run(gold, documents, None, None, plain=True)["documents"]["a"]["nid"] == 1.0
        assert run(gold, documents, None, None)["documents"]["a"]["nid"] < 1.0
