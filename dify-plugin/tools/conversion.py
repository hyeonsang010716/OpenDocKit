"""The conversion behind the tool. It does not import the Dify SDK, so the OpenDocKit test suite can run it as it is."""

import asyncio
from dataclasses import dataclass
import logging
import threading
from typing import Any

from opendockit import ConversionResult, ConvertOptions, IsolatedConverter, convert
from opendockit.integrations._documents import pieces


_LOG = logging.getLogger(__name__)
# The integrations log a file that gave no text together with its warnings, and a warning can name a part of the
# document (a hidden sheet, an attachment). Nothing of the document goes to the plugin runtime's log (PRIVACY.md)
# (Pinned by: test_dify_plugin.py::TestConversion::test_a_file_without_text_fails_without_quoting_the_document).
logging.getLogger("opendockit.integrations").setLevel(logging.ERROR)

# The tool returns text, so no picture is decoded: pictures would only cost memory on the way out of the worker. Each one
# keeps its placeholder, so the alt text an author gave a picture stays in the text (`[Image: …]`), which
# `extract_images=False` dropped (Pinned by: test_dify_plugin.py::TestConversion::test_a_picture_keeps_its_alt_text).
OPTIONS = ConvertOptions(decode_images=False)

# A fixed sentence: the warnings that say why can quote the document (a hidden sheet's name), and errors never do.
NO_TEXT = ("No text could be extracted from this file. It may be empty, or hold only scanned pages or pictures, which need "
           "OCR.")

# Dify serves the requests of a plugin side by side in one thread (gevent), and asyncio allows one running loop per thread:
# a second conversion started while one was running failed with "asyncio.run() cannot be called from a running event
# loop". Conversions take turns instead, each in a worker of its own
# (Pinned by: test_dify_plugin.py::TestConversion::test_two_requests_at_once_in_one_thread_both_finish).
_ONE_AT_A_TIME = threading.Lock()


class NoTextError(ValueError):
    """The file was read, but it holds no text."""


@dataclass(frozen=True)
class Converted:
    text: str
    # One per page, slide or sheet that has text, or one for the whole document when it has no such units.
    documents: list[dict[str, Any]]
    warnings: list[str]


def upload_name(filename: str | None, extension: str | None) -> str:
    """The name to convert a Dify upload under. Most formats are told by their content, but CSV only by its extension, so
    a name that does not end with the extension Dify recorded for the file gets it
    (Pinned by: test_dify_plugin.py::TestUploadName)."""
    name = filename or "document"
    if extension:
        suffix = extension if extension.startswith(".") else f".{extension}"
        if not name.lower().endswith(suffix.lower()):
            name += suffix
    return name


def convert_upload(data: bytes, filename: str) -> Converted:
    """Convert one uploaded file. Every error is a fixed sentence that never quotes the document."""
    with _ONE_AT_A_TIME:
        result = _converted(data, filename)
    documents = [{"page_content": text, "metadata": metadata} for text, metadata in pieces(result, filename, "sections")]
    if not documents:
        # Raised, so the node fails and an error branch can take it, where text would reach the LLM as if it were the
        # document (Pinned by: test_dify_plugin.py::TestConversion::test_a_file_without_text_fails_without_quoting_the_document).
        raise NoTextError(NO_TEXT)
    return Converted(result.text, documents, list(result.warnings))


def _converted(data: bytes, filename: str) -> ConversionResult:
    try:
        converter = IsolatedConverter(workers=1)
    except (OSError, NotImplementedError):
        # A runtime that cannot make a worker pool at all (a serverless sandbox without the shared memory multiprocessing
        # needs) still converts the file: here, without the worker's limits. Only making the pool is covered. A worker
        # that cannot be started later (no memory, no process slots left) is a machine running short, and then the file
        # must not be converted inside the runtime every plugin of this Dify shares, without limits: that fails the node
        # (Pinned by: test_dify_plugin.py::TestConversion::test_a_runtime_that_cannot_make_a_worker_pool_still_converts,
        # test_a_worker_that_cannot_start_fails_the_file_instead_of_converting_it_here).
        _LOG.warning("No worker pool could be made; the file is converted in the plugin process, without limits.")
        return convert(data, filename, OPTIONS)
    return asyncio.run(_in_worker(converter, data, filename))


async def _in_worker(converter: IsolatedConverter, data: bytes, filename: str) -> ConversionResult:
    # A worker process of its own, with a memory and a time limit: a file built to expand to gigabytes, or to run for
    # minutes, ends only that worker, not the plugin runtime the other plugins of this Dify share.
    async with converter:
        return await converter.convert(data, filename, OPTIONS)
