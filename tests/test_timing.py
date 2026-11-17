"""The measurements of `tests/timing.py` and the second chances `tests/conftest.py` gives a timing test."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests import conftest, timing
from tests.timing import Fastest, fastest


class _Clock:
    """`time.perf_counter` replaced by readings given in advance; `gc.collect` recorded among them."""

    def __init__(self, monkeypatch, readings: list[float]) -> None:
        self.readings = list(readings)
        self.events: list[str] = []
        monkeypatch.setattr(timing.time, "perf_counter", self._read)
        monkeypatch.setattr(timing.gc, "collect", lambda: self.events.append("collect"))

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


def test_untimed_fails_once():
    assert _run("untimed") > 1
'''


class TestTimingTestsAreRunAgain:
    """A failed timing test runs twice more and passes when one run does; any other test gets no second chance (`conftest.py`)."""

    @staticmethod
    def _run(tmp_path, *options: str) -> tuple[subprocess.CompletedProcess, dict[str, int]]:
        """Runs `_INNER_TESTS` in a pytest of its own with this suite's `conftest.py` (reruns without the delay)."""
        source = Path(conftest.__file__).read_text(encoding="utf-8")
        assert source.count("TIMING_RERUN_DELAY = 1.0\n") == 1
        (tmp_path / "conftest.py").write_text(source.replace("TIMING_RERUN_DELAY = 1.0\n", "TIMING_RERUN_DELAY = 0\n"),
                                              encoding="utf-8")
        (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    timing: measures time\n", encoding="utf-8")
        (tmp_path / "test_inner.py").write_text(_INNER_TESTS, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
        run = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "--strict-markers",
                              "-q", *options, "test_inner.py"], cwd=tmp_path, env=environment, capture_output=True, text=True,
                             timeout=120)
        return run, {name: int((tmp_path / name).read_text()) for name in ("stalled", "slow", "class", "untimed")}

    def test_reruns_in_a_real_pytest_run(self, tmp_path):
        run, runs = self._run(tmp_path)

        assert run.returncode == 1, run.stdout + run.stderr
        assert "2 failed, 2 passed, 4 rerun" in run.stdout, run.stdout
        assert runs == {"stalled": 2, "slow": 3, "class": 2, "untimed": 1}

    def test_with_the_plugin_switched_off_every_test_runs_once(self, tmp_path):
        """`--strict-markers` refused the whole run when the marker was added without the plugin that defines it."""
        run, runs = self._run(tmp_path, "-p", "no:rerunfailures")

        assert run.returncode == 1, run.stdout + run.stderr
        assert "4 failed in" in run.stdout and "rerun" not in run.stdout, run.stdout
        assert runs == {"stalled": 1, "slow": 1, "class": 1, "untimed": 1}
