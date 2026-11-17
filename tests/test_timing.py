"""The measurements of `tests/timing.py` and the second chances `tests/conftest.py` gives a timing test."""

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from tests import conftest, timing
from tests.timing import Fastest, fastest


class _Clock:
    """The clock of `tests/timing.py` replaced by readings given in advance, its garbage collection recorded among them."""

    def __init__(self, monkeypatch, readings: list[float]) -> None:
        self.readings = list(readings)
        self.events: list[str] = []
        monkeypatch.setattr(timing, "_clock", self._read)
        monkeypatch.setattr(timing, "_collect", lambda: self.events.append("collect"))

    def _read(self) -> float:
        self.events.append("clock")
        return self.readings.pop(0)


class TestFastestOfAFewRuns:
    def test_the_fastest_run_is_taken(self, monkeypatch):
        _Clock(monkeypatch, [0.0, 5.0, 10.0, 11.0, 20.0, 23.0])  # runs of 5, 1 and 3 seconds
        calls = []

        assert fastest(lambda: calls.append(1)) == 1.0
        assert len(calls) == 3

    def test_number_of_runs_can_be_given(self, monkeypatch):
        _Clock(monkeypatch, [0.0, 4.0, 10.0, 12.0])
        calls = []

        assert fastest(lambda: calls.append(1), runs=2) == 2.0
        assert len(calls) == 2

    def test_garbage_is_collected_before_each_timed_block_not_inside_it(self, monkeypatch):
        clock = _Clock(monkeypatch, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

        fastest(lambda: None)

        assert clock.events == ["collect", "clock", "clock"] * 3

    def test_input_made_anew_for_every_run_is_not_timed(self, monkeypatch):
        _Clock(monkeypatch, [0.0, 2.0, 10.0, 13.0, 20.0, 22.0])
        made = []
        timer = Fastest()
        for _ in timer:
            made.append(object())  # between the readings: not part of any run
            with timer:
                pass

        assert timer.seconds == 2.0 and len(made) == 3

    def test_failed_run_is_not_a_measurement_and_the_failure_goes_on(self, monkeypatch):
        _Clock(monkeypatch, [0.0, 0.1])
        timer = Fastest()

        with pytest.raises(AssertionError):
            for _ in timer:
                with timer:
                    raise AssertionError

        assert timer.seconds == float("inf")

    def test_timer_never_entered_fails_any_bound(self):
        timer = Fastest()
        for _ in timer:
            pass

        assert not timer.seconds < 1e300

    def test_at_least_one_run(self):
        with pytest.raises(ValueError, match="at least 1"):
            Fastest(0)

    @pytest.mark.timing
    def test_real_clock_measures_the_work(self):
        assert 0 < fastest(lambda: sum(range(10_000))) < 1


_INNER_TESTS = '''
from pathlib import Path

import pytest


def _run(name):
    path = Path(__file__).with_name(name)
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count


@pytest.mark.timing
def test_timing_stalled_once():
    assert _run("stalled") > 1


@pytest.mark.timing
def test_timing_slow_every_time():
    _run("slow")
    assert False


@pytest.mark.timing
class TestTimingClass:
    def test_in_a_timing_class(self):
        assert _run("class") > 1


@pytest.mark.timing
@pytest.mark.no_rerun
def test_timing_race_fails_once():
    assert _run("race") > 1


def test_untimed_fails_once():
    assert _run("untimed") > 1
'''


def _outcomes(output: str) -> dict[str, int]:
    """The counts of the last line of a pytest run ('2 failed, 2 passed, 13 warnings, 4 rerun in 1.04s'), warnings left out:
    a plugin's deprecation warnings vary with the Python version."""
    counts = re.findall(r"(\d+) (failed|passed|rerun|error|errors|skipped)\b", output.strip().splitlines()[-1])
    return {name: int(number) for number, name in counts}


class TestTimingTestsAreRunAgain:
    """A failed timing test runs twice more and passes when one run does; one marked `no_rerun` and any other test get no second
    chance (`conftest.py`)."""

    @staticmethod
    def _run(tmp_path, *options: str, tests: str = _INNER_TESTS, max_reruns: int = 10) -> tuple[str, dict[str, int]]:
        """Runs `tests` in a pytest of its own with this suite's `conftest.py` (reruns without the delay, at most `max_reruns`):
        the summary line's counts and how many times each counter file was run."""
        source = Path(conftest.__file__).read_text(encoding="utf-8")
        for line, value in (("TIMING_RERUN_DELAY = 1.0\n", "TIMING_RERUN_DELAY = 0\n"),
                            ("TIMING_MAX_RERUNS = 10\n", f"TIMING_MAX_RERUNS = {max_reruns}\n")):
            assert source.count(line) == 1
            source = source.replace(line, value)
        (tmp_path / "conftest.py").write_text(source, encoding="utf-8")
        (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    timing: measures time\n    no_rerun: checks a race\n",
                                            encoding="utf-8")
        (tmp_path / "test_inner.py").write_text(tests, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
        run = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "--strict-markers",
                              "-q", *options, "test_inner.py"], cwd=tmp_path, env=environment, capture_output=True, text=True,
                             timeout=120)
        assert run.returncode == 1, run.stdout + run.stderr
        runs = {path.name: int(path.read_text()) for path in tmp_path.iterdir() if path.suffix == "" and path.is_file()}
        return run.stdout, runs | {"outcomes": _outcomes(run.stdout)}

    def test_reruns_in_a_real_pytest_run(self, tmp_path):
        output, runs = self._run(tmp_path)

        assert runs == {"stalled": 2, "slow": 3, "class": 2, "race": 1, "untimed": 1,
                        "outcomes": {"failed": 3, "passed": 2, "rerun": 4}}, output
        # the failure of an attempt that was run again is printed, also when a later attempt passed
        assert "test_timing_stalled_once" in output.split("rerun test summary info")[1].split("short test summary info")[0]
        assert "assert 1 > 1" in output.split("rerun test summary info")[1], output

    def test_with_the_plugin_switched_off_every_test_runs_once(self, tmp_path):
        """`--strict-markers` refused the whole run when the marker was added without the plugin that defines it."""
        output, runs = self._run(tmp_path, "-p", "no:rerunfailures")

        assert runs == {"stalled": 1, "slow": 1, "class": 1, "race": 1, "untimed": 1, "outcomes": {"failed": 5}}, output

    def test_reruns_stop_at_the_limit_for_the_session(self, tmp_path):
        """Code that became slow fails many timing tests: past the limit they fail on their first run, so the step does not triple."""
        tests = _INNER_TESTS.split("@pytest.mark.timing", 1)[0] + "".join(
            f"\n\n@pytest.mark.timing\ndef test_slow_{index}():\n    _run('slow{index}')\n    assert False\n" for index in range(6))

        output, runs = self._run(tmp_path, tests=tests, max_reruns=3)

        assert runs["outcomes"] == {"failed": 6, "rerun": 3}, output
        assert sum(runs[f"slow{index}"] for index in range(6)) == 6 + 3
