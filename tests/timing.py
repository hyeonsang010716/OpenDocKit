"""Time bounds and measurements for tests marked `timing`.

A bound written in seconds holds on the machine it was measured on. CI runners are several times slower and differ among
themselves, so a bound with a comfortable margin locally failed there by a hair, while a bound loose enough for the slowest
runner no longer tells the fixed code from the slow code locally. `allowed` keeps the bound as written on a machine as fast
as the reference and stretches it in proportion on a slower one.

A shared runner also stalls now and then for a few hundred milliseconds. Such a stall only ever adds time, so the fastest of a
few runs (`fastest`, `Fastest`) is the measurement closest to the cost of the code itself; it still shows code that became
slow, because then every run is slow. A test comparing two measurements takes the fastest of each: with one run apiece, one
stall in the larger run read as code slower than linear (20 ms against 780 ms on a macOS runner for a pair that measures 3.5 ms
against 14 ms locally). A test held to a bound in seconds instead is run again when it fails (`tests/conftest.py`), which comes
to the same — it passes when one run is within the bound — and costs nothing while it passes (Pinned by:
test_timing.py::TestFastestOfAFewRuns, TestTimingTestsAreRunAgain).
"""

from collections.abc import Callable, Iterator
import gc
import math
import time


_REFERENCE_SECONDS = 0.04  # what the loop in `allowed` takes on the machine the bounds were written on
_MAX_STRETCH = 8.0
RUNS = 3


def allowed(seconds: float) -> float:
    """`seconds` on a machine as fast as the reference one; proportionally more on a slower one, at most 8 times as much."""
    started = time.perf_counter()
    total = 0
    for number in range(1_000_000):
        total += number * number
    spent = time.perf_counter() - started
    return seconds * min(max(spent / _REFERENCE_SECONDS, 1.0), _MAX_STRETCH)


class Fastest:
    """The fastest of `runs` runs of a timed block, for a run whose input must be made anew each time (the timed code changes it
    or keeps a cache in it) without timing that:

        timer = Fastest()
        for _ in timer:
            page = build_page()
            with timer:
                read(page)
        seconds = timer.seconds

    The garbage left by earlier runs is collected before each timed block, so that collecting it does not fall into one of them;
    what the timed code itself allocates is collected as usual. A pass that never enters the timed block leaves `seconds` at
    infinity, which fails any bound.
    """

    def __init__(self, runs: int = RUNS) -> None:
        if runs < 1:
            raise ValueError("runs must be at least 1")
        self.runs = runs
        self.seconds = math.inf
        self._started: float | None = None

    def __iter__(self) -> Iterator["Fastest"]:
        for _ in range(self.runs):
            yield self

    def __enter__(self) -> "Fastest":
        gc.collect()
        self._started = time.perf_counter()
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        spent = time.perf_counter() - self._started
        self._started = None
        if kind is None:
            self.seconds = min(self.seconds, spent)


def fastest(action: Callable[[], object], runs: int = RUNS) -> float:
    """Seconds of the fastest of `runs` calls of `action`, which must do the same work every time (see `Fastest` otherwise)."""
    timer = Fastest(runs)
    for _ in timer:
        with timer:
            action()
    return timer.seconds
