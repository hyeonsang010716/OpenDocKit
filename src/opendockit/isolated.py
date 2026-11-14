"""Conversion isolated in a worker process. Keeps `convert` from running directly inside a server (FastAPI etc.) process.

    converter = IsolatedConverter(workers=2)          # create one at application startup
    result = await converter.convert(data, "report.pdf")
    await converter.aclose()                           # at application shutdown

Each file is converted in a fresh worker process. A file that is small when compressed but several GB once expanded, a file
that takes minutes, or a file on which a C extension (pdfium, lxml) takes the whole process down only ends that worker; the
server stays up. Every such failure is reported as a single `DocumentTooLargeError` ("too large or too complex"). The
rationale and measurements are in docs/server.md.
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
from multiprocessing.context import BaseContext
import os
import threading
import time
from typing import TYPE_CHECKING


try:
    import resource
except ImportError:  # not available on Windows. Only the time limit applies, without a memory limit
    resource = None

from opendockit.converter import _check_arguments, _content_bytes, check_size, convert
from opendockit.exceptions import ConversionError, DocumentTooLargeError
from opendockit.models import ConversionResult, ConvertOptions


if TYPE_CHECKING:
    from opendockit.describe import ImageDescriber


TOO_COMPLEX_MESSAGE = "The document is too large or too complex to convert."
_DEFAULT_MEMORY_LIMIT = 2 * 1024**3
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_OPTIONS = ConvertOptions()


# How often a worker looks whether its server is still there (`_leave_with`).
_SERVER_WATCH_SECONDS = 1.0

class IsolatedConverter:
    """Run `convert` in a worker process so one file's runaway memory or time does not spread to the server.

    - `workers`: number of files converted at the same time. Each worker has its own pool — so killing a worker that ran over
      time does not cut off other files. Plan for up to `workers × memory_limit` of memory.
    - `memory_limit`: address-space limit of one worker (bytes, `RLIMIT_AS`). Enforced on Linux only (macOS does not accept it).
      None means no limit.
    - `timeout`: time limit of one conversion (seconds). When it is exceeded the worker is actually terminated. Image description
      (`describer`) is network waiting, so it is not counted in this time.

    Workers are started with forkserver (spawn on platforms without it). The server process has asyncio and DB-driver threads
    running, so a fork could leave the child stuck holding a lock. This package is preloaded into the forkserver, so starting a
    new worker per file takes only a few ms (around 0.2 s without preloading). A new worker per file also means no memory is
    left behind after a large document. Known accepted gap: the forkserver preload list is one per process, so this overwrites
    a list set by other code in the same process (it does not apply to a forkserver that is already running).
    """

    def __init__(self, *, workers: int = 1, memory_limit: int | None = _DEFAULT_MEMORY_LIMIT,
                 timeout: float = _DEFAULT_TIMEOUT) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError(f"workers must be an integer of at least 1 (got {workers!r}).")
        if memory_limit is not None and (isinstance(memory_limit, bool) or not isinstance(memory_limit, int) or memory_limit < 1):
            raise ValueError(f"memory_limit must be an integer of at least 1 (bytes) or None (got {memory_limit!r}).")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError(f"timeout must be a finite number of seconds greater than 0 (got {timeout!r}).")

        self._memory_limit = memory_limit
        self._timeout = float(timeout)
        self._context = _worker_context()
        self._server = os.getpid()  # the process the pools were built in, and the one their workers watch
        self._pools = [self._new_pool() for _ in range(workers)]
        self._free: asyncio.Queue[int] = asyncio.Queue()
        for slot in range(workers):
            self._free.put_nowait(slot)
        self._closed = False

    async def convert(self, data: bytes | bytearray | memoryview, filename: str | None = None,
                      options: ConvertOptions | None = None, *, describer: "ImageDescriber | None" = None,
                      max_concurrency: int = 8) -> ConversionResult:
        """Same as `aconvert`, but the conversion runs in a worker process. With no free slot, wait until an earlier conversion ends.

        Exceeding the time limit, hitting the memory limit, or the worker dying raises `DocumentTooLargeError`
        (`TOO_COMPLEX_MESSAGE`). Any other failure is the same `ConversionError` subclass `convert` raises. An argument type
        error is a `TypeError`, and a file over `max_file_bytes` or an empty one the same error `convert` raises, before
        anything is sent to the worker.
        """
        data = _content_bytes(data)  # a memoryview cannot be sent to a worker (not picklable)
        _check_arguments(filename, options)
        if self._closed:
            raise RuntimeError("This IsolatedConverter is closed.")
        # The size checks of `convert`, here in the server process: an upload over `max_file_bytes` was pickled into a worker
        # (a copy through a pipe and a second copy there) only to be refused by the first line of `convert`
        # (Pinned by: test_isolated.py::TestIsolatedConverter::test_oversized_or_empty_input_is_refused_before_a_worker_starts).
        check_size(data, options or _DEFAULT_OPTIONS)
        if self._server != os.getpid():
            # The process was forked after this converter was built (gunicorn --preload). The pools carry the pid their
            # workers watch (`_leave_with`), and that was the master's: a server killed outright left its idle worker behind
            # for as long as the master lived. They are built again for this process (Pinned by:
            # test_audit_runtime.py::TestConverterBuiltBeforeAFork).
            self._pools = [self._new_pool() for _ in self._pools]
            self._server = os.getpid()

        result = await self._convert_in_worker(data, filename, options)
        if describer is None or not result.images:
            return result

        from opendockit.describe import describe_images

        return await describe_images(result, describer, max_concurrency=max_concurrency)

    async def aclose(self) -> None:
        """Terminate every worker. Files being converted are cut off (call this at application shutdown): they, and the files
        still waiting for a worker, end with the same `RuntimeError` a closed converter raises for a new file."""
        self._closed = True
        await asyncio.gather(*(_stop(pool) for pool in self._pools))

    async def __aenter__(self) -> "IsolatedConverter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _convert_in_worker(self, data: bytes, filename: str | None, options: ConvertOptions | None) -> ConversionResult:
        slot = await self._free_slots().get()
        try:
            if self._closed:  # closed while this file waited for a slot: its pool is shut down
                raise RuntimeError("This IsolatedConverter is closed.")
            future = asyncio.get_running_loop().run_in_executor(self._pools[slot], _run, data, filename, options)
            return await asyncio.wait_for(future, self._timeout)
        except (TimeoutError, BrokenProcessPool) as exc:
            if self._closed:
                # `aclose` killed the worker. That is a shutdown, not a document that is too large
                # (Pinned by: test_audit_runtime.py::TestClosingWithConversionsUnderWay).
                raise RuntimeError("This IsolatedConverter is closed.") from exc
            self._replace(slot)
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from exc
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if self._closed and current is not None and not current.cancelling():
                # Not a cancelled request: `aclose` shut the pool down before the worker took the file, which cancels the call
                raise RuntimeError("This IsolatedConverter is closed.") from exc
            # The worker keeps running even after the request is dropped. Left alone, the next file would queue behind it
            self._replace(slot)
            raise
        except MemoryError as exc:  # the conversion finished, but the server ran out of memory unpickling the result
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from exc
        finally:
            self._free.put_nowait(slot)

    def _free_slots(self) -> "asyncio.Queue[int]":
        """The queue of free slots, replaced when it is bound to an event loop that has been closed.

        An `asyncio.Queue` binds to the loop in which a caller first has to wait. A converter used from one `asyncio.run` after
        another then raised "bound to a different event loop" in every later loop as soon as a file had to wait
        (Pinned by: test_audit_runtime.py::TestConverterUsedFromSuccessiveEventLoops). Nothing can wait in a closed loop any
        more, so the free slots are moved to a new queue: the number of slots, and with it the worker limit, stays the same.
        Deliberate: a queue bound to a loop that is still open is left alone (`_loop` is a private attribute; without it
        nothing is moved). One converter shared by event loops running at the same time in several threads is not supported.
        """
        bound = getattr(self._free, "_loop", None)
        if bound is not None and bound.is_closed():
            free: asyncio.Queue[int] = asyncio.Queue()
            while not self._free.empty():
                free.put_nowait(self._free.get_nowait())
            self._free = free
        return self._free

    def _replace(self, slot: int) -> None:
        if self._closed:  # `aclose` is stopping every pool itself; a second `shutdown` of the same pool at the same time broke it
            return
        # Do not wait for the manager thread to finish — no reason to delay the failure response. Nobody awaits this future, so
        # its outcome is collected here; otherwise asyncio logs "Future exception was never retrieved" for a failed shutdown.
        _stop(self._pools[slot]).add_done_callback(lambda stopped: stopped.cancelled() or stopped.exception())
        self._pools[slot] = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=1, mp_context=self._context, initializer=_start_worker,
                                   initargs=(self._memory_limit, os.getpid()), max_tasks_per_child=1)


def _worker_context() -> BaseContext:
    if "forkserver" not in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("spawn")
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(["opendockit"])
    return context


def _stop(pool: ProcessPoolExecutor) -> "asyncio.Future[None]":
    """Terminate the workers, and wait for the pool's manager thread to finish outside the server loop (in the default thread pool)."""
    # Deliberate: the public API of Python 3.12 cannot stop a running worker, so the processes are killed directly
    # (`_processes` is a private attribute). The dict is kept by reference: the pool sets its own attribute to None when it
    # shuts down, but the manager thread keeps adding replacement workers to this same dict.
    processes = pool._processes
    _kill_all(processes)
    return asyncio.get_running_loop().run_in_executor(None, _shut_down, pool, processes)


def _kill_all(processes: dict | None) -> None:
    try:
        workers = list((processes or {}).values())
    except RuntimeError:  # the manager thread added or removed a worker meanwhile; the caller comes round again
        return
    for process in workers:
        try:
            process.kill()
        except (OSError, ValueError, AttributeError):  # already gone, already closed, or not started yet
            pass


def _shut_down(pool: ProcessPoolExecutor, processes: dict | None) -> None:
    """Runs in a thread. Keeps killing the pool's workers until its manager thread has finished.

    Killing once is not enough. With `max_tasks_per_child=1` the manager thread starts a replacement worker whenever one
    exits, so a worker can appear after the first kill. On shutdown that worker announces itself through the result queue,
    and if a killed worker died between writing its result and releasing the queue's write lock, the announcement blocks
    forever: the manager thread waits for the worker, `shutdown(wait=True)` waits for the manager thread, and `aclose()`
    never returns. It happened under load in a Linux container (1 run in about 4 of the whole suite in parallel).
    (Pinned by: test_isolated.py::TestIsolatedConverter::test_stopping_does_not_wait_for_a_worker_blocked_on_the_result_queue)

    shutdown(wait=False) is not an alternative: it cleared `_processes` to None while the manager thread was replacing a
    finished worker, and that thread died with a TypeError
    (max_tasks_per_child, Pinned by: test_isolated.py::TestIsolatedConverter::test_stopping_workers_waits_for_the_pool_manager_thread).
    """
    finished = threading.Event()

    def keep_killing() -> None:
        while not finished.wait(0.05):
            _kill_all(processes)

    threading.Thread(target=keep_killing, name="opendockit-stop", daemon=True).start()
    try:
        pool.shutdown(wait=True, cancel_futures=True)
    finally:
        finished.set()


def _start_worker(limit: int | None, server: int) -> None:
    """Runs when a worker starts."""
    _limit_memory(limit)
    if os.name == "posix":   # elsewhere `os.kill(pid, 0)` is not a harmless question
        threading.Thread(target=_leave_with, args=(server,), daemon=True, name="opendockit-server-watch").start()


def _leave_with(server: int) -> None:
    """Ends this worker once the server process is gone.

    A worker waits for work on a queue. When the server is killed outright (SIGKILL, the out-of-memory killer, a supervisor
    that does not wait) nothing closes that queue, so the idle worker waited for ever — and it holds the pipes that keep
    multiprocessing's fork server and resource tracker alive, so all three stayed behind for every server that died
    (Pinned by: test_isolated.py::TestIsolatedConverter::test_worker_does_not_outlive_a_server_that_was_killed).
    """
    while True:
        time.sleep(_SERVER_WATCH_SECONDS)
        try:
            os.kill(server, 0)
        except ProcessLookupError:
            os._exit(0)
        except OSError:   # the pid exists but belongs to someone else now: the server is gone all the same
            os._exit(0)


def _limit_memory(limit: int | None) -> None:
    """Runs when a worker starts. No limit is put on the server process."""
    if limit is None or resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        return  # Deliberate: macOS does not accept RLIMIT_AS. Only the time limit and worker isolation remain


def _run(data: bytes, filename: str | None, options: ConvertOptions | None) -> ConversionResult:
    """Runs in the worker. Out-of-memory is sorted out here — once an exception crosses the process, `__cause__` becomes a string traceback."""
    try:
        return convert(data, filename, options)
    except ConversionError as exc:
        if isinstance(exc.__cause__, MemoryError):
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from None
        raise
    except MemoryError:
        raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from None
