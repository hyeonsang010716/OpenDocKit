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
from tests import process_is_alive


# Every test here starts worker processes and waits on real timeouts: run serially (see `timing` in pyproject.toml).
pytestmark = [
    pytest.mark.timing,
    pytest.mark.no_rerun,  # a worker that ends wrongly once is a bug, not a slow runner: never run again
]


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
    """`process_is_alive`, and for a child of this process also reaped first: until `waitpid` collects it, a worker that
    has ended still has a pid that answers."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, AttributeError, OSError):   # not our child, already reaped, or no waitpid (Windows)
        pass
    return process_is_alive(pid)


def _hard_kill(pid: int) -> None:
    """End a process outright, so that nothing of its `finally` runs. On Windows every signal but the console ones is
    `TerminateProcess`, which is what SIGKILL is here."""
    os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)


@pytest.mark.skipif(os.name != "nt", reason="the handle wait is the Windows mechanism")
class TestWorkerLeavesWithItsServer:
    """How a worker decides its server is gone on Windows. The job object ends it too, but this is the part that works
    where no job could be made, so it has to be right on its own."""

    def test_a_pid_that_does_not_exist_is_read_as_gone(self):
        from opendockit import _windows

        assert _windows.wait_for_process(999_999) is True

    def test_a_server_that_cannot_be_waited_for_does_not_end_the_worker(self, monkeypatch):
        """A handle that cannot be opened for any reason but "no such process" says nothing about whether the server is
        alive. Read as "gone", it ended every worker the moment it started, and every document on that machine came back
        as "too large or too complex"."""
        from opendockit import isolated

        ended: list[int] = []
        monkeypatch.setattr(isolated._windows, "wait_for_process", lambda pid: False)
        monkeypatch.setattr(os, "_exit", lambda code: ended.append(code))

        isolated._leave_with_windows(12_345)

        assert ended == []

    def test_a_server_that_has_ended_ends_the_worker(self, monkeypatch):
        from opendockit import isolated

        ended: list[int] = []
        monkeypatch.setattr(isolated._windows, "wait_for_process", lambda pid: True)
        monkeypatch.setattr(os, "_exit", lambda code: ended.append(code))

        isolated._leave_with_windows(12_345)

        assert ended == [0]


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

    def test_oversized_or_empty_input_is_refused_before_a_worker_starts(self):
        """An upload over `max_file_bytes` was pickled into a worker (copied through a pipe and again there) only to be
        refused by the first check of `convert`. It is refused in the server process with the same error; so is an empty file."""
        submitted = []

        async def scenario(data):
            async with IsolatedConverter() as converter:
                for pool in converter._pools:
                    original = pool.submit
                    pool.submit = lambda *args, _original=original, **kwargs: submitted.append(args) or _original(*args, **kwargs)
                await converter.convert(data, "a.txt", ConvertOptions(max_file_bytes=1024))

        with pytest.raises(DocumentTooLargeError, match=r"^The file is too large \(maximum 1KB\)\.$"):
            asyncio.run(scenario(b"a" * 4096))
        with pytest.raises(CorruptedDocumentError, match="^The file is empty.$"):
            asyncio.run(scenario(b""))
        assert submitted == []

    def test_wrong_argument_is_a_type_error_before_reaching_a_worker(self):
        async def scenario():
            async with IsolatedConverter() as converter:
                await converter.convert("a.txt")

        with pytest.raises(TypeError, match="convert_file"):
            asyncio.run(scenario())

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
                    _hard_kill(pid)
                with pytest.raises(DocumentTooLargeError):
                    await task
                return await converter.convert(_SMALL_TEXT, "a.txt")

        result = asyncio.run(scenario())

        assert "둘째 문단" in result.text

    def test_worker_that_dies_before_it_reads_its_file_does_not_hang_the_close(self):
        """A worker killed while its file is still on the way left `aclose()` waiting for ever.

        The file travels to the worker on a queue whose feeder thread writes it into a pipe. A worker that dies before
        draining that pipe leaves the feeder blocked in a write that closing the handles does not cancel on Windows, and the
        pool's manager thread joins that feeder while it breaks the pool — so the pool never finished breaking and the close
        never returned. Seen on Windows, where a spawned worker takes 0.66 s to be ready and a large file is still in flight;
        the queue's join is cancelled when the pool is built.
        """
        async def scenario():
            async with IsolatedConverter() as converter:
                task = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", _NO_LIMIT))
                for pid in await _wait_for_worker(converter):   # as soon as it exists: before it has read the file
                    _hard_kill(pid)
                with pytest.raises(DocumentTooLargeError):
                    await task
                return await converter.convert(_SMALL_TEXT, "a.txt")

        result = asyncio.run(asyncio.wait_for(scenario(), 60))

        assert "둘째 문단" in result.text

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

    @pytest.mark.skipif(os.name != "posix", reason="the result queue has a write lock only on POSIX")
    def test_stopping_does_not_wait_for_a_worker_blocked_on_the_result_queue(self):
        """A worker killed between writing its result and releasing the result queue's write lock leaves that lock held. A
        replacement worker the first kill missed (the manager thread starts one whenever a worker exits) then blocks on the
        lock while announcing its exit, and `aclose()` waited for it forever — seen as a hung test run in a Linux container
        under load. The lock is taken here to stand for the killed worker, and the first kill is made to miss the worker.

        Windows cannot reach this: `multiprocessing` gives the result queue no write lock there, because a write that fits
        in the pipe buffer is atomic."""
        async def scenario():
            converter = IsolatedConverter()
            await converter.convert(_SMALL_TEXT, "a.txt")
            pool = converter._pools[0]
            await asyncio.sleep(0.3)  # the replacement worker is up and waiting for a call
            manager = pool._executor_manager_thread
            pool._result_queue._wlock.acquire()
            for process in pool._processes.values():
                kill, calls = process.kill, []
                process.kill = lambda kill=kill, calls=calls: (calls.append(1), kill() if len(calls) > 1 else None)[1]
            started = time.perf_counter()
            await asyncio.wait_for(converter.aclose(), 20)
            return time.perf_counter() - started, manager

        elapsed, manager = asyncio.run(scenario())

        assert elapsed < 10 and not manager.is_alive()

    @pytest.mark.skipif(os.name != "posix", reason="the fork server and the pid watch are the POSIX mechanism")
    def test_worker_does_not_outlive_a_server_that_was_killed(self, tmp_path):
        """A server process converts one file and is then killed outright (SIGKILL: no `finally`, no `aclose`) while its worker
        idles. The idle worker waited on its queue for ever, and it holds the pipes that keep multiprocessing's fork server
        and resource tracker alive: three processes stayed behind for every server that died. The worker now watches for
        its server and leaves, and the other two follow."""
        import subprocess

        script = ("import asyncio, os, signal\n"
                  "import multiprocessing.forkserver as forkserver\n"
                  "from opendockit import IsolatedConverter\n"
                  "async def main():\n"
                  "    converter = IsolatedConverter(workers=1)\n"
                  "    await converter.convert(b'hello', 'a.txt')\n"
                  "    print(forkserver._forkserver._forkserver_pid, flush=True)\n"
                  "    await asyncio.sleep(0.5)\n"
                  "    os.kill(os.getpid(), signal.SIGKILL)\n"
                  "if __name__ == '__main__':\n"
                  "    asyncio.run(main())\n")
        output = tmp_path / "pid.txt"   # a file, not a pipe: the orphans would hold a pipe open and the test would hang on it
        with output.open("w") as stdout:
            subprocess.run([sys.executable, "-c", script], stdout=stdout, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, check=False)
        helper = int(output.read_text().split()[0])

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(helper, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        os.kill(helper, signal.SIGKILL)   # do not leave it behind when the test fails
        pytest.fail("the fork server of a killed server process was still running after 15 seconds")

    @pytest.mark.skipif(os.name != "nt", reason="the job object is the Windows mechanism")
    def test_worker_does_not_outlive_a_server_that_was_killed_windows(self, tmp_path):
        """The same promise as above, kept by the job object instead of a pid watch.

        Windows has no fork server, and `os.kill(pid, 0)` cannot be used to ask whether the server is still there, so the
        worker is put into a job the server holds the only handle to. A server killed outright closes that handle by dying,
        and the kernel ends the idle worker with it — no thread of ours has to notice.
        """
        import subprocess

        script = ("import asyncio, os, signal\n"
                  "from opendockit import IsolatedConverter\n"
                  "async def main():\n"
                  "    converter = IsolatedConverter(workers=1)\n"
                  "    await converter.convert(b'hello', 'a.txt')\n"
                  "    pids = []\n"
                  "    for _ in range(500):\n"   # `max_tasks_per_child=1`: wait for the worker that idles on the next file
                  "        pids = [pid for pool in converter._pools for pid in (pool._processes or {})]\n"
                  "        if pids:\n"
                  "            break\n"
                  "        await asyncio.sleep(0.01)\n"
                  "    print(pids[0], flush=True)\n"
                  "    await asyncio.sleep(0.5)\n"
                  "    os.kill(os.getpid(), signal.SIGTERM)\n"   # TerminateProcess: no `finally`, no `aclose`
                  "if __name__ == '__main__':\n"
                  "    asyncio.run(main())\n")
        output = tmp_path / "pid.txt"   # a file, not a pipe: the orphan would hold a pipe open and the test would hang on it
        with output.open("w") as stdout:
            subprocess.run([sys.executable, "-c", script], stdout=stdout, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, check=False)
        worker = int(output.read_text().split()[0])

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not _is_alive(worker):
                return
            time.sleep(0.2)
        _hard_kill(worker)   # do not leave it behind when the test fails
        pytest.fail("the worker of a killed server process was still running after 15 seconds")

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

    @pytest.mark.skipif(sys.platform not in ("linux", "win32"),
                        reason="a memory limit is enforced by RLIMIT_AS on Linux and by the job object on Windows; macOS accepts neither")
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
