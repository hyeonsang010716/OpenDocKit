# Running in a server

This page explains how to use OpenDocKit behind an upload endpoint: how the per-file character limit works (`max_chars` stops conversion rather than truncating), how to run conversions in isolated worker processes with `IsolatedConverter` in a FastAPI application, which resource limits apply and why, and what those limits cannot stop. Option details are in [options.md](options.md); a summary for people building the upload UI is in [integration-guide.md](integration-guide.md).

## Character limit

Conversion works on **one file at a time**. Each conversion returns the character count and whether the limit was exceeded, and the caller uses that to restrict uploads.

```python
from opendockit import ConversionError, OpenAIImageDescriber

describer = OpenAIImageDescriber()          # pass a describer to count the text inside pictures too

async def count(file: UploadFile):
    try:                                    # converter: the application-lifetime IsolatedConverter (see below)
        result = await converter.convert(await file.read(), file.filename, describer=describer, max_concurrency=8)
    except ConversionError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"char_count": result.char_count, "exceeded": result.exceeded, "warnings": result.warnings}
```

```bash
opendockit file.pdf --count --describe    # "18232" or "555222 exceeded"
```

**The character limit (`max_chars`, default 500,000) is a point at which conversion stops, not a point at which text is cut.** The moment it is exceeded, conversion stops and the result comes back with `exceeded=True`. The purpose is to spend no time and no vision-model calls on a document that will be rejected anyway. There are three places where it stops.

| Stage | How it stops | Measured |
| --- | --- | --- |
| During extraction | PDF is read page by page, Excel and CSV row by row, PPTX slide by slide, DOCX/HWP/HWPX paragraph by paragraph and table by table (large tables cell by cell); once over the limit, the rest is not read | 130,000-row Excel file: 7.6 s → 0.4 s; DOCX with a 200,000-row table: 33 s → 4.6 s |
| After extraction | If the text alone is already over the limit, images are not decoded and description is not started | 0 vision-model calls |
| During image description | The length is counted again each time an image finishes; once it is certainly over, the remaining calls are cancelled | 40 scanned pages: 40 calls → 7 completed, 20.9 s → 5.6 s |

- When `exceeded=True`, `text` holds content only up to the limit and `char_count` is **the count at the moment conversion stopped**. The real length of the document is that or more. `sections[].text` is cut at the same point as `text` (a section that starts after the cut has empty text).
- **`exceeded` is exactly equal to "the character count of a full conversion > the limit".** The decision to stop early is only ever made with a value that is not larger than the real one: during extraction the count excludes whitespace, image placeholders and characters that will be removed; during image description the text is actually assembled to check whether the limit is exceeded "even if every remaining image is removed, alt text included". This matters because when a picture is judged decorative its `[Image]` is removed and the character count **goes down**. This was checked on 181 real files with the limit set to the real length −1, equal and +1 (0 violations).
- `char_count` includes image descriptions. Counting without a `describer` counts each picture only as `[Image]`, and a scanned PDF comes out close to 0 (with a warning that some pages contain text only as images).
- Excel is not cut by row count. The only size limit is the character count.
- To remove the limit: `ConvertOptions(max_chars=None)`.
- Page images of a scanned PDF are all rendered before description starts (around 0.1 s per page). If description stops midway, rendering has already been done.
- For a total over several files, the caller adds up each file's `char_count`. This library does not handle batches.

## FastAPI

**Do not run `convert` or `aconvert` inside the server process (in a thread).** The GIL does not protect you from blow-ups. A file that expands to several GB takes the whole server down with OOM; a file that crashes a C extension (pdfium, lxml) ends the server process; and a file that takes minutes runs in a thread that cannot be stopped. Meanwhile pure-Python parsing holds the GIL and slows down other responses of the same process (chat, SSE). Run conversions in worker processes with `IsolatedConverter` instead.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile
from opendockit import ConversionError, ConvertOptions, IsolatedConverter, OpenAIImageDescriber

options = ConvertOptions(max_file_bytes=30 * 1024 * 1024, max_images=20)   # max_images is unlimited by default — set it in a server
describer = OpenAIImageDescriber()          # reads OPENAI_API_KEY; or OpenAIImageDescriber(model) to reuse your own LangChain chat model

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with IsolatedConverter(workers=2) as converter:   # 2 workers, each limited to 2 GB of memory and 2 minutes
        app.state.converter = converter
        yield

app = FastAPI(lifespan=lifespan)

@app.post("/upload")
async def upload(file: UploadFile):
    data = await file.read()
    try:
        result = await app.state.converter.convert(data, file.filename, options, describer=describer, max_concurrency=4)
    except ConversionError as exc:          # the message is fixed wording that can be shown as is; branch on exc.code
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"text": result.text, "warnings": result.warnings}
```

- When the time or memory limit is exceeded, or the worker dies, only that file ends with `DocumentTooLargeError` (`document_too_large`, message `TOO_COMPLEX_MESSAGE`: the document is too large or too complex to convert). The server and the next file are unaffected. When the request is cancelled (client disconnect), that worker is terminated too.
- A new worker is started for every file (a few ms, because the package is preloaded in the forkserver). No memory is left behind after a large document. Plain `fork` is not used: a server process runs database-driver and asyncio threads, and a forked child can hang.
- `workers` is the number of files converted at the same time. Further files wait until a slot is free. Memory use is at most `workers × 2 GB` (multiplied again by the number of uvicorn workers, if you run several).
- The upload size check (`max_file_bytes`) happens after the file has been received into memory, so limit the request size earlier, at the reverse proxy or endpoint.
- Image description is sent by the server's event loop, outside the time limit (it is network waiting, so a long duration is normal). It takes seconds to tens of seconds, so rather than making the upload response wait, run it as a background task. To add descriptions later, convert without a `describer`, store the result, and call `await describe_images(result, describer)` separately.
- An `OpenAIImageDescriber()` created without a model builds a separate httpx client per event loop. A worker that calls `asyncio.run` several times should close that loop's client with `await describer.aclose()` before the loop ends. A server that keeps using one loop does not need this.
- `aconvert(..., executor=...)` is for scripts and batch jobs, where a bad file is acceptable.
- Workers re-import the server's entry module (`__main__`) — this is how forkserver and spawn work. Started as `uvicorn myapp.main:app`, only the uvicorn launcher script is re-imported, which is harmless. Started as `python -m myapp.main`, the top-level code of `myapp.main` (application creation, logging setup) runs in every worker and the startup log is printed for every conversion. The next worker is started as soon as the previous conversion ends, so response time is hardly affected. Keep the entry module free of work done at import time such as opening database connections (connect in the lifespan), and wrap directly executed scripts in `if __name__ == "__main__":`.
- In Docker (arm64 Linux) on an Apple M4 Mac, importing `cryptography` (used by pdfminer) dies with `Illegal instruction`. The virtual machine advertises an instruction set (SVE) that the CPU cannot actually execute, and OpenSSL uses it. This is not a problem of this library; set `OPENSSL_armcap=0` in that environment. Linux servers (x86_64, Graviton) are not affected.

## Resource limits

Defaults: upload 50 MB · worker memory 2 GB · conversion 2 minutes.

**An upload size limit alone is not enough.** A file that compresses small but expands large can use several GB and several minutes for one document while staying under 50 MB. Measured: a 6.8 KB DOCX used 4.1 GB; a 10 KB XLS, 2 GB and 132 s; a 0.48 MB XLSX, 2.3 GB; a 48 MB CSV, 7 GB; a 150 KB TXT, 77 s. Most such files are made on purpose and the library cannot block all of them, so the server bounds the memory and time a single conversion may use, so that the outcome is **"only that file fails to convert"**.

| Limit | Value | Basis (measured on Linux) |
| --- | --- | --- |
| Upload size | 50 MB (default of `max_file_bytes`) | — |
| Worker memory (`RLIMIT_AS`) | 2 GB | The heaviest normal documents: a 46 MB scanned PDF used 875 MB resident and 1.19 GB of address space; a 48 MB Korean TXT, 702 MB and 1.03 GB. Ordinary documents are around 0.1 GB and 0.42 GB. `RLIMIT_AS` counts about 300 MB more than actual use, so 1.5 GB leaves too little headroom |
| Conversion time | 2 minutes | The PDF's own time budget (`pdf_time_budget`, 60 s) plus rendering. A 200-page scanned PDF takes 20 s |

`IsolatedConverter` applies these limits as follows (defaults `memory_limit=2 GB`, `timeout=120`).

1. **The memory limit is applied to the worker process only** (`RLIMIT_AS`). It works on Linux only; macOS ignores it, leaving the time limit and process isolation.
2. **When the time is exceeded the worker is actually terminated.** `asyncio.wait_for` alone only stops waiting; the worker keeps running. The public API of Python 3.12 cannot stop a running worker, so the process is killed directly (deliberate; uses the private `_processes`). A process that dies on its own (`BrokenProcessPool`) is handled the same way.
3. **Each worker has its own pool.** With several files in one pool, terminating a worker would fail the other files too.
4. **The time limit applies to the conversion step only.** Image description is sent separately from the server's event loop.
5. **Memory exhaustion is identified inside the worker.** When an exception crosses the process boundary its `__cause__` becomes a string traceback, so the main process cannot tell that it was a `MemoryError`. Exceeding memory or time, and worker death, are all `DocumentTooLargeError` ("too large or too complex").

### Measurements

Pinned by `tests/test_isolated.py` (time limit, worker death, the next file after a cancelled request, a new worker per file; the memory limit on Linux only). In a Linux container (arm64, Python 3.12) all 3,024 tests passed. Feeding a 40 MB CSV to a worker with a 300 MB memory limit ended in 0.6 s with `document_too_large`, the server process stayed at 156 MB, and the next file succeeded immediately. A 40 MB CSV with a 10 s time limit ended at 10.0 s.

In a Linux container (8 GB of memory), 5 kinds of normal documents and 23 crafted files were fed one after another through hand-written code using the same scheme (a pool of one worker, 2 GB, 120 s, a new pool after termination). These measurements predate the move to `IsolatedConverter`.

- **All normal documents succeeded**: ordinary PDF, HWP and XLSX, a 46 MB scanned PDF (200 pictures), a 48 MB Korean TXT. Main process peak: 462 MB.
- **All 13 memory bombs ended in failure**: DOCX and XLSX with falsified sizes, empty XML (DOCX of 80 and 300 MB, PPTX), CSV of empty lines and commas, HTML of empty tags, XLSX shared strings and styles, XLS with far-away cells. Most took a few seconds; the longest, 104 s.
- **All 8 time bombs were terminated at 120 s** (regular expressions, quadratic loops, formats, style chains, sheets with far-away cells).
- **After a failure the server and the next conversion were unaffected** (normal PDFs inserted in between succeeded). The main process never went above 462 MB.
- A DOCX that used 4 GB through a small WebP now only fails to decode the picture and succeeds without it. A DOCX that produced 2.4 GB of pictures failed with `MemoryError` while the worker was pickling the result, and never reached the main process.

Known accepted gap: for a DOCX with a large empty XML, lxml reports running out of memory not as `MemoryError` but as `XMLSyntaxError("unknown error")`. The file is blocked, but with the "may be corrupted" message. There is no way to tell this apart from genuinely broken XML, so it is left as is.

### What the limits cannot stop

Both of the following happen outside the worker, in the main process. They do not occur with normal documents, only with crafted image files, so they are to be fixed in the library before opening a public service where anyone can upload.

- **Memory when the conversion result is copied to the main process.** The result (including images) crosses to the main process by pickle, and the main process uses about 2.2 times the total image size (a result with 2.4 GB of images cost the main process 5.3 GB). Even within the 2 GB worker limit, 1.5–2 GB may appear in the main process (estimate). For normal documents, even a 50 MB scanned PDF has 134–141 MB of images in total. Where to fix: a limit on the total bytes of result images.
- **CPU in the image description step.** Each time a picture is judged decorative (SKIP), the whole text is assembled again; in a document just under the limit, 10,000 pictures block the event loop (including chat responses) for 245 s. Real documents have tens of pictures, which takes under 1 s. Where to fix: make `converter.image_length_bound` return 0 for decorative pictures.

One bad file can hold a worker for up to 2 minutes, during which other uploads wait. If that becomes a problem, limit the number of uploads per user.
