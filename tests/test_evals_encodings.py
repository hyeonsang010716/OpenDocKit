"""Text without a stated charset, written in each code page of its language and read back (`evals/encodings.py`)."""

import json

import pytest

from evals import encodings
from evals.encodings import MIXED, PLAIN, compare, encoded, main, run, samples
from evals.fetch_encoding_texts import lines


_ITALIAN = ["Il bollettino di martedì e mercoledì annuncia nuvolosità in aumento su tutta la regione.",
            "Giovedì e venerdì il tempo sarà più stabile, però la visibilità resterà ridotta nelle valli.",
            "La società ha già comunicato che l'attività riprenderà lunedì, così come previsto dal calendario.",
            "Chi è interessato può scrivere all'ufficio, che risponderà entro la metà della settimana prossima."]


def _texts(tmp_path, **languages: list[str]):
    folder = tmp_path / "texts"
    folder.mkdir(exist_ok=True)
    for language, text in languages.items():
        (folder / f"{language}.txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    return folder


class TestSamples:
    def test_text_is_cut_five_ways_and_written_with_names_behind_it(self):
        cuts = list(samples(_ITALIAN))

        plain = [text for kind, text in cuts if kind == PLAIN]
        long = _ITALIAN[1]
        start = "Giovedì e venerdì il tempo sarà più stabile, però la visibilità resterà ridotta nelle"
        assert long in plain and start in plain and long.startswith(start + " ") and len(start) <= 90
        assert "\n\n".join(_ITALIAN) in plain
        assert any(text.startswith("Item 1;") and "martedì" in text for text in plain)
        assert (MIXED, f"{start} Žižek ok.") in cuts
        assert sum(kind == MIXED for kind, _ in cuts) == len(_ITALIAN) * len(encodings.NAMES_AND_SIGNS)

    def test_text_a_code_page_cannot_write_is_none(self):
        assert encoded("Žižek", "iso8859_1") is None
        assert encoded("martedì", "cp1252") == b"marted\xec"

    def test_vietnamese_tone_marks_are_written_behind_their_vowel(self):
        assert encoded("họp", "cp1258") == b"ho\xf2p"

    def test_lines_of_a_text_are_its_paragraphs_and_list_items(self):
        xml = ('<udhr xmlns="http://efele.net/udhr"><title>Dichiarazione</title><preamble><para>Considerato che il '
               'riconoscimento della dignità è il fondamento.</para></preamble><article><title>Articolo 1</title><orderedlist>'
               "<listitem>Ogni individuo ha diritto alla libertà di movimento.</listitem></orderedlist></article></udhr>")

        assert lines(xml.encode()) == ["Considerato che il riconoscimento della dignità è il fondamento.",
                                       "Ogni individuo ha diritto alla libertà di movimento."]


class TestRun:
    def test_card_says_of_each_sample_whether_it_was_read_right(self, tmp_path):
        card, wrong = run(_texts(tmp_path, ita=_ITALIAN))

        cell = card["cells"]["cp1252 ita"]
        assert set(cell[PLAIN]) <= {"0", "1"} and len(cell[PLAIN]) == 2 * sum(kind == PLAIN for kind, _ in samples(_ITALIAN))
        assert card["totals"][PLAIN] == [cell[PLAIN].count("1"), len(cell[PLAIN])]
        assert all(len(sample) <= 120 for places in wrong.get("cp1252 ita", {}).values() for sample in places.values())
        assert set(card["versions"]) == {"python", "charset-normalizer"}

    def test_sample_read_wrong_is_kept_by_its_place_among_the_marks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(encodings, "decode_text", lambda data, markup=False: (data.decode("cp1250"), "cp1250"))

        card, wrong = run(_texts(tmp_path, ita=_ITALIAN))

        cell = card["cells"]["cp1252 ita"]
        for kind in (PLAIN, MIXED):
            assert sorted(wrong["cp1252 ita"][kind]) == [place for place, mark in enumerate(cell[kind]) if mark == "0"]
        assert "marted\u011b" in wrong["cp1252 ita"][PLAIN][0]

    def test_sample_is_read_as_text_and_as_a_page_of_html(self, tmp_path, monkeypatch):
        """Each sample gives two marks: the reader is asked with the bytes of the text, then with those of a page."""
        asked = []
        monkeypatch.setattr(encodings, "decode_text",
                            lambda data, markup=False: asked.append((data[:6], markup)) or (data.decode("cp1252"), "cp1252"))

        run(_texts(tmp_path, ita=_ITALIAN))

        assert asked[0] == (b"Il bol", False) and asked[1] == (b"<html>", True)
        assert [markup for _, markup in asked] == [False, True] * (len(asked) // 2)

    def test_text_no_sample_of_which_a_code_page_writes_has_no_cell(self, tmp_path):
        card, _ = run(_texts(tmp_path, tur=["Az\u0259rbaycan dilind\u0259 bir c\u00fcml\u0259."], ita=_ITALIAN))

        assert list(card["cells"]) == ["cp1252 ita"]

    def test_card_holds_no_text(self, tmp_path):
        card, _ = run(_texts(tmp_path, ita=_ITALIAN))

        assert "bollettino" not in json.dumps(card, ensure_ascii=False)

    def test_language_without_a_text_is_left_out(self, tmp_path):
        card, _ = run(_texts(tmp_path, ita=_ITALIAN))

        assert list(card["cells"]) == ["cp1252 ita"]


class TestCompare:
    @staticmethod
    def _card(plain: str, mixed: str = "11") -> dict:
        return {"cells": {"cp1252 ita": {PLAIN: plain, MIXED: mixed}}, "versions": {"python": "3.12", "charset-normalizer": "3.5.2"},
                "totals": {PLAIN: [plain.count("1"), len(plain)], MIXED: [mixed.count("1"), len(mixed)]}}

    def test_sample_that_was_right_and_is_wrong_is_worse_whatever_the_count(self):
        """The rule that read French with a Slavic name as Czech read more French right than it lost."""
        lines_, worse = compare(self._card("0111"), self._card("1100"))

        assert worse == {"cp1252 ita": {PLAIN: [0]}}
        assert any("2 -> 3 of 4, 1 lost" in line for line in lines_)

    def test_sample_with_a_name_that_was_right_and_is_wrong_is_worse(self):
        """The kind of sample the card was made for."""
        _, worse = compare(self._card("1100", "011"), self._card("1100", "110"))

        assert worse == {"cp1252 ita": {MIXED: [0]}}

    def test_cell_of_the_baseline_that_the_run_does_not_have_is_worse(self):
        """A text missing from the folder is not a run that lost nothing."""
        card = self._card("1100")
        lines_, worse = compare({**card, "cells": {}}, card)

        assert worse == {"cp1252 ita": {}} and any("only in the baseline" in line for line in lines_)

    def test_cell_the_baseline_does_not_have_is_said_and_is_not_worse(self):
        card = self._card("1100")
        lines_, worse = compare(card, {**card, "cells": {}})

        assert worse == {} and any(line.endswith("new") for line in lines_)

    def test_samples_gained_are_not_worse(self):
        lines_, worse = compare(self._card("1110"), self._card("1100"))

        assert worse == {} and any("2 -> 3 of 4" in line and "lost" not in line for line in lines_)

    def test_cell_of_other_samples_than_the_baseline_is_worse(self):
        _, worse = compare(self._card("111"), self._card("1100"))

        assert worse == {"cp1252 ita": {}}

    @pytest.mark.parametrize("versions", [{"python": "3.14", "charset-normalizer": "3.5.2"},
                                          {"python": "3.12", "charset-normalizer": "3.6.0"}], ids=["python", "library"])
    def test_baseline_written_with_other_versions_is_said(self, versions):
        lines_, _ = compare({**self._card("1100"), "versions": versions}, self._card("1100"))

        assert "python 3.12" in lines_[0] and f"python {versions['python']}" in lines_[0].split("this run has")[1]
        assert f"charset-normalizer {versions['charset-normalizer']}" in lines_[0].split("this run has")[1]

    def test_same_versions_are_not_spoken_of(self):
        lines_, _ = compare(self._card("1100"), self._card("1100"))

        assert not any("written with" in line for line in lines_)

    def test_cell_that_did_not_move_has_no_line(self):
        lines_, worse = compare(self._card("1100"), self._card("1100"))

        assert worse == {} and not any("cp1252" in line for line in lines_)


class TestMain:
    def test_run_against_its_own_baseline_ends_with_0(self, tmp_path, capsys):
        folder, baseline = _texts(tmp_path, ita=_ITALIAN), tmp_path / "baseline.json"

        assert main(["--texts", str(folder), "--baseline", str(baseline), "--update-baseline"]) == 0
        assert main(["--texts", str(folder), "--baseline", str(baseline)]) == 0
        assert "one language:" in capsys.readouterr().out

    def test_run_that_lost_a_sample_ends_with_1(self, tmp_path, monkeypatch):
        folder, baseline = _texts(tmp_path, ita=_ITALIAN), tmp_path / "baseline.json"
        main(["--texts", str(folder), "--baseline", str(baseline), "--update-baseline"])
        monkeypatch.setattr(encodings, "decode_text", lambda data, markup=False: (data.decode("cp1250"), "cp1250"))

        assert main(["--texts", str(folder), "--baseline", str(baseline), "--show", "1"]) == 1

    def test_samples_shown_are_those_that_were_lost(self, tmp_path, monkeypatch, capsys):
        """Not the first samples read wrong: one that was already wrong in the baseline is no news."""
        folder, baseline = _texts(tmp_path, ita=_ITALIAN), tmp_path / "baseline.json"
        main(["--texts", str(folder), "--baseline", str(baseline), "--update-baseline"])
        card = json.loads(baseline.read_text(encoding="utf-8"))
        cell = card["cells"]["cp1252 ita"]
        cell[PLAIN] = "0" + cell[PLAIN][1:]   # the first sample was read wrong before
        baseline.write_text(json.dumps(card), encoding="utf-8")
        monkeypatch.setattr(encodings, "decode_text", lambda data, markup=False: (data.decode("cp1250"), "cp1250"))
        capsys.readouterr()

        assert main(["--texts", str(folder), "--baseline", str(baseline), "--show", "2"]) == 1
        shown = [line for line in capsys.readouterr().out.splitlines() if line.startswith("--- ")]
        assert len(shown) == 2 and "<html>" in shown[0] and "regione" in shown[1]   # the second and third marks

    def test_nothing_is_shown_unless_asked(self, tmp_path, monkeypatch, capsys):
        folder, baseline = _texts(tmp_path, ita=_ITALIAN), tmp_path / "baseline.json"
        main(["--texts", str(folder), "--baseline", str(baseline), "--update-baseline"])
        monkeypatch.setattr(encodings, "decode_text", lambda data, markup=False: (data.decode("cp1250"), "cp1250"))

        assert main(["--texts", str(folder), "--baseline", str(baseline)]) == 1
        assert "--- " not in capsys.readouterr().out

    def test_run_without_a_baseline_ends_with_0(self, tmp_path, capsys):
        assert main(["--texts", str(_texts(tmp_path, ita=_ITALIAN))]) == 0
        assert "with a name or a sign:" in capsys.readouterr().out

    def test_folder_that_is_not_there_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--texts", str(tmp_path / "nowhere")])

    def test_folder_without_a_text_of_the_scorecard_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--texts", str(_texts(tmp_path))])
