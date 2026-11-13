"""Time bounds for tests marked `timing`, scaled to the machine that runs them.

A bound written in seconds holds on the machine it was measured on. CI runners are several times slower and differ among
themselves, so a bound with a comfortable margin locally failed there by a hair, while a bound loose enough for the slowest
runner no longer tells the fixed code from the slow code locally. `allowed` keeps the bound as written on a machine as fast
as the reference and stretches it in proportion on a slower one.
"""

import time


_REFERENCE_SECONDS = 0.04  # what the loop in `allowed` takes on the machine the bounds were written on
_MAX_STRETCH = 8.0


def allowed(seconds: float) -> float:
    """`seconds` on a machine as fast as the reference one; proportionally more on a slower one, at most 8 times as much."""
    started = time.perf_counter()
    total = 0
    for number in range(1_000_000):
        total += number * number
    spent = time.perf_counter() - started
    return seconds * min(max(spent / _REFERENCE_SECONDS, 1.0), _MAX_STRETCH)
