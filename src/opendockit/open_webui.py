"""Open WebUI document loader: a small HTTP server Open WebUI sends uploaded files to, which answers with their Markdown.

    pip install "opendockit[open-webui]"
    OPENDOCKIT_OPEN_WEBUI_API_KEY=$(openssl rand -hex 32) opendockit-open-webui

In Open WebUI, Admin Settings > Documents: Content Extraction Engine "External", the Document Loader URL of this server
(`http://localhost:8765`) and the same API key. Open WebUI then sends every uploaded file as `PUT /process` (the file's bytes
as the body, its name in `X-Filename`) and indexes what comes back: one document per page, slide or sheet, or one for a
document without such units (the pieces of the other adapters, `integrations._documents.pieces`).

Conversion is offline (no vision model, nothing fetched) and runs in worker processes with a memory and a time limit
(`IsolatedConverter`); pictures keep their placeholders, alt text included. Every error is a fixed sentence: Open WebUI shows
the body of a failed answer to the user as it is.
"""

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac
import logging
import mimetypes
import os
import re
from typing import Any
from urllib.parse import unquote

from opendockit import __version__
from opendockit.converter import _size_text, check_size, convert
from opendockit.exceptions import ConversionError
from opendockit.integrations._documents import pieces
from opendockit.isolated import IsolatedConverter
from opendockit.models import ConversionResult, ConvertOptions, SectionKind


_NEEDS_EXTRA = 'The OpenDocKit loader for Open WebUI needs Starlette and Uvicorn: pip install "opendockit[open-webui]"'
try:
    from starlette.applications import Starlette
    from starlette.requests import ClientDisconnect, Request
    from starlette.responses import JSONResponse, PlainTextResponse, Response
    from starlette.routing import Route
    import uvicorn
except ImportError:  # the command exists in every install; without the extra it says what to install
    Starlette = None


__all__ = ["create_app", "main"]

API_KEY_VARIABLE = "OPENDOCKIT_OPEN_WEBUI_API_KEY"
# A key shorter than this is refused at start: the server converts whatever a caller who knows the key sends it, and a short
# key on a port other machines can reach is guessed (Pinned by: test_open_webui.py::TestCommandLine::test_short_key_is_refused).
_MIN_KEY_LENGTH = 16
DEFAULT_PORT = 8765
# A document past this many characters is refused, not cut. The limit is far above real documents (a 237-page Supreme Court
# opinion is 500,000) and is there for the server: a result comes back from its worker into this process whole, and is
# held twice more while it is written as JSON; a workbook of millions of filled cells must not bring gigabytes of it
# (Pinned by: test_open_webui.py::TestProcess::test_document_over_the_character_limit_is_refused).
DEFAULT_MAX_CHARS = 10_000_000
DEFAULT_MAX_FILE_MB = 50

# Open WebUI names a stored upload '<file id>_<original name>' and sends that in `X-Filename`; the id is not part of the name
# the document was given (Pinned by: test_open_webui.py::TestUploadName).
_STORED_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_(?=.)", re.IGNORECASE)

# Fixed sentences only: Open WebUI shows the body of a failed answer to the user, and nothing of the document or of a parser
# error may be in it (core.md).
UNAUTHORIZED = "The API key is missing or wrong. Set the same key in Open WebUI (Documents > External) and on this server."
BUSY = "The converter is busy with other files. Upload this one again in a moment."
NO_TEXT = ("No text could be extracted from this file. It may be empty, or hold only scanned pages or pictures, which need "
           "OCR.")
TOO_LONG = "The document is longer than the character limit of this server (--max-chars)."
FAILED = "The file could not be converted."

# The integrations log a file that gave no text together with its warnings, and a warning can name a part of the document
# (a hidden sheet, an attachment). The server answers with a fixed sentence instead and logs nothing of the document
# (Pinned by: test_open_webui.py::TestProcess::test_file_without_text_fails_with_a_fixed_sentence).
logging.getLogger("opendockit.integrations").setLevel(logging.ERROR)
_LOG = logging.getLogger("opendockit.open_webui")


def upload_name(header: str | None, content_type: str | None) -> str:
    """The name to convert an upload under: `X-Filename` decoded, without Open WebUI's file id and any folder, and with the
    extension of its content type when it has none (CSV is told from text by its extension only)
    (Pinned by: test_open_webui.py::TestUploadName)."""
    name = unquote(header or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _STORED_NAME.sub("", name, count=1) or "document"
    if "." not in name.lstrip(".") and content_type:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip().lower())
        if extension:
            name += extension
    return name


def documents(result: ConversionResult, name: str) -> list[dict[str, Any]]:
    """What Open WebUI indexes: `{page_content, metadata}` for every page, slide or sheet (`pieces` in `sections` mode).

    A PDF page also gets `page`, its number counted from 0: Open WebUI shows a citation as 'page N+1' and opens the PDF at it.
    Each text ends with an empty line: Open WebUI keeps the file's whole text as the documents joined by a space, and a page
    starting with a heading or a table would otherwise run on from the last line of the page before
    (Pinned by: test_open_webui.py::TestProcess::test_pdf_gives_a_document_per_page_with_its_page_for_citations,
    test_pages_joined_as_open_webui_joins_them_keep_their_blocks).
    """
    pages = {section.index for section in result.sections if section.kind is SectionKind.PAGE}
    found = []
    for text, metadata in pieces(result, name, "sections"):
        if metadata.get("section_kind") == SectionKind.PAGE.value and metadata.get("section_index") in pages:
            metadata = {**metadata, "page": int(metadata["section_index"]) - 1}
        found.append({"page_content": f"{text}\n\n", "metadata": metadata})
    return found


def create_app(api_key: str, *, workers: int = 2, queue: int = 16, max_chars: int | None = DEFAULT_MAX_CHARS,
               max_file_bytes: int = DEFAULT_MAX_FILE_MB * 1024 * 1024) -> "Starlette":
    """The loader as an ASGI application. `workers` files are converted at once and `queue` more wait for a worker; a file
    beyond that is answered 503 at once. A document longer than `max_chars` characters (None: no limit) or a file larger than
    `max_file_bytes` is refused."""
    if Starlette is None:
        raise ImportError(_NEEDS_EXTRA)
    if len(api_key) < _MIN_KEY_LENGTH:
        raise ValueError(f"The API key must be at least {_MIN_KEY_LENGTH} characters long.")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be an integer of at least 1.")
    if isinstance(queue, bool) or not isinstance(queue, int) or queue < 0:
        raise ValueError("queue must be an integer of at least 0.")
    if max_chars is not None and (isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1):
        raise ValueError("max_chars must be an integer of at least 1 or None.")
    if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
        raise ValueError("max_file_bytes must be an integer of at least 1.")
    # No picture is decoded: the answer is text, so pictures would only cost memory on the way out of the worker. Each keeps
    # its placeholder, alt text included (`[Image: Quarterly revenue chart]`), which `extract_images=False` drops
    # (Pinned by: test_open_webui.py::TestProcess::test_a_picture_keeps_its_alt_text).
    options = ConvertOptions(decode_images=False, max_chars=max_chars, max_file_bytes=max_file_bytes)
    expected = f"Bearer {api_key}".encode()
    state: dict[str, Any] = {"converter": None, "accepted": 0}

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if os.name == "posix":
            # One worker process per file, ended after 2 minutes or at 2 GB (Linux): a document built to hang or expand must
            # end its worker, not the server every upload of the Open WebUI goes through. IsolatedConverter is verified on
            # Linux and macOS only (docs/limitations.md); elsewhere files are converted in a thread of this process.
            state["converter"] = IsolatedConverter(workers=workers)
        try:
            yield
        finally:
            if state["converter"] is not None:
                await state["converter"].aclose()

    async def process(request: Request) -> Response:
        # The key is checked in constant time, before anything of the body is read
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return PlainTextResponse(UNAUTHORIZED, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        # Every accepted file is held in memory until its worker is free: past `workers + queue` the answer is 503 before the
        # body is read, so a burst of uploads cannot pile up gigabytes here (Pinned by:
        # test_open_webui.py::TestProcess::test_files_beyond_the_queue_are_refused_at_once)
        if state["accepted"] >= workers + queue:
            return PlainTextResponse(BUSY, status_code=503, headers={"Retry-After": "5"})
        state["accepted"] += 1
        try:
            return await _answer(request)
        finally:
            state["accepted"] -= 1

    async def _answer(request: Request) -> Response:
        name = upload_name(request.headers.get("x-filename"), request.headers.get("content-type"))
        try:
            data = await _body(request, options.max_file_bytes)
        except ClientDisconnect:  # Open WebUI went away during the upload: there is no one to answer
            return Response(status_code=400)
        if data is None:
            return PlainTextResponse(f"The file is too large (maximum {_size_text(options.max_file_bytes)}).", status_code=413)
        try:
            check_size(data, options)  # an empty file: the library's own sentence, before a worker is asked
            if state["converter"] is None:
                result = await asyncio.to_thread(convert, data, name, options)
            else:
                result = await state["converter"].convert(data, name, options)
        except ConversionError as error:
            return PlainTextResponse(str(error), status_code=422)
        except Exception as error:  # noqa: BLE001 — the answer is a fixed sentence, and the log names only the kind of error
            _LOG.error("A file could not be converted (%s).", type(error).__name__)
            return PlainTextResponse(FAILED, status_code=500)
        if result.exceeded:
            # A document cut at the limit would be indexed in part without a word: Open WebUI has no way to show a warning
            # (Pinned by: test_open_webui.py::TestProcess::test_document_over_the_character_limit_is_refused)
            return PlainTextResponse(TOO_LONG, status_code=422)
        found = documents(result, name)
        if not found:
            # Open WebUI fails a file that gives an empty list with its own message; this one says why
            return PlainTextResponse(NO_TEXT, status_code=422)
        return JSONResponse(found)

    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/process", process, methods=["PUT"]), Route("/health", health, methods=["GET"])],
                     lifespan=lifespan)


async def _body(request: "Request", limit: int) -> bytes | None:
    """The body, or None as soon as it is known to be over `limit` bytes: from its declared length, or after reading one byte
    more than the limit (Pinned by: test_open_webui.py::TestProcess::test_file_over_the_size_limit_is_refused_without_reading_it_all)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            return None
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opendockit-open-webui",
                                     description="Document loader for Open WebUI (Content Extraction Engine: External).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default: %(default)s; 0.0.0.0 to be reached from another container)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=2, metavar="N",
                        help="files converted at once, each in a worker process of up to 2 GB (default: %(default)s)")
    parser.add_argument("--queue", type=int, default=16, metavar="N",
                        help="files that may wait for a worker; more are refused with 503 (default: %(default)s)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, metavar="N",
                        help="refuse documents longer than N characters; 0 for no limit (default: %(default)s)")
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_MB, metavar="N",
                        help="refuse files larger than N MB; each file waiting in the queue is held in memory (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"opendockit-open-webui {__version__}")
    args = parser.parse_args(argv)
    if Starlette is None:
        parser.exit(1, _NEEDS_EXTRA + "\n")
    # The key comes from the environment only: on the command line it would be visible to every user of the machine (`ps`)
    key = os.environ.get(API_KEY_VARIABLE, "").strip()
    if not key:
        parser.error(f"set {API_KEY_VARIABLE} to the API key Open WebUI will send (for example: openssl rand -hex 32)")
    if len(key) < _MIN_KEY_LENGTH:
        parser.error(f"{API_KEY_VARIABLE} must be at least {_MIN_KEY_LENGTH} characters long (for example: openssl rand -hex 32)")
    for flag, value, low in (("--workers", args.workers, 1), ("--queue", args.queue, 0), ("--max-chars", args.max_chars, 0),
                             ("--max-file-mb", args.max_file_mb, 1)):
        if value < low:
            parser.error(f"{flag} must be at least {low}")
    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")
    app = create_app(key, workers=args.workers, queue=args.queue, max_chars=args.max_chars or None,
                     max_file_bytes=args.max_file_mb * 1024 * 1024)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", server_header=False)


if __name__ == "__main__":
    main()
