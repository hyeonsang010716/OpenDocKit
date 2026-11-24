"""One document as HWP and as HWPX, converted to the same text (`evals/parity.py`)."""

import json

from evals.parity import compare, first_difference, main, run
from opendockit.extractors.hwp import TAG_PARA_HEADER, TAG_PARA_TEXT
from tests import builder
from tests.test_hwp import _fake_hwp


_BROKEN = b"PK\x03\x04" + b"\x00" * 40   # the start of a ZIP archive and nothing after it


def _hwp(*texts: str) -> bytes:
    records = b"".join(builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                       + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(text)) for text in texts)
    return _fake_hwp(None, sections=[records])


def _hwpx(*texts: str) -> bytes:
    return builder.hwpx_bytes("".join(f"<hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>" for text in texts))


def _folder(tmp_path, **documents: bytes):
    folder = tmp_path / "pairs"
    folder.mkdir(exist_ok=True)
    for name, data in documents.items():
        (folder / name.replace("_", ".")).write_bytes(data)
    return folder


class TestRun:
    def test_a_pair_converted_to_one_text_is_alike(self, tmp_path):
        card, differences = run(_folder(tmp_path, a_hwp=_hwp("첫 문단", "둘째 문단"), a_hwpx=_hwpx("첫 문단", "둘째 문단")))

        assert card == {"totals": {"pairs": 1, "alike": 1, "apart": 0, "refused": 0}, "pairs": {"a": "alike"}}
        assert differences == {}

    def test_a_pair_converted_to_two_texts_is_apart_and_its_first_difference_is_kept(self, tmp_path):
        card, differences = run(_folder(tmp_path, a_hwp=_hwp("첫 문단", "둘째 문단", "끝"), a_hwpx=_hwpx("첫 문단", "다른 문단", "끝")))

        assert card["pairs"] == {"a": "apart"}
        assert differences == {"a": ("둘째 문단", "다른 문단")}

    def test_a_pair_with_a_refused_file_is_refused(self, tmp_path):
        card, differences = run(_folder(tmp_path, a_hwp=_hwp("첫 문단"), a_hwpx=_BROKEN))

        assert card["pairs"] == {"a": "refused"}
        assert differences == {}

    def test_a_file_without_its_twin_is_no_pair(self, tmp_path):
        card, _ = run(_folder(tmp_path, a_hwp=_hwp("첫 문단"), b_hwpx=_hwpx("첫 문단")))

        assert card == {"totals": {"pairs": 0, "alike": 0, "apart": 0, "refused": 0}, "pairs": {}}

    def test_the_card_holds_no_text_of_a_document(self, tmp_path):
        card, _ = run(_folder(tmp_path, a_hwp=_hwp("첫 문단"), a_hwpx=_hwpx("다른 문단")))

        assert "문단" not in json.dumps(card, ensure_ascii=False)


class TestFirstDifference:
    def test_blocks_that_are_only_in_one_text(self):
        assert first_difference("가\n\n나\n\n다", "가\n\n다") == ("나", "")

    def test_equal_texts_have_none(self):
        assert first_difference("가\n\n나", "가\n\n나") is None


def _card(**pairs: str) -> dict:
    totals = {state: sum(value == state for value in pairs.values()) for state in ("alike", "apart", "refused")}
    return {"totals": {"pairs": len(pairs), **totals}, "pairs": pairs}


class TestCompare:
    def test_a_pair_that_was_alike_and_is_apart_is_worse(self):
        lines, worse = compare(_card(a="apart", b="alike"), _card(a="alike", b="alike"))

        assert worse
        assert "  a: alike -> apart" in lines

    def test_a_pair_that_was_alike_and_is_refused_is_worse(self):
        assert compare(_card(a="refused"), _card(a="alike"))[1]

    def test_a_pair_that_was_apart_and_is_alike_is_listed_and_not_worse(self):
        lines, worse = compare(_card(a="alike"), _card(a="apart"))

        assert not worse
        assert "  a: apart -> alike" in lines

    def test_a_pair_the_baseline_does_not_know_is_not_judged(self):
        lines, worse = compare(_card(a="alike", new="apart"), _card(a="alike"))

        assert not worse
        assert not any("new" in line for line in lines)


class TestMain:
    def test_a_baseline_is_written_and_then_compared_with(self, tmp_path, capsys):
        folder = _folder(tmp_path, a_hwp=_hwp("첫 문단"), a_hwpx=_hwpx("첫 문단"))
        baseline = tmp_path / "parity.json"

        assert main(["--documents", str(folder), "--baseline", str(baseline), "--update-baseline"]) == 0
        assert json.loads(baseline.read_text(encoding="utf-8"))["pairs"] == {"a": "alike"}
        assert main(["--documents", str(folder), "--baseline", str(baseline)]) == 0

        (folder / "a.hwpx").write_bytes(_hwpx("다른 문단"))
        capsys.readouterr()

        assert main(["--documents", str(folder), "--baseline", str(baseline), "--show", "1"]) == 1
        output = capsys.readouterr().out
        assert "1 pairs: 0 alike, 1 apart, 0 refused" in output
        assert "  a: alike -> apart" in output
        assert "  hwp:  첫 문단" in output
        assert "  hwpx: 다른 문단" in output
