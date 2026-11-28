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
from functools import lru_cache
import multiprocessing
from multiprocessing.context import BaseContext
import os
import socket
import sys
import threading
import time
import types
from typing import TYPE_CHECKING


try:
    import resource
except ImportError:  # not available on Windows, where a job object carries the limit instead (`opendockit._windows`)
    resource = None

if os.name == "nt":
    from opendockit import _windows
else:
    _windows = None

from opendockit.converter import TOO_COMPLEX_MESSAGE, _check_arguments, _content_bytes, _ran_out_of_memory, check_size, convert
from opendockit.exceptions import ConversionError, DocumentTooLargeError
from opendockit.models import ConversionResult, ConvertOptions


if TYPE_CHECKING:
    from opendockit.describe import ImageDescriber


_DEFAULT_MEMORY_LIMIT = 2 * 1024**3
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_OPTIONS = ConvertOptions()


# How often a worker looks whether its server is still there (`_leave_with`).
_SERVER_WATCH_SECONDS = 1.0

class IsolatedConverter:
    """Run `convert` in a worker process so one file's runaway memory or time does not spread to the server.

    - `workers`: number of files converted at the same time. Each worker has its own pool — so killing a worker that ran over
      time does not cut off other files. Plan for up to `workers × memory_limit` of memory.
    - `memory_limit`: memory limit of one worker (bytes). On Linux it is the address-space limit (`RLIMIT_AS`), on Windows the
      job object's `ProcessMemoryLimit`; macOS accepts neither, so there only the time limit and process isolation remain.
      None means no limit.
    - `timeout`: time limit of one conversion (seconds). When it is exceeded the worker is actually terminated. Image description
      (`describer`) is network waiting, so it is not counted in this time.

    Workers are started with forkserver (spawn on platforms without it, and where gevent or eventlet would make the
    forkserver die: macOS and the BSDs with their sockets patched, see `_forkserver_can_die`). The server process has asyncio
    and DB-driver threads running, so a fork could leave the child stuck holding a lock. This package is preloaded into the
    forkserver, so starting a new worker per file takes only a few ms (around 0.2 s without preloading). A new worker per
    file also means no memory is left behind after a large document. Known accepted gap: the forkserver preload list is one
    per process, so this overwrites a list set by other code in the same process (it does not apply to a forkserver that is
    already running).

    On Windows there is no forkserver and no preloading, so every file pays a full interpreter start and `import opendockit`
    (measured on Windows 11: 0.2 s for a small text file on Python 3.12 and 0.3 s on 3.14, then the import of the reader
    its format needs; a few ms where the forkserver is preloaded). It is the price of a fresh worker per file there, and
    `timeout` counts it. For the same reason a program that builds an `IsolatedConverter` on a spawn platform must do so
    under `if __name__ == "__main__":`, or the child process re-runs the module and builds converters without end.
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
        self._job_name, self._job = _new_job(memory_limit)
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
        (`TOO_COMPLEX_MESSAGE`). Any other failure is the same `ConversionError` subclass `convert` raises, or its `ImportError`
        for a library that cannot be imported in the worker. An argument type
        error is a `TypeError`, and a file over `max_file_bytes` or an empty one the same error `convert` raises, before
        anything is sent to the worker.
        """
        try:
            data = _content_bytes(data)  # a memoryview cannot be sent to a worker (not picklable)
        except MemoryError as exc:
            # The copy, as in `convert` and `aconvert`: the raw MemoryError left the server entry point, where memory is the
            # most likely to run short (Pinned by: test_converter.py::TestRunningOutOfMemory::test_while_copying_the_content).
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from exc
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
            # test_audit_runtime.py::TestConverterBuiltBeforeAFork). The job is built again with them: the one made before
            # the fork belongs to the master, and its workers would be ended when the master ends, not when this process does.
            if _windows is not None:
                _windows.close_job(self._job)   # the inherited handle, or this process would hold it to its own end
            self._job_name, self._job = _new_job(self._memory_limit)
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
        try:
            await asyncio.gather(*(_stop(pool) for pool in self._pools))
        finally:
            # Last: closing the job ends anything of ours the pools did not (Windows). Before them it would end workers the
            # pool's manager thread is still waiting for.
            if _windows is not None:
                _windows.close_job(self._job)
                self._job = None

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
        pool = ProcessPoolExecutor(max_workers=1, mp_context=self._context, initializer=_start_worker,
                                   initargs=(self._memory_limit, os.getpid(), self._job_name), max_tasks_per_child=1)
        # The queue the files are sent on must never be waited for. A worker that dies before it has read its file leaves
        # the queue's feeder thread blocked writing to a pipe nobody drains, and the pool's manager thread joins that feeder
        # while breaking the pool: `aclose()` then never returned. Windows loses this race where Linux usually wins it,
        # because a worker there is ready in milliseconds and a spawned one took 0.66 s when this was found (0.2 s since the
        # readers are imported on first use) — long enough for a large file to still be on its way. Cancelled here rather
        # than when stopping: by then the manager thread is already in the join.
        # The feeder is a daemon thread and the file it was carrying is one we are giving up on anyway
        # (Pinned by: test_isolated.py::TestIsolatedConverter::test_worker_that_dies_before_it_reads_its_file_does_not_hang_the_close).
        try:
            pool._call_queue.cancel_join_thread()
        except (AttributeError, OSError):   # a Python whose pool does not have this queue: the hang is the old behavior
            pass
        return pool


def _worker_context() -> BaseContext:
    # A Windows server without a console starts its workers without a window: started as spawn starts them, every worker of
    # such a server got a console of its own and opened a window on the desktop, one for every file (Pinned by:
    # test_isolated.py::TestIsolatedConverter::test_a_server_without_a_console_starts_its_workers_without_a_window). A
    # server with a console is left as it was: its workers share it, its Ctrl+C reaches them and what they print is seen.
    if _windows is not None and not _windows.has_console() and _windowless_popen() is not None:
        return _WindowlessSpawnContext()
    # Where the forkserver would die, every worker is spawned instead: a fresh interpreter per file, about 0.1 s against a few
    # ms, and no conversion lost (Pinned by: test_isolated.py::TestStartMethod).
    if "forkserver" not in multiprocessing.get_all_start_methods() or _forkserver_can_die():
        return multiprocessing.get_context("spawn")
    context = multiprocessing.get_context("forkserver")
    # The extractors are imported on first use (`opendockit.extractors`); preloaded here, a new worker still starts with all of
    # them loaded (Pinned by: test_converter.py::TestExtractorsImportedOnFirstUse::test_forkserver_preloads_every_extractor)
    context.set_forkserver_preload(["opendockit", "opendockit.extractors.all"])
    return context


if os.name == "nt":
    import _winapi

    class _WithoutAWindow:
        """`_winapi` as the spawn code of `_windowless_popen` sees it: `CreateProcess` adds CREATE_NO_WINDOW to the flags it
        is given, and every other name is the module's own."""

        def __getattr__(self, name: str) -> object:
            return getattr(_winapi, name)

        @staticmethod
        def CreateProcess(*arguments: object) -> object:
            # Application, command line, process and thread attributes, inherit handles, creation flags, environment,
            # folder, startup information: a call of another shape is passed on as it was made.
            if len(arguments) == 9 and isinstance(arguments[5], int):
                arguments = (*arguments[:5], arguments[5] | _winapi.CREATE_NO_WINDOW, *arguments[6:])
            return _winapi.CreateProcess(*arguments)

    @lru_cache(maxsize=1)
    def _windowless_popen() -> type | None:
        """The `Popen` of spawn, starting the worker with CREATE_NO_WINDOW. None where that `Popen` is not shaped as this
        expects, and the workers are started as before.

        A console program started by a process without a console gets a console of its own, which opens a window unless
        CREATE_NO_WINDOW says it is to have none; spawn starts its workers with no flags at all, and has no way to be asked
        for one. Deliberate: CPython's own `__init__` is run, with `_winapi` in its globals standing for `_WithoutAWindow`
        — the way it starts a worker differs between versions (3.14 passes a `STARTUPINFO`, 3.12 none), and a copy of it
        would follow neither. Nothing global changes: another thread starting a process at the same moment calls the
        real `_winapi`. Should a later Python start its workers elsewhere than in this `__init__`, the stand-in is not
        called and the workers have their window again, which the pinning test of `_worker_context` says.
        """
        from multiprocessing import popen_spawn_win32

        start = popen_spawn_win32.Popen.__init__
        if not {"_winapi", "CreateProcess"} <= set(start.__code__.co_names):
            return None
        names = {**vars(popen_spawn_win32), "_winapi": _WithoutAWindow()}
        windowless = types.FunctionType(start.__code__, names, start.__name__, start.__defaults__, start.__closure__)
        windowless.__kwdefaults__ = start.__kwdefaults__
        return type("_WindowlessPopen", (popen_spawn_win32.Popen,), {"__init__": windowless, "__module__": __name__})

    class _WindowlessSpawnProcess(multiprocessing.context.SpawnProcess):
        """A spawned worker started without a window (`_windowless_popen`). A class of this module, not of a function: the
        process object is sent to the worker, which finds its class by name."""

        @staticmethod
        def _Popen(process_obj: object) -> object:
            return _windowless_popen()(process_obj)

    class _WindowlessSpawnContext(multiprocessing.context.SpawnContext):
        Process = _WindowlessSpawnProcess


def _forkserver_can_die() -> bool:
    """Whether a forkserver started from this process would die the first time a worker is asked for before the request has
    arrived: its listening socket would be non-blocking, on a system that passes that on to the sockets accepted from it.

    gevent and eventlet replace `socket.socket` with a class that makes every socket non-blocking, and multiprocessing makes
    the forkserver's listening socket with whatever class `socket.socket` is. The forkserver takes the socket it is handed
    for a blocking one. On macOS and the BSDs a socket accepted from a non-blocking one is non-blocking too (Linux does not
    pass the flag on, accept(2)), so the forkserver read a request that had not arrived yet, got BlockingIOError and died:
    the conversion in progress failed with BrokenPipeError and the next with ConnectionRefusedError. Measured on macOS with
    two Dify requests at once, 2 runs in 100 under load; Python 3.12 to 3.15 read the request the same way.
    Asked of a socket made the way multiprocessing makes it rather than of gevent, so any library that does the same is
    covered and one that keeps its sockets blocking is not. On Linux nothing is asked: its forkserver is not at risk.
    """
    if sys.platform in ("linux", "android"):
        return False
    try:
        with socket.socket(socket.AF_UNIX) as probe:
            return not os.get_blocking(probe.fileno())
    except (AttributeError, OSError):   # no Unix sockets: the forkserver could not start either, and fails as it did
        return False


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


def _new_job(memory_limit: int | None) -> "tuple[str | None, int | None]":
    """The job this converter's workers join, and its name to hand them. `(None, None)` anywhere but Windows."""
    if _windows is None:
        return None, None
    name = _windows.new_job_name()
    # A process may leave it when it asks: the guard of a LibreOffice conversion does, so that a server killed outright,
    # which ends everything in this job, does not end the guard too and leave the uploaded document on disk
    # (Pinned by: test_legacy.py::TestRunner::test_a_server_killed_outright_leaves_no_folder_windows).
    job = _windows.open_job(name, memory_limit, lets_go=True)
    return (name, job) if job is not None else (None, None)   # no job, no name to join: the workers simply have no limit


def _start_worker(limit: int | None, server: int, job_name: str | None) -> None:
    """Runs when a worker starts."""
    if _windows is not None:
        if job_name is not None:
            _windows.join_job(job_name)   # the memory limit, in place of RLIMIT_AS
        threading.Thread(target=_leave_with_windows, args=(server,), daemon=True, name="opendockit-server-watch").start()
        return
    _limit_memory(limit)
    if os.name == "posix":   # elsewhere `os.kill(pid, 0)` is not a harmless question
        threading.Thread(target=_leave_with, args=(server,), daemon=True, name="opendockit-server-watch").start()


def _leave_with_windows(server: int) -> None:
    """Ends this worker once the server process is gone (Windows).

    What `_leave_with` does on POSIX, without its two weaknesses: the server's handle is taken once and held, so the answer
    can never be about a pid that has since been given to another program, and the wait is a wait rather than a question
    asked once a second. The job object the worker is in ends it as well, and either alone was measured to do so at once;
    this one is what works where no job could be made
    (Pinned by: test_isolated.py::TestIsolatedConverter::test_worker_does_not_outlive_a_server_that_was_killed_windows).
    """
    if _windows.wait_for_process(server):
        os._exit(0)


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
    """Runs in the worker. Out-of-memory is sorted out here — once an exception crosses the process, `__cause__` becomes a string traceback.

    A MemoryError anywhere in the chain of causes and contexts counts, not only the direct cause: a parser that wraps it in
    its own exception, which the converter wraps again, was reported as a corrupted document
    (Pinned by: test_image.py::TestOutOfMemoryWhileReadingAPicture::test_memory_error_deeper_in_the_chain_is_too_complex).
    `convert` reports it the same way itself; this stays for what is raised around it in the worker.
    """
    try:
        return convert(data, filename, options)
    except ConversionError as exc:
        if _ran_out_of_memory(exc):
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from None
        raise
    except MemoryError:
        raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from None
