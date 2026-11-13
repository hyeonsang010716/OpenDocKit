import collections
import gc
import sys
import time

import pytest

from opendockit import convert
from tests import builder


def _document(count: int) -> bytes:
    body = "".join(f'<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="{index}" type="MEMO"><hp:subList><hp:p><hp:run>'
                   f"<hp:t>메모{index}</hp:t></hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl></hp:run></hp:p>"
                   for index in range(count))
    return builder.hwpx_bytes(body)


def _timed(data: bytes) -> str:
    spent = {"gc": 0.0, "runs": [0, 0, 0]}
    started_at = [0.0]

    def watch(phase, info):
        if phase == "start":
            started_at[0] = time.perf_counter()
        else:
            spent["gc"] += time.perf_counter() - started_at[0]
            spent["runs"][info["generation"]] += 1

    gc.callbacks.append(watch)
    try:
        started = time.perf_counter()
        convert(data, "a.hwpx")
        total = time.perf_counter() - started
    finally:
        gc.callbacks.remove(watch)
    return f"{total:.3f}s (gc {spent['gc']:.3f}s, runs {spent['runs']})"


def probe(label: str) -> None:
    from tests.conftest import HEAP_BY_MODULE
    small, large = _document(4_000), _document(8_000)
    lines = [f"[{label}] objects={len(gc.get_objects())} threshold={gc.get_threshold()} count={gc.get_count()}"]
    for attempt in range(3):
        lines.append(f"  enabled #{attempt}: small {_timed(small)}  large {_timed(large)}")
    gc.disable()
    try:
        for attempt in range(2):
            lines.append(f"  disabled #{attempt}: small {_timed(small)}  large {_timed(large)}")
    finally:
        gc.enable()
    kinds = collections.Counter(type(item).__name__ for item in gc.get_objects())
    lines.append("  kinds: " + ", ".join(f"{name}={number}" for name, number in kinds.most_common(8)))
    lines.append("  heap: " + ", ".join(f"{name.removeprefix('tests.').removeprefix('test_')}={number}" for name, number in HEAP_BY_MODULE))
    if sys.platform == "win32":
        pytest.fail("\n".join(lines), pytrace=False)
    print("\n".join(lines))
