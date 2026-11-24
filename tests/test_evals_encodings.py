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
        assert all(len(sample) <= 120 for sample in wrong.get("cp1252 ita", []))

    def test_card_holds_no_text(self, tmp_path):
        card, _ = run(_texts(tmp_path, ita=_ITALIAN))

        assert "bollettino" not in json.dumps(card, ensure_ascii=False)

    def test_language_without_a_text_is_left_out(self, tmp_path):
        card, _ = run(_texts(tmp_path, ita=_ITALIAN))

        assert list(card["cells"]) == ["cp1252 ita"]


class TestCompare:
    @staticmethod
    def _card(plain: str, mixed: str = "11") -> dict:
        return {"cells": {"cp1252 ita": {PLAIN: plain, MIXED: mixed}}, "unicode": "15.0.0",
                "totals": {PLAIN: [plain.count("1"), len(plain)], MIXED: [mixed.count("1"), len(mixed)]}}

    def test_sample_that_was_right_and_is_wrong_is_worse_whatever_the_count(self):
        """The rule that read French with a Slavic name as Czech read more French right than it lost."""
        lines_, worse = compare(self._card("0111"), self._card("1100"))

        assert worse == ["cp1252 ita"]
        assert any("2 -> 3 of 4, 1 lost" in line for line in lines_)

    def test_samples_gained_are_not_worse(self):
        lines_, worse = compare(self._card("1110"), self._card("1100"))

        assert worse == [] and any("2 -> 3 of 4" in line and "lost" not in line for line in lines_)

    def test_cell_of_other_samples_than_the_baseline_is_worse(self):
        _, worse = compare(self._card("111"), self._card("1100"))

        assert worse == ["cp1252 ita"]

    def test_baseline_of_other_unicode_tables_is_said(self):
        lines_, _ = compare({**self._card("1100"), "unicode": "16.0.0"}, self._card("1100"))

        assert "Unicode 15.0.0" in lines_[0] and "16.0.0" in lines_[0]

    def test_cell_that_did_not_move_has_no_line(self):
        lines_, worse = compare(self._card("1100"), self._card("1100"))

        assert worse == [] and not any("cp1252" in line for line in lines_)


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

    def test_folder_without_a_text_of_the_scorecard_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--texts", str(_texts(tmp_path))])
