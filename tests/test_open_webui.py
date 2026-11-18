"""The document loader for Open WebUI (`opendockit.open_webui`): `PUT /process` as Open WebUI's External content extraction
engine sends it (v0.13.3, `backend/open_webui/retrieval/loaders/external_document.py`): the file's bytes as the body, its MIME
type as `Content-Type`, `Bearer <key>` and the stored name `<file id>_<original name>` URL-encoded in `X-Filename`."""

import asyncio
from io import BytesIO
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import pytest

from opendockit import open_webui
from tests import builder
from tests.test_integrations import _scanned_pdf


if open_webui.Starlette is None:
    pytest.skip("needs the open-webui extra", allow_module_level=True)

import httpx  # after the skip: the extra's absence is what the skip reports
from starlette.testclient import TestClient


KEY = "k" * 32
FILE_ID = "0f8e7a52-3b1c-4c5e-9a1d-2b6f4e8c9d01"


def _headers(name: str, content_type: str = "application/octet-stream", key: str = KEY) -> dict[str, str]:
    """The headers Open WebUI sends for an upload named `name`."""
    return {"Authorization": f"Bearer {key}", "Content-Type": content_type, "X-Filename": quote(f"{FILE_ID}_{name}")}


@pytest.fixture(scope="module")
def client():
    """The loader with its worker processes, as `opendockit-open-webui` runs it."""
    with TestClient(open_webui.create_app(KEY)) as test_client:
        yield test_client


def _put(client, data: bytes, name: str, **headers) -> httpx.Response:
    return client.put("/process", content=data, headers=_headers(name, **headers))


async def _put_async(app, data, name: str) -> httpx.Response:
    """A request to `app` without its lifespan, for what a test client run in one thread cannot do: several requests at once,
    a body that is read as it comes. A test that converts a file this way replaces `IsolatedConverter.convert`, so no worker
    pool is left behind without the lifespan that would close it."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://loader") as async_client:
        return await async_client.put("/process", content=data, headers=_headers(name))


class TestUploadName:
    def test_file_id_and_encoding_are_taken_off(self):
        assert open_webui.upload_name(quote(f"{FILE_ID}_2024 사업보고서.hwpx"), "application/haansofthwpx") == "2024 사업보고서.hwpx"

    def test_a_name_without_the_file_id_is_kept(self):
        assert open_webui.upload_name("report_final.docx", None) == "report_final.docx"

    def test_folders_in_the_name_are_dropped(self):
        assert open_webui.upload_name(quote("../../etc/report.pdf"), None) == "report.pdf"
        assert open_webui.upload_name(quote("C:\\Users\\me\\report.pdf"), None) == "report.pdf"

    def test_a_name_without_an_extension_gets_the_one_of_its_content_type(self):
        """CSV is told from text by its extension only; a remote file can come without one."""
        assert open_webui.upload_name(quote(f"{FILE_ID}_download"), "text/csv; charset=utf-8") == "download.csv"
        assert open_webui.upload_name(None, "application/pdf") == "document.pdf"
        assert open_webui.upload_name(None, None) == "document"

    def test_an_extension_is_not_replaced_by_the_content_type(self):
        assert open_webui.upload_name("sales.csv", "text/plain") == "sales.csv"


class TestProcess:
    def test_pdf_gives_a_document_per_page_with_its_page_for_citations(self, client):
        """Open WebUI shows `metadata.page` as 'page N+1' in a citation and opens the PDF at it."""
        response = _put(client, builder.pdf_bytes(pages=2), "paper.pdf", content_type="application/pdf")

        assert response.status_code == 200
        documents = response.json()
        assert [document["metadata"]["page"] for document in documents] == [0, 1]
        assert [document["metadata"]["section_index"] for document in documents] == [1, 2]
        assert all(document["metadata"]["format"] == "pdf" and document["metadata"]["source"] == "paper.pdf" for document in documents)
        assert all(document["page_content"].strip() for document in documents)

    def test_slides_and_sheets_are_documents_without_a_page(self, client):
        """A slide or sheet number is not a page of a PDF to open: only PDF pages get `page`."""
        for data, name, heading in ((builder.pptx_bytes(), "deck.pptx", "## Slide 1"), (builder.xlsx_bytes(), "book.xlsx", "## Sheet: ")):
            documents = _put(client, data, name).json()

            assert documents
            assert documents[0]["page_content"].startswith(heading)
            assert all("page" not in document["metadata"] for document in documents)

    def test_document_without_units_is_one_document(self, client):
        documents = _put(client, builder.hwpx_bytes(), "notice.hwpx").json()

        assert len(documents) == 1
        assert documents[0]["metadata"]["format"] == "hwpx" and "section_index" not in documents[0]["metadata"]

    def test_pages_joined_as_open_webui_joins_them_keep_their_blocks(self, client):
        """Open WebUI keeps a file's whole text as its documents joined by a space (`' '.join`): each page's heading or table
        must still start a block of its own there, not run on from the last line of the page before."""
        documents = _put(client, builder.pptx_bytes(), "deck.pptx").json()
        joined = " ".join(document["page_content"] for document in documents)

        for document in documents[1:]:
            assert f"\n\n {document['page_content'].strip()}" in joined

    def test_a_picture_keeps_its_alt_text(self, client):
        """No picture is decoded, but the alt text an author gave one stays (`decode_images=False`)."""
        from docx import Document
        from docx.shared import Inches

        document = Document()
        document.add_paragraph("Revenue grew in Q3.")
        document.add_picture(BytesIO(builder.png_bytes(300, 200, seed=1)), width=Inches(2))
        document.inline_shapes[-1]._inline.docPr.set("descr", "Quarterly revenue chart: Q3 up 12%")
        buffer = BytesIO()
        document.save(buffer)

        text = _put(client, buffer.getvalue(), "report.docx").json()[0]["page_content"]

        assert "[Image: Quarterly revenue chart: Q3 up 12%]" in text

    def test_file_without_text_fails_with_a_fixed_sentence(self, client, caplog):
        """An empty list would fail in Open WebUI with 'No content returned'; this says why. Nothing of the document is in the
        answer or in the log (a warning can name a part of it)."""
        with caplog.at_level(logging.DEBUG):
            response = _put(client, _scanned_pdf(), "scan.pdf")

        assert (response.status_code, response.text) == (422, open_webui.NO_TEXT)
        assert not [record for record in caplog.records if record.name.startswith("opendockit.integrations")]

    def test_conversion_errors_are_the_converters_fixed_sentences(self, client):
        broken = _put(client, b"PK\x03\x04 not really a zip", "report.docx")
        empty = _put(client, b"", "report.docx")

        assert broken.status_code == 422 and broken.text.endswith(".") and "zip" not in broken.text.lower()
        assert (empty.status_code, empty.text) == (422, "The file is empty.")

    def test_conversion_runs_in_a_worker_process(self, client, monkeypatch):
        """The loader's own process must not parse the document: a file built to hang or expand would end the server every
        upload goes through."""
        used: list[str] = []
        convert = open_webui.IsolatedConverter.convert

        async def spy(self, data, filename, options=None, **more):
            used.append(filename)
            return await convert(self, data, filename, options, **more)

        monkeypatch.setattr(open_webui.IsolatedConverter, "convert", spy)

        assert _put(client, builder.docx_bytes(), "report.docx").status_code == 200
        assert used == ["report.docx"]

    @pytest.mark.parametrize("authorization", [None, "Bearer wrong-key-of-enough-length", f"Basic {KEY}", KEY])
    def test_a_request_without_the_key_is_refused_before_its_body_is_read(self, authorization):
        read: list[bytes] = []

        async def body():
            for chunk in (b"a", b"b"):
                read.append(chunk)
                yield chunk

        async def run() -> httpx.Response:
            headers = {"X-Filename": "a.txt"} | ({"Authorization": authorization} if authorization else {})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=open_webui.create_app(KEY)), base_url="http://loader") as c:
                return await c.put("/process", content=body(), headers=headers)

        response = asyncio.run(run())

        assert (response.status_code, response.text) == (401, open_webui.UNAUTHORIZED)
        assert response.headers["www-authenticate"] == "Bearer"
        assert len(read) <= 1  # the transport may take the first piece before the app runs; the app reads none

    def test_file_over_the_size_limit_is_refused_without_reading_it_all(self):
        """A body of unknown length (chunked) is read only to one piece past the limit (50 MB), not to its end."""
        sent: list[int] = []
        piece = b"\0" * (1 << 20)

        async def body():
            for _ in range(200):
                sent.append(len(piece))
                yield piece

        response = asyncio.run(_put_async(open_webui.create_app(KEY), body(), "huge.pdf"))

        assert (response.status_code, response.text) == (413, "The file is too large (maximum 50MB).")
        assert sum(sent) <= 52 << 20

    def test_file_declared_over_the_size_limit_is_refused_before_any_of_it_is_read(self):
        """Open WebUI sends the length of the file (`requests` sets `Content-Length`): one over the limit is answered at once."""
        sent: list[dict] = []

        async def receive():
            pytest.fail("the body was read")

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "http",
                 "path": "/process", "raw_path": b"/process", "query_string": b"", "root_path": "", "server": ("loader", 80),
                 "client": ("127.0.0.1", 1), "headers": [(b"authorization", f"Bearer {KEY}".encode()), (b"x-filename", b"a.pdf"),
                                                         (b"content-length", str(60 << 20).encode())]}
        asyncio.run(open_webui.create_app(KEY)(scope, receive, send))

        assert sent[0]["status"] == 413 and sent[1]["body"] == b"The file is too large (maximum 50MB)."

    def test_an_upload_cut_off_by_the_client_ends_quietly(self, caplog):
        """Open WebUI going away during the upload is not a failed conversion: nothing is converted and nothing is logged."""
        sent: list[dict] = []
        messages = iter([{"type": "http.request", "body": b"PK", "more_body": True}, {"type": "http.disconnect"}])

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "http",
                 "path": "/process", "raw_path": b"/process", "query_string": b"", "root_path": "", "server": ("loader", 80),
                 "client": ("127.0.0.1", 1), "headers": [(b"authorization", f"Bearer {KEY}".encode()), (b"x-filename", b"a.docx")]}
        with caplog.at_level(logging.DEBUG):
            asyncio.run(open_webui.create_app(KEY)(scope, receive, send))

        assert sent[0]["status"] == 400
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    def test_a_picture_file_fails_with_the_no_text_sentence(self, client):
        """Open WebUI sends pictures to an External engine unless its media types are set; one is not indexed as '[Image]'."""
        response = _put(client, builder.png_bytes(), "photo.png", content_type="image/png")

        assert (response.status_code, response.text) == (422, open_webui.NO_TEXT)

    def test_size_limit_can_be_raised(self):
        """Annual reports with pictures are often past 50 MB: the operator sets the limit (`--max-file-mb`)."""
        response = asyncio.run(_put_async(open_webui.create_app(KEY, max_file_bytes=1024), b"x" * 2048, "notes.txt"))

        assert (response.status_code, response.text) == (413, "The file is too large (maximum 1KB).")

    def test_document_over_the_character_limit_is_refused(self, monkeypatch):
        """Indexed in part, a document cut at `max_chars` would be missing its end without a word."""
        monkeypatch.setattr(open_webui.os, "name", "nt")
        response = asyncio.run(_put_async(open_webui.create_app(KEY, max_chars=10), builder.docx_bytes(), "report.docx"))

        assert (response.status_code, response.text) == (422, open_webui.TOO_LONG)
        assert open_webui.DEFAULT_MAX_CHARS == 10_000_000  # far above real documents; a bound on what one result holds

    def test_files_beyond_the_queue_are_refused_at_once(self, monkeypatch):
        """With one worker and one place in the queue, a third file at once gets 503 without waiting; the two others finish."""
        started = threading.Semaphore(0)
        release = threading.Event()
        convert = open_webui.IsolatedConverter.convert

        async def slow(self, data, filename, options=None, **more):
            started.release()
            await asyncio.to_thread(release.wait, 10)
            return await convert(self, data, filename, options, **more)

        monkeypatch.setattr(open_webui.IsolatedConverter, "convert", slow)
        app = open_webui.create_app(KEY, workers=1, queue=1)
        data = builder.docx_bytes()

        async def run() -> list[int]:
            first = asyncio.create_task(_put_async(app, data, "a.docx"))
            second = asyncio.create_task(_put_async(app, data, "b.docx"))
            await asyncio.to_thread(started.acquire, True, 10)
            await asyncio.sleep(0.2)  # the second request is accepted and waiting as well
            third = await _put_async(app, data, "c.docx")
            release.set()
            return [(await first).status_code, (await second).status_code, third.status_code]

        codes = asyncio.run(run())

        assert codes == [200, 200, 503]

    def test_an_unexpected_failure_is_a_fixed_sentence_and_logs_nothing_of_the_document(self, monkeypatch, caplog):
        async def failing(*_, **__):
            raise ValueError("Quarterly revenue 12,345 for ACME")

        monkeypatch.setattr(open_webui.IsolatedConverter, "convert", failing)
        with caplog.at_level(logging.DEBUG):
            response = asyncio.run(_put_async(open_webui.create_app(KEY), builder.docx_bytes(), "report.docx"))

        assert (response.status_code, response.text) == (500, open_webui.FAILED)
        assert "ACME" not in caplog.text and "ValueError" in caplog.text

    def test_an_app_mounted_in_another_one_converts_in_a_worker(self, monkeypatch):
        """Starlette does not run the lifespan of a mounted app. The workers were made there, so such an app converted every
        file in the server process, without the worker's time and memory limits."""
        from starlette.applications import Starlette
        from starlette.routing import Mount

        made: list[object] = []

        class Recorded(open_webui.IsolatedConverter):
            def __init__(self, **arguments):
                super().__init__(**arguments)
                made.append(self)

        monkeypatch.setattr(open_webui, "IsolatedConverter", Recorded)
        parent = Starlette(routes=[Mount("/owui", app=open_webui.create_app(KEY))])
        try:
            with TestClient(parent) as test_client:
                response = test_client.put("/owui/process", content=builder.docx_bytes(), headers=_headers("report.docx"))
        finally:
            for converter in made:  # nothing closes them: the mounted app's lifespan is not run
                asyncio.run(converter.aclose())

        assert response.status_code == 200
        assert "1장 개요" in response.json()[0]["page_content"]
        assert len(made) == 1, "the mounted app did not convert in a worker"

    def test_health_needs_no_key(self, client):
        assert (client.get("/health").status_code, client.get("/health").text) == (200, "ok")

    def test_other_methods_are_not_allowed(self, client):
        assert client.post("/process", content=b"x", headers=_headers("a.txt")).status_code == 405


class TestCreateApp:
    @pytest.mark.parametrize("arguments", [{"workers": 0}, {"workers": True}, {"queue": -1}, {"max_chars": 0}, {"max_file_bytes": 0}])
    def test_wrong_arguments_are_refused(self, arguments):
        with pytest.raises(ValueError):
            open_webui.create_app(KEY, **arguments)

    def test_short_key_is_refused(self):
        with pytest.raises(ValueError, match="at least 16 characters"):
            open_webui.create_app("short")


class TestCommandLine:
    def test_without_a_key_it_says_how_to_make_one(self, monkeypatch, capsys):
        monkeypatch.delenv(open_webui.API_KEY_VARIABLE, raising=False)
        with pytest.raises(SystemExit):
            open_webui.main([])

        assert "OPENDOCKIT_OPEN_WEBUI_API_KEY" in capsys.readouterr().err

    def test_short_key_is_refused(self, monkeypatch, capsys):
        monkeypatch.setenv(open_webui.API_KEY_VARIABLE, "secret")
        with pytest.raises(SystemExit):
            open_webui.main([])

        assert "at least 16 characters" in capsys.readouterr().err

    def test_without_the_extra_it_says_what_to_install(self):
        """The command is installed with every OpenDocKit: without Starlette and Uvicorn the module still imports, and the
        command ends with the install command instead of a traceback."""
        blocked = ("import sys; sys.modules['starlette'] = None; sys.modules['uvicorn'] = None; "
                   "from opendockit.open_webui import main; main(sys.argv[1:])")
        environment = os.environ | {open_webui.API_KEY_VARIABLE: KEY}
        version = subprocess.run([sys.executable, "-c", blocked, "--version"], capture_output=True, text=True, env=environment)
        start = subprocess.run([sys.executable, "-c", blocked], capture_output=True, text=True, env=environment)

        assert version.returncode == 0 and version.stdout.startswith("opendockit-open-webui ")
        assert start.returncode == 1 and 'pip install "opendockit[open-webui]"' in start.stderr
        assert "Traceback" not in start.stderr

    def test_listens_on_this_machine_only_by_default(self, monkeypatch):
        started: dict = {}
        monkeypatch.setenv(open_webui.API_KEY_VARIABLE, KEY)
        monkeypatch.setattr(open_webui.uvicorn, "run", lambda app, **options: started.update(options))

        open_webui.main([])

        assert (started["host"], started["port"]) == ("127.0.0.1", 8765)

    def test_limits_from_the_command_line(self, monkeypatch):
        made: dict = {}
        monkeypatch.setenv(open_webui.API_KEY_VARIABLE, KEY)
        monkeypatch.setattr(open_webui, "create_app", lambda key, **options: made.update(options))
        monkeypatch.setattr(open_webui.uvicorn, "run", lambda app, **options: None)

        open_webui.main(["--max-chars", "0", "--max-file-mb", "200"])

        assert made["max_chars"] is None and made["max_file_bytes"] == 200 * 1024 * 1024

    @pytest.mark.timing
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
    def test_server_answers_open_webui_over_http(self, tmp_path):
        """The real thing: the command started as a separate process, and a request as Open WebUI sends it over TCP."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = os.environ | {open_webui.API_KEY_VARIABLE: KEY}
        server = subprocess.Popen([sys.executable, "-m", "opendockit.open_webui", "--port", str(port)], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline and server.poll() is None, "the server did not start"
                time.sleep(0.1)
            response = httpx.put(f"http://127.0.0.1:{port}/process", content=builder.pdf_bytes(pages=2),
                                 headers=_headers("paper.pdf", "application/pdf"), timeout=60)
        finally:
            server.terminate()
            output = server.communicate(timeout=30)[0].decode()

        assert response.status_code == 200 and [document["metadata"]["page"] for document in response.json()] == [0, 1]
        assert "paper.pdf" not in output  # the log names no file
