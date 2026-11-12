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
from functools import partial
import multiprocessing
from multiprocessing.context import BaseContext
from typing import TYPE_CHECKING


try:
    import resource
except ImportError:  # not available on Windows. Only the time limit applies, without a memory limit
    resource = None

from opendockit.converter import _check_arguments, _content_bytes, convert
from opendockit.exceptions import ConversionError, DocumentTooLargeError
from opendockit.models import ConversionResult, ConvertOptions


if TYPE_CHECKING:
    from opendockit.describe import ImageDescriber


TOO_COMPLEX_MESSAGE = "The document is too large or too complex to convert."
_DEFAULT_MEMORY_LIMIT = 2 * 1024**3
_DEFAULT_TIMEOUT = 120.0


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
        error is a `TypeError` raised before anything is sent to the worker.
        """
        data = _content_bytes(data)  # a memoryview cannot be sent to a worker (not picklable)
        _check_arguments(filename, options)
        if self._closed:
            raise RuntimeError("This IsolatedConverter is closed.")

        result = await self._convert_in_worker(data, filename, options)
        if describer is None or not result.images:
            return result

        from opendockit.describe import describe_images

        return await describe_images(result, describer, max_concurrency=max_concurrency)

    async def aclose(self) -> None:
        """Terminate every worker. Files being converted are cut off (call this at application shutdown)."""
        self._closed = True
        await asyncio.gather(*(_stop(pool) for pool in self._pools))

    async def __aenter__(self) -> "IsolatedConverter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _convert_in_worker(self, data: bytes, filename: str | None, options: ConvertOptions | None) -> ConversionResult:
        slot = await self._free.get()
        try:
            future = asyncio.get_running_loop().run_in_executor(self._pools[slot], _run, data, filename, options)
            return await asyncio.wait_for(future, self._timeout)
        except (TimeoutError, BrokenProcessPool) as exc:
            self._replace(slot)
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from exc
        except asyncio.CancelledError:
            # The worker keeps running even after the request is dropped. Left alone, the next file would queue behind it
            self._replace(slot)
            raise
        except MemoryError as exc:  # the conversion finished, but the server ran out of memory unpickling the result
            raise DocumentTooLargeError(TOO_COMPLEX_MESSAGE) from exc
        finally:
            self._free.put_nowait(slot)

    def _replace(self, slot: int) -> None:
        _stop(self._pools[slot])  # do not wait for the manager thread to finish — no reason to delay the failure response
        if not self._closed:
            self._pools[slot] = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=1, mp_context=self._context, initializer=_limit_memory,
                                   initargs=(self._memory_limit,), max_tasks_per_child=1)


def _worker_context() -> BaseContext:
    if "forkserver" not in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("spawn")
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(["opendockit"])
    return context


def _stop(pool: ProcessPoolExecutor) -> "asyncio.Future[None]":
    """Terminate the workers, and wait for the pool's manager thread to finish outside the server loop (in the default thread pool)."""
    # Deliberate: the public API of Python 3.12 cannot stop a running worker, so the processes are killed directly
    # (`_processes` is a private attribute).
    for process in list((pool._processes or {}).values()):
        process.kill()

    # shutdown(wait=False) cleared `_processes` to None while the manager thread was replacing a finished worker, and that thread
    # died with a TypeError
    # (max_tasks_per_child, Pinned by: test_isolated.py::TestIsolatedConverter::test_stopping_workers_waits_for_the_pool_manager_thread)
    return asyncio.get_running_loop().run_in_executor(None, partial(pool.shutdown, wait=True, cancel_futures=True))


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
