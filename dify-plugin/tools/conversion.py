"""The conversion behind the tool. It does not import the Dify SDK, so the OpenDocKit test suite can run it as it is."""

import asyncio
from dataclasses import dataclass
import logging
import threading
from typing import Any

from opendockit import ConvertOptions, IsolatedConverter, convert
from opendockit.integrations._documents import pieces


_LOG = logging.getLogger(__name__)

# The tool returns text, so pictures are not extracted: they would only cost memory on the way out of the worker.
OPTIONS = ConvertOptions(extract_images=False)

NO_TEXT = "No text could be extracted from this file."
# OpenDocKit's own warning for the same thing; the error starts with NO_TEXT already, so it is not repeated after it.
_LIBRARY_NO_TEXT = "No text was extracted."

# Dify serves the requests of a plugin side by side in one thread (gevent), and asyncio allows one running loop per thread:
# a second conversion started while one was running failed with "asyncio.run() cannot be called from a running event
# loop". They take turns instead. The worker converts one file at a time anyway, and one takes seconds
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


def convert_upload(data: bytes, filename: str) -> Converted:
    """Convert one uploaded file. Every error is a fixed English sentence that never quotes the document."""
    with _ONE_AT_A_TIME:
        try:
            result = asyncio.run(_converted(data, filename))
        except (OSError, NotImplementedError):
            # A runtime that cannot start a process (a serverless sandbox without shared memory) still converts the file:
            # here, without the worker's limits. A file that fails to convert raises ConversionError, which is not caught
            # (Pinned by: test_dify_plugin.py::TestConversion::test_a_runtime_that_cannot_start_a_worker_still_converts).
            _LOG.warning("No worker process could be started; the file is converted in the plugin process, without limits.")
            result = convert(data, filename, OPTIONS)
    documents = [{"page_content": text, "metadata": metadata} for text, metadata in pieces(result, filename, "sections")]
    if not documents:
        # The warnings say why: a scanned page that needs OCR, an empty sheet. Raised, so the node fails and an error
        # branch can take it, where text would reach the LLM as if it were the document
        # (Pinned by: test_dify_plugin.py::TestConversion::test_a_file_without_text_fails_with_the_reason_said_once).
        raise NoTextError(" ".join([NO_TEXT, *(warning for warning in result.warnings if warning != _LIBRARY_NO_TEXT)]))
    return Converted(result.text, documents, list(result.warnings))


async def _converted(data: bytes, filename: str):
    # A worker process of its own, with a memory and a time limit: a file built to expand to gigabytes, or to run for
    # minutes, ends only that worker, not the plugin runtime the other plugins of this Dify share.
    async with IsolatedConverter(workers=1) as converter:
        return await converter.convert(data, filename, OPTIONS)
