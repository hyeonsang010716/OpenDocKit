"""Audited runtime defects in picture decoding (`image.py`) and in the isolated converter (`isolated.py`): each test is the
input that showed the defect, built in memory."""

import asyncio
import concurrent.futures
import gc
from io import BytesIO
import os
import subprocess
import sys
import time

from PIL import Image, UnidentifiedImageError, features
import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, IsolatedConverter, convert, isolated
from opendockit.image import (
    _OPEN_FORMATS,
    HEIF_AVAILABLE,
    ImageTooLarge,
    ImageUnreadable,
    prepare_image,
    split_tiff_pages,
)


def _jpeg(width: int, height: int, *, progressive: bool) -> bytes:
    buffer = BytesIO()
    Image.new("L", (width, height), 128).save(buffer, format="JPEG", progressive=progressive)
    return buffer.getvalue()


class TestProgressiveJpegOverThePixelLimit:
    """A JPEG over the decode pixel limit is read reduced (draft), and only the reduced size was checked against the limit. A
    progressive JPEG keeps the coefficients of the whole picture in memory whatever the reduction: a 20000×20000 one (1.5 MB on
    disk) peaked at 946 MB where a baseline one took 148 MB. The tests lower the limit to take the same path with small pictures."""

    @pytest.fixture(autouse=True)
    def _small_pixel_limit(self, monkeypatch):
        monkeypatch.setattr("opendockit.image._MAX_DECODE_PIXELS", 1_000_000)

    def test_progressive_jpeg_over_the_limit_is_too_large(self):
        data = _jpeg(3200, 2400, progressive=True)  # 7.68 million pixels; 480 thousand at 1/4, which is under the limit

        with pytest.raises(ImageTooLarge):
            prepare_image(data, min_side=1, max_side=512)

    def test_uploaded_progressive_jpeg_over_the_limit_is_a_document_too_large(self):
        with pytest.raises(DocumentTooLargeError) as error:
            convert(_jpeg(3200, 2400, progressive=True), "scan.jpg")

        assert "The image is too large" in str(error.value)

    def test_baseline_jpeg_over_the_limit_is_still_read_reduced(self):
        image = prepare_image(_jpeg(3200, 2400, progressive=False), min_side=1, max_side=512)

        assert (image.width, image.height) == (512, 384)

    def test_progressive_jpeg_within_the_limit_is_read(self):
        result = convert(_jpeg(800, 600, progressive=True), "photo.jpg", options=ConvertOptions(image_max_side=512))

        assert [(image.width, image.height) for image in result.images] == [(512, 384)]


_EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\nshowpage\n%%EOF\n"


def _picture(image_format: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, format=image_format)
    return buffer.getvalue()


class TestPictureFormatAllowList:
    """`Image.open` tried every Pillow plugin on a picture taken out of a document. An EPS reached the PostScript plugin, which
    starts Ghostscript to draw it: an external interpreter run on untrusted input."""

    def test_eps_is_refused_without_reaching_the_postscript_plugin(self, monkeypatch):
        Image.init()
        factory, accept = Image.OPEN["EPS"]
        reached = []

        def watched(*arguments, **keywords):
            reached.append("EPS")
            return factory(*arguments, **keywords)

        monkeypatch.setitem(Image.OPEN, "EPS", (watched, accept))

        with pytest.raises(ImageUnreadable) as error:
            prepare_image(_EPS, min_side=1, max_side=2000)

        assert reached == []
        assert isinstance(error.value.__cause__, UnidentifiedImageError)

    def test_no_format_that_runs_another_program_or_is_not_a_picture_is_allowed(self):
        assert not {"EPS", "WMF", "FPX", "MIC", "MPEG", "BUFR", "GRIB", "HDF5", "FITS"} & set(_OPEN_FORMATS)

    def test_every_allowed_format_is_one_pillow_can_open(self):
        Image.init()

        assert set(_OPEN_FORMATS) <= set(Image.OPEN)  # a name Pillow does not know is a KeyError in `Image.open`

    @pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "AVIF", "ICO", "PCX", "PPM", "MPO"])
    def test_supported_format_is_still_read(self, image_format):
        image = prepare_image(_picture(image_format), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    @pytest.mark.skipif(not features.check_codec("jpg_2000"), reason="this Pillow build has no JPEG 2000 codec")
    def test_jpeg_2000_is_still_read(self):
        image = prepare_image(_picture("JPEG2000"), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    @pytest.mark.skipif(not HEIF_AVAILABLE, reason="the optional heic extra is not installed")
    def test_heic_is_still_read_with_the_optional_extra(self):
        image = prepare_image(_picture("HEIF"), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    def test_multi_page_tiff_is_still_split(self):
        buffer = BytesIO()
        pages = [Image.new("L", (64, 48), shade) for shade in (0, 255)]
        pages[0].save(buffer, format="TIFF", save_all=True, append_images=pages[1:])

        assert len(split_tiff_pages(buffer.getvalue()).pages) == 2


_CLOSED = "This IsolatedConverter is closed."
_SMALL_TEXT = b"first line\n\nsecond paragraph\n"
# A CSV (8MB) that takes seconds to convert: still running when the converter is closed
_SLOW_CSV = ("가나다 라마바 사아자 1234,5678\n" * 200_000).encode()
# Tests that start worker processes wait on them: marked `timing` (run serially), and POSIX only (see tests/test_isolated.py)
_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="worker process control is tested on POSIX only")


async def _wait_for_worker(converter: IsolatedConverter) -> None:
    for _ in range(200):
        if any(pool._processes for pool in converter._pools):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the worker did not start")


class _PoolWithoutWorkers:
    """Stands for a pool whose worker has not taken the file yet. `shutdown(cancel_futures=True)` cancels such a call."""

    _processes = None

    def __init__(self, *, cancels_on_shutdown: bool) -> None:
        self.calls: list[concurrent.futures.Future] = []
        self._cancels_on_shutdown = cancels_on_shutdown

    def submit(self, *_: object) -> concurrent.futures.Future:
        self.calls.append(concurrent.futures.Future())
        return self.calls[-1]

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        if self._cancels_on_shutdown and cancel_futures:
            for call in self.calls:
                call.cancel()


async def _converter_with(pool: _PoolWithoutWorkers) -> tuple[IsolatedConverter, asyncio.Task]:
    """A converter whose only pool is `pool`, and a conversion that has reached that pool."""
    converter = IsolatedConverter()
    for unused in converter._pools:
        unused.shutdown()
    converter._pools = [pool]
    task = asyncio.create_task(converter.convert(_SMALL_TEXT, "a.txt"))
    while not pool.calls:
        await asyncio.sleep(0)
    return converter, task


class TestClosingWithConversionsUnderWay:
    """`aclose()` while one file was being converted and another waited for the worker: both ended as "too large or too complex"
    (a shutdown is not the document's fault), the waiting one was sent to the pool that had just been shut down, and the
    running one shut its pool down a second time while `aclose` was doing so (1 run in 6 logged "Future exception was never
    retrieved ... Bad file descriptor")."""

    @pytest.mark.timing
    @_posix_only
    def test_running_and_waiting_files_end_as_closed_and_each_pool_is_stopped_once(self, monkeypatch):
        stopped, logged = [], []
        stop = isolated._stop
        monkeypatch.setattr(isolated, "_stop", lambda pool: (stopped.append(pool), stop(pool))[1])

        async def scenario():
            asyncio.get_running_loop().set_exception_handler(lambda _, context: logged.append(context["message"]))
            converter = IsolatedConverter(workers=1)
            running = asyncio.create_task(converter.convert(_SLOW_CSV, "a.csv", ConvertOptions(max_chars=None)))
            await _wait_for_worker(converter)
            waiting = asyncio.create_task(converter.convert(_SMALL_TEXT, "b.txt"))
            await asyncio.sleep(0.1)
            pools = list(converter._pools)
            await converter.aclose()
            outcomes = await asyncio.gather(running, waiting, return_exceptions=True)
            await asyncio.sleep(0.2)  # a shutdown started by the failed conversion would still be running here
            return outcomes, pools

        outcomes, pools = asyncio.run(scenario())
        gc.collect()  # an exception nobody retrieved is logged when its future is collected

        assert [(type(outcome), str(outcome)) for outcome in outcomes] == [(RuntimeError, _CLOSED)] * 2
        assert stopped == pools
        assert logged == []

    def test_file_the_worker_had_not_taken_yet_ends_as_closed_not_as_a_cancelled_request(self):
        async def scenario():
            converter, task = await _converter_with(_PoolWithoutWorkers(cancels_on_shutdown=True))
            await converter.aclose()
            return (await asyncio.gather(task, return_exceptions=True))[0]

        outcome = asyncio.run(scenario())

        assert (type(outcome), str(outcome)) == (RuntimeError, _CLOSED)

    def test_request_cancelled_after_closing_is_still_a_cancelled_request(self):
        async def scenario():
            converter, task = await _converter_with(_PoolWithoutWorkers(cancels_on_shutdown=False))
            await converter.aclose()
            task.cancel()
            return (await asyncio.gather(task, return_exceptions=True))[0]

        assert isinstance(asyncio.run(scenario()), asyncio.CancelledError)

    def test_request_cancelled_while_open_still_gets_a_new_pool(self):
        async def scenario():
            pool = _PoolWithoutWorkers(cancels_on_shutdown=False)
            converter, task = await _converter_with(pool)
            task.cancel()
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
            replaced = converter._pools[0] is not pool
            await converter.aclose()
            return outcome, replaced

        outcome, replaced = asyncio.run(scenario())

        assert isinstance(outcome, asyncio.CancelledError) and replaced


class TestConverterUsedFromSuccessiveEventLoops:
    """The queue of free workers binds to the first event loop in which a file has to wait. A converter kept across several
    `asyncio.run` calls (a job runner, a test suite) then failed in every later loop as soon as a file had to wait:
    "RuntimeError: <Queue ...> is bound to a different event loop"."""

    @pytest.mark.timing
    @_posix_only
    def test_files_wait_for_a_worker_in_a_later_event_loop_too(self):
        converter = IsolatedConverter(workers=2)

        async def three_files():
            files = (converter.convert(_SMALL_TEXT, name) for name in ("a.txt", "b.txt", "c.txt"))
            return await asyncio.gather(*files, return_exceptions=True)

        try:
            outcomes = [asyncio.run(three_files()) for _ in range(3)]
            free = sorted(converter._free.get_nowait() for _ in range(converter._free.qsize()))
        finally:
            asyncio.run(converter.aclose())

        assert [[getattr(outcome, "text", outcome) for outcome in loop] for loop in outcomes] == [[convert(_SMALL_TEXT, "a.txt").text] * 3] * 3
        assert free == [0, 1]  # every worker came back, none twice: the limit is still two files at a time


class TestConverterBuiltBeforeAFork:
    """A converter built in a master process that then forks its servers (gunicorn --preload) told its workers to watch the
    master: the pid was taken when the converter was built. A server killed outright then left its idle worker behind for as
    long as the master lived — the leak the watch exists to prevent."""

    @pytest.mark.timing
    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
    def test_worker_leaves_with_the_forked_server_that_uses_it_not_with_the_master(self, tmp_path):
        script = ("import asyncio, os, signal, time\n"
                  "from opendockit import IsolatedConverter\n"
                  "async def serve(converter):\n"
                  "    await converter.convert(b'hello', 'a.txt')\n"
                  "    await asyncio.sleep(0.5)\n"   # the worker for the next file is up and idle
                  "    print(*converter._pools[0]._processes, flush=True)\n"
                  "    os.kill(os.getpid(), signal.SIGKILL)\n"
                  "if __name__ == '__main__':\n"
                  "    converter = IsolatedConverter(workers=1)\n"
                  "    server = os.fork()\n"
                  "    if server == 0:\n"
                  "        asyncio.run(serve(converter))\n"
                  "    os.waitpid(server, 0)\n"
                  "    time.sleep(60)\n")   # the master outlives its server
        output = tmp_path / "pids.txt"   # a file, not a pipe: an orphan would hold a pipe open and the test would hang on it
        with output.open("w") as stdout:
            master = subprocess.Popen([sys.executable, "-c", script], stdout=stdout, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while not output.read_text().split() and time.monotonic() < deadline:
                time.sleep(0.1)
            workers = [int(pid) for pid in output.read_text().split()]
            assert workers, "the forked server did not report its idle worker"

            deadline = time.monotonic() + 10
            while any(_exists(pid) for pid in workers) and time.monotonic() < deadline:
                time.sleep(0.2)
            left_behind = [pid for pid in workers if _exists(pid)]
        finally:
            master.kill()   # the workers of a failed run watch the master: they leave with it
            master.wait()

        assert left_behind == []


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
