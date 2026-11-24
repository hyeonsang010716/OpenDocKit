"""Damaged copies of a document, converted in a worker (`evals/damage.py`)."""

from concurrent.futures.process import BrokenProcessPool
from io import BytesIO
import zipfile

import olefile
import pytest

from evals.damage import Dice, _outcome, damaged, flipped, main, report, with_value
from opendockit import CorruptedDocumentError, DocumentTooLargeError
from opendockit.extractors.hwp import TAG_PARA_HEADER, TAG_PARA_TEXT
from tests import builder
from tests.test_hwp import _fake_hwp


def _hwp(flags: int = 1) -> bytes:
    records = b"".join(builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
                       + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(f"문단 {index}")) for index in range(40))
    return _fake_hwp(None, flags=flags, sections=[records])


def _hwpx() -> bytes:
    return builder.hwpx_bytes("".join(f'<hp:p id="{index}"><hp:run><hp:t>문단 {index}</hp:t></hp:run></hp:p>' for index in range(40)))


class TestDice:
    def test_the_numbers_depend_on_the_names_alone(self):
        first, again, other = Dice("a.hwp", "0"), Dice("a.hwp", "0"), Dice("a.hwp", "1")

        drawn = [first.below(1000) for _ in range(20)]

        assert drawn == [again.below(1000) for _ in range(20)]
        assert drawn != [other.below(1000) for _ in range(20)]
        assert all(0 <= number < 1000 for number in drawn)
        assert len(set(drawn)) > 10


class TestDamageOfBytes:
    def test_flipped_changes_one_bit_at_each_place(self):
        data = bytes(200)

        changed = flipped(data, Dice("x"), 3)

        assert len(changed) == len(data)
        assert 1 <= sum(bin(byte).count("1") for byte in changed) <= 3

    def test_flipped_leaves_nothing_as_it_is(self):
        assert flipped(b"", Dice("x"), 3) == b""

    def test_with_value_writes_a_number_and_keeps_the_length(self):
        data = bytes(range(100, 200))

        changed = with_value(data, Dice("x"))

        assert len(changed) == len(data)
        assert changed != data
        assert with_value(b"short", Dice("x")) == b"short"


class TestDamaged:
    def test_the_copies_are_the_same_on_every_run(self):
        data = _hwpx()

        assert list(damaged("a.hwpx", data)) == list(damaged("a.hwpx", data))

    def test_another_round_damages_other_places_and_does_not_repeat_the_whole_document(self):
        data = _hwpx()
        first, second = dict(damaged("a.hwpx", data)), dict(damaged("a.hwpx", data, 1))

        assert first["whole"] == data
        assert "whole" not in second
        assert first["bits flipped 0"] != second["bits flipped 0"]

    def test_the_file_is_cut_flipped_and_zeroed(self):
        data = _hwpx()
        copies = dict(damaged("a.hwpx", data))

        assert copies["cut at 0.5"] == data[:len(data) // 2]
        assert copies["cut one byte short"] == data[:-1]
        assert len(copies["bits flipped 0"]) == len(data) and copies["bits flipped 0"] != data
        assert len(copies["zeros"]) == len(data) and bytes(64) in copies["zeros"]

    def test_a_part_of_a_package_is_damaged_inside_an_archive_that_still_opens(self):
        copies = dict(damaged("a.hwpx", _hwpx()))
        names = [name for name in copies if name.startswith("Contents/section0.xml: ")]

        assert {name.split(": ")[1].rstrip(" 0123456789") for name in names} == {"bits flipped", "number", "cut", "left out"}
        for name in names:
            with zipfile.ZipFile(BytesIO(copies[name])) as archive:
                assert archive.testzip() is None
                assert ("Contents/section0.xml" in archive.namelist()) == (not name.endswith("left out"))
        with zipfile.ZipFile(BytesIO(copies["Contents/section0.xml: cut"])) as archive, zipfile.ZipFile(BytesIO(_hwpx())) as whole:
            assert len(archive.read("Contents/section0.xml")) == len(whole.read("Contents/section0.xml")) // 2

    @pytest.mark.parametrize("flags", [0, 1], ids=["plain", "compressed"])
    def test_a_stream_of_an_hwp_file_is_damaged_inside_a_container_that_still_opens(self, flags):
        data = _hwp(flags)
        copies = dict(damaged("a.hwp", data))
        names = [name for name in copies if name.startswith("BodyText/Section0: ")]

        assert {name.split(": ")[1].rstrip(" 0123456789") for name in names} == {"bits flipped", "value", "cut", "cut inside a record"}
        for name in names:
            with olefile.OleFileIO(BytesIO(copies[name])) as ole:
                assert ole.exists("BodyText/Section0") and ole.exists("FileHeader")
                assert ole.openstream("DocInfo").read() == olefile.OleFileIO(BytesIO(data)).openstream("DocInfo").read()
        with olefile.OleFileIO(BytesIO(copies["without its body"])) as ole:
            assert not ole.exists("BodyText/Section0")

    def test_a_file_that_is_no_container_gets_the_damage_of_the_file_alone(self):
        names = [name for name, _ in damaged("a.hwp", b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + bytes(300))]

        assert names[0] == "whole"
        assert all(":" not in name for name in names)
        assert "without its body" not in names


class TestOutcome:
    def test_what_a_copy_may_do(self):
        assert _outcome(None) == "read"
        assert _outcome(CorruptedDocumentError("x")) == "refused"
        assert _outcome(DocumentTooLargeError("x")) == "refused"

    def test_what_it_may_not(self):
        hung, died = DocumentTooLargeError("x"), DocumentTooLargeError("x")
        hung.__cause__ = TimeoutError()
        died.__cause__ = BrokenProcessPool()

        assert _outcome(hung) == "hung"
        assert _outcome(died) == "died"
        assert _outcome(ImportError("x")) == "raised"
        assert _outcome(ValueError("x")) == "raised"


class TestReport:
    def test_counts_the_outcomes_and_names_the_slowest(self):
        results = [{"file": "a.hwp", "damage": "whole", "outcome": "read", "seconds": 0.1},
                   {"file": "a.hwp", "damage": "cut at 0.5", "outcome": "refused", "seconds": 0.3}]

        lines, failed = report(results)

        assert not failed
        assert lines == ["2 conversions of 1 documents: 1 read, 1 refused, 0 died, 0 hung, 0 raised", "slowest: 0.3 s  a.hwp  cut at 0.5"]

    @pytest.mark.parametrize("outcome", ["died", "hung", "raised"])
    def test_a_copy_that_ends_a_worker_hangs_or_raises_fails_the_run(self, outcome):
        lines, failed = report([{"file": "a.hwp", "damage": "zeros", "outcome": outcome, "seconds": 1.0}])

        assert failed
        assert f"  {outcome.upper()}  a.hwp  zeros  1.0 s" in lines

    def test_many_failures_are_cut_short(self):
        lines, _ = report([{"file": "a.hwp", "damage": "zeros", "outcome": "died", "seconds": 1.0}] * 5, shown=2)

        assert "  ... and 3 more" in lines

    def test_nothing_converted(self):
        assert report([]) == (["0 conversions of 0 documents: 0 read, 0 refused, 0 died, 0 hung, 0 raised"], False)


# Starts worker processes: run serially, and never again when a worker ends wrongly (see `timing` in pyproject.toml).
@pytest.mark.timing
@pytest.mark.no_rerun
class TestMain:
    def test_every_copy_of_a_folder_is_converted_in_a_worker(self, tmp_path, capsys):
        folder = tmp_path / "documents"
        folder.mkdir()
        (folder / "a.hwp").write_bytes(_hwp())
        (folder / "a.hwpx").write_bytes(_hwpx())
        expected = len(list(damaged("a.hwp", _hwp()))) + len(list(damaged("a.hwpx", _hwpx())))

        status = main(["--documents", str(folder), "--keep", str(tmp_path / "kept")])

        output = capsys.readouterr().out
        assert status == 0
        assert f"{expected} conversions of 2 documents: " in output
        assert "0 died, 0 hung, 0 raised" in output
        assert not (tmp_path / "kept").exists()

    def test_more_rounds_convert_more_copies(self, tmp_path, capsys):
        folder = tmp_path / "documents"
        folder.mkdir()
        (folder / "a.hwpx").write_bytes(_hwpx())
        one_round = len(list(damaged("a.hwpx", _hwpx())))

        assert main(["--documents", str(folder), "--rounds", "2"]) == 0
        assert f"{one_round * 2 - 1} conversions of 1 documents: " in capsys.readouterr().out
