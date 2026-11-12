"""Conversion isolated in worker processes (`IsolatedConverter`): does one file's runaway time or memory, or the death of its
worker, end as a failure of that file only?"""

import asyncio
import os
import signal
import sys
import time

import pytest

from opendockit import (
    TOO_COMPLEX_MESSAGE,
    ConvertOptions,
    CorruptedDocumentError,
    DocumentTooLargeError,
    IsolatedConverter,
    convert,
)


# Every test here starts worker processes and waits on real timeouts: run serially (see `timing` in pyproject.toml).
pytestmark = pytest.mark.timing


_SMALL_TEXT = "첫 줄\n\n둘째 문단입니다.\n".encode()
# A CSV (8MB) that takes around 3.5 seconds on this machine — comfortably longer than the 1-second time limit
_SLOW_CSV = ("가나다 라마바 사아자 1234,5678\n" * 200_000).encode()
_NO_LIMIT = ConvertOptions(max_chars=None)


def _worker_pids(converter: IsolatedConverter) -> list[int]:
    return [pid for pool in converter._pools for pid in (pool._processes or {})]


async def _wait_for_worker(converter: IsolatedConverter) -> list[int]:
    for _ in range(200):
        pids = _worker_pids(converter)
        if pids:
            return pids
        await asyncio.sleep(0.01)
    raise AssertionError("the worker did not start")


def _is_alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
        os.kill(pid, 0)
    except (ChildProcessError, ProcessLookupError):
        return False
    return True


class TestIsolatedConverter:
    def test_result_is_the_same_as_converting_in_the_server_process(self):
        async def scenario():
            async with IsolatedConverter() as converter:
                return await converter.convert(_SMALL_TEXT, "a.txt")

        result = asyncio.run(scenario())

        assert result.text == convert(_SMALL_TEXT, "a.txt").text

    def test_ordinary_conversion_failure_comes_back_as_the_same_error(self):
        async def scenario():
            async with IsolatedConverter() as converter:
                await converter.convert(b"", "a.txt")

        with pytest.raises(CorruptedDocumentError, match="The file is empty"):
            asyncio.run(scenario())

    def test_wrong_argument_is_a_type_error_before_reaching_a_worker(self):
        async def scenario():
            async with IsolatedConverter() as converter:
                await converter.convert("a.txt")

        with pytest.raises(TypeError, match="convert_file"):
            asyncio.run(scenario())

    @pytest.mark.skipif(sys.platform == "win32", reason="checks worker processes with POSIX waitpid")
    def test_time_limit_kills_that_worker_and_the_next_file_converts(self):
        """`asyncio.wait_for` alone only stopped the waiting; the worker kept running (docs/server.md)."""
        async def scenario():
            async with IsolatedConverter(timeout=1.0) as converter:
                task = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                pids = await _wait_for_worker(converter)
                with pytest.raises(DocumentTooLargeError) as caught:
                    await task
                await asyncio.sleep(0.1)
                started = time.perf_counter()
                result = await converter.convert(_SMALL_TEXT, "a.txt")
                return caught.value, pids, result, time.perf_counter() - started

        error, pids, result, elapsed = asyncio.run(scenario())

        assert str(error) == TOO_COMPLEX_MESSAGE
        assert not any(_is_alive(pid) for pid in pids)
        assert "둘째 문단" in result.text
        assert elapsed < 1.0

    def test_worker_that_dies_ends_only_that_file(self):
        """When memory runs short a C extension kills the whole process instead of raising. The server reports only that file as
        failed and accepts the next one."""
        async def scenario():
            async with IsolatedConverter() as converter:
                task = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                for pid in await _wait_for_worker(converter):
                    os.kill(pid, signal.SIGKILL)
                with pytest.raises(DocumentTooLargeError):
                    await task
                return await converter.convert(_SMALL_TEXT, "a.txt")

        result = asyncio.run(scenario())

        assert "둘째 문단" in result.text

    @pytest.mark.skipif(sys.platform == "win32", reason="checks worker processes with POSIX waitpid")
    def test_cancelled_request_stops_its_worker(self):
        """If the worker kept running after the request was dropped, the next file queued up behind it."""
        async def scenario():
            async with IsolatedConverter() as converter:
                task = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                pids = await _wait_for_worker(converter)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0.1)
                started = time.perf_counter()
                await converter.convert(_SMALL_TEXT, "a.txt")
                return pids, time.perf_counter() - started

        pids, elapsed = asyncio.run(scenario())

        assert not any(_is_alive(pid) for pid in pids)
        assert elapsed < 1.0

    def test_each_file_gets_a_fresh_worker(self):
        """Each file gets a new worker so that memory grown by a large document does not linger in the worker."""
        async def scenario():
            async with IsolatedConverter() as converter:
                first = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                first_pids = await _wait_for_worker(converter)
                await first
                second = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                await asyncio.sleep(0.2)
                second_pids = _worker_pids(converter)
                await second
                return first_pids, second_pids

        first_pids, second_pids = asyncio.run(scenario())

        assert second_pids
        assert not set(first_pids) & set(second_pids)

    def test_stopping_workers_waits_for_the_pool_manager_thread(self):
        """Stopping with `shutdown(wait=False)` set `_processes` to None while the manager thread was replacing a finished worker,
        and that thread died with a TypeError (a traceback in the server log, 3 times per run of the 15 tests)."""
        async def scenario():
            converter = IsolatedConverter()
            await converter.convert(_SMALL_TEXT, "a.txt")
            manager = converter._pools[0]._executor_manager_thread
            await converter.aclose()
            return manager

        manager = asyncio.run(scenario())

        assert not manager.is_alive()

    def test_closed_converter_refuses_new_files(self):
        async def scenario():
            converter = IsolatedConverter()
            await converter.aclose()
            await converter.convert(_SMALL_TEXT, "a.txt")

        with pytest.raises(RuntimeError, match="closed"):
            asyncio.run(scenario())

    @pytest.mark.parametrize(("field", "value"), [("workers", 0), ("workers", 1.5), ("memory_limit", 0), ("timeout", 0),
                                                  ("timeout", float("nan")), ("timeout", True)])
    def test_meaningless_limits_are_rejected(self, field, value):
        with pytest.raises(ValueError, match=field):
            IsolatedConverter(**{field: value})

    @pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced only on Linux")
    def test_memory_limit_ends_that_file_only(self):
        """When a worker uses more memory than the limit, only that file ends as "too large or too complex"."""
        async def scenario():
            async with IsolatedConverter(memory_limit=64 * 1024**2) as converter:
                with pytest.raises(DocumentTooLargeError):
                    await converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT)
            async with IsolatedConverter() as converter:
                return await converter.convert(_SMALL_TEXT, "a.txt")

        result = asyncio.run(scenario())

        assert "둘째 문단" in result.text
