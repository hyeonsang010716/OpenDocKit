# Design choices and verification

This page records why OpenDocKit is built the way it is — the PDF engine and its license, the pdfium fallback, the in-house HWP parser, the choice of default vision model, the split between conversion and description, and why general-purpose converters were not used — and how the output was verified against real documents. Per-module design records are in [decisions/](decisions/core.md).

## Design choices

### PDF engine

- **The PDF engine is pdfplumber (pdfminer.six, MIT); rendering is pypdfium2 (Apache/BSD).** PyMuPDF (pymupdf4llm) is much faster and its quality is good, but it is **AGPL**: putting it into a service creates an obligation to publish source. If you have resolved the license, only `extractors/pdf.py` needs to be replaced.
- **When pdfminer cannot read something, pdfium reads it instead.** If the document cannot be opened, a page fails to parse (some AES encryption), or a font silently cannot be read and the page is seen as having 0 characters, that page is read as pdfium text and a warning is added. An encrypted document is still an encryption error. Encoding CMaps embedded in fonts (`/Encoding` streams, as produced by WeasyPrint and others) are read directly. If a page still has a font whose characters could not be decoded and pdfium reads clearly more characters, that page is read by plain pdfium extraction with a warning.
- **A slide-style PDF with few lines per page also stops reading the remaining pages once the character limit is exceeded.** However, the top and bottom lines of roughly the first half of the document may be repeated headers, so they are reflected late in the limit decision — so that the decision never counts more than the real length (see [server.md](server.md#character-limit)).
- **pdfium is not thread-safe**, so pdfium calls within a process are serialized with a lock. Converting PDFs concurrently in a thread pool is safe, but PDF rendering runs one at a time. Use a process pool if you need throughput.
- **PDF pictures are not decoded from their original data; the page is rendered as displayed and the picture is cropped out** (matched to the original pixel size, up to 4× scale). The result is exactly what a person sees, regardless of JPX, JBIG2 or CCITT compression, CMYK, masks or clipping.

More: [decisions/pdf.md](decisions/pdf.md), [decisions/pdf-images.md](decisions/pdf-images.md), [decisions/pdf-fonts.md](decisions/pdf-fonts.md), [decisions/pdf-limits.md](decisions/pdf-limits.md).

### Licensing of dependencies

- **Everything installed by default is permissively licensed** (MIT, BSD, Apache-2.0, MIT-CMU, PSF). This was checked against
  the license metadata of every package in the default dependency tree.
- **HEIC/HEIF support is an opt-in extra** (`opendockit[heic]`). The `pillow-heif` source is BSD-3-Clause, but its binary
  wheels bundle libheif and libde265 (LGPLv3) and x265 (GPLv2), and the wheel as a whole is distributed under GPLv2. AVIF does
  not need it: Pillow 11.3+ reads AVIF itself.
- **The vision extra** (`opendockit[openai]`) pulls in LangChain and the OpenAI SDK (MIT / Apache-2.0).

### HWP

- **The HWP 5.x parser is implemented in this project** (olefile + record parsing). pyhwp (hwp5txt) is AGPL and flattens tables into plain text.
- **The body encryption of HWP distribution (read-only) documents is not broken** (it is a copy-protection mechanism). Only the preview text saved by Hancom Office (the beginning of the document) is used, with a warning.

More: [decisions/hwp.md](decisions/hwp.md).

### Default vision model

**The default vision model is `gpt-5.6-terra`** (`describe.DEFAULT_MODEL`). The previous default, gpt-6-luna, was chosen by feeding a dense 40-line A4 scan 4 times and text blurred beyond reading 8 times (table below). After switching to terra, the full set of tests against the real model was run again.

| Model | Lines identical to the original (40 lines × 4 runs) | "(illegible)" for unreadable text |
| --- | --- | --- |
| gpt-4o-mini | 0, 0, 0, 0 (made up numbers) | 4/4 |
| gpt-4.1-mini · gpt-4o · gpt-4.1 | 96–99% of characters | 0/4 (made up sentences that are not there) |
| gpt-5-mini | 40, 35, 35, 40 | 4/4 |
| gpt-5.6-terra | 35, 35, 35, numbering omitted | 8/8 |
| gpt-5.6-luna | 40, 40, 40, numbering omitted | 8/8 |
| gpt-6-luna | 40, 40, 40, 40 | 8/8 |

These are synthetic images and a small sample. Newer models tend to correct particle typos of the original while transcribing ("요청는" → "요청은"). Defenses such as the SKIP instruction and the confirmation query were added because of wobble observed with gpt-4o-mini; they cost almost nothing with other models, so they stay. See [images.md](images.md#prompts-by-image-kind).

### Conversion and description are separate

Conversion is offline, synchronous and deterministic (it can be tested and run in a process pool). Only description, which needs the network and costs money, is split off as an asynchronous step.

### Why not a general-purpose converter

General-purpose converters (markitdown, markdownify, docling) were not used because they either lack the handling that Korean documents need — HWP, form-style tables with many merged cells, CP949, HTML disguised as `.xls` (markitdown does not support HWP) — or require ML models and a GPU, which is heavy for a server (docling).

## Verification

### Comparison against public documents (2026-09-30)

36 press releases from Korea's policy briefing site (the same document as HWP, HWPX and PDF) and 95 files from the Apache POI public PPTX collection were checked automatically for whether every character of the original appears in the result.

| Target | Reference | Result |
| --- | --- | --- |
| 22 HWP · 29 HWPX | Characters of the same document's PDF (pdfium) | Of 34,207 words in the HWP/HWPX results, 5 are not in the PDF |
| 31 HWPX | The 8,305 text runs (`hp:t`) in the files | 0 missing |
| 34 PDF (236 pages) | The 37,213 words read by pdfium | 66 missing (0.18%, mostly words joined on the reference side) |
| 33 PDF ↔ HWPX pairs | Words identical including spacing | 99.1% (the remaining differences are words spanning a line break) |
| 7 scanned-OCR pages (real pages rendered as images) | The original text layer | 99–100% of words, 151/154 numbers |
| 83 PPTX | 2,773 slide paragraphs · 242 SmartArt texts | 0 missing (excluding page numbers and footers, dropped on purpose) |
| 5 XLSX | 24,474 cells (609 merges) | 0 missing |

Found and fixed through this comparison: a regression that inserted a space after every character in justified PDFs, tables with dotted cell boundaries, loss of characters inside table regions, rejection of a whole PPTX because of a picture shape without picture information, and missing SmartArt text. One distribution (copy-protected) HWP document produced only its preview, as intended.

### Checking the shape of the output

Convert real documents with the CLI (`opendockit file.pdf`, `--json` for sections, metadata and warnings) and read the result. Personal and copyrighted documents are not committed to the repository.

### Unit tests

```bash
uv sync
uv run pytest -n auto
uv run ruff check
```

Sample documents are built by the tests themselves, in memory, and the vision model is replaced by a fake describer. Because no Python library can create an OLE container for HWP, only the container is faked; the record streams inside are built in the real format.

### Tests against a real vision model

**Checked with the English prompts (2026-11-12).** The prompts were rewritten in English for 0.1.0, so the live suite in
`tests/test_live_vision.py` was run against `gpt-5.6-terra` on an OpenAI-compatible Azure endpoint: 16 tests (pictures inside
DOCX, a DOCX table cell, PPTX, XLSX, HTML and PDF; a scanned PDF page; PNG, JPEG, WebP, GIF, BMP, TIFF and AVIF files; a long
picture sent as tiles; a chart; a decorative picture; a wrong key followed by a retry), six consecutive runs, 96/96 passed,
plus one run with `gpt-5.6-luna`, 16/16. In separate spot checks a Korean schedule drawn as an image was transcribed in Korean
as a table with every cell correct, and 8 of 8 decorative pictures were dropped. The older figures below were measured with
the earlier Korean prompts and have not been repeated.

20 calls to real OpenAI (default model `gpt-5.6-terra`). A picture carrying a numeric code is placed in every format and the test checks that the number appears at the right place in the result (between paragraphs, in a table cell, on a slide, on a sheet, on a scanned page). The run covers 7 image types (PNG, JPEG, WEBP, GIF, BMP, TIFF, HEIC), Hangul text, removal of decoration, a wrong key (placeholder text plus a warning), a process pool, and `detail="low"`. All passed 3 times in a row with the final code.

Model wobble (measured with the earlier default model gpt-4o-mini): text pictures inside PDFs preserved 72/72; textless patterns removed 17/18 (the remaining one was kept with a description attached — not a loss).

### Public corpus and corrupted input (2026-11-12)

1,584 public test documents (Apache POI test data, pdfplumber's PDFs, the hwplib and hwpxlib samples — including their fuzzer
crash cases) were converted through `IsolatedConverter` with a 120 s limit: 1,161 converted, 423 were rejected with a
`ConversionError` (legacy `.doc`/`.ppt`, encrypted files, Strict Open XML, deliberately broken or fuzzed files), and there were
no crashes, no unexpected exceptions and no timeouts. Converted results were checked for leftover placeholders, NFC, control
characters, consistent table widths and picklability. A further 1,680 randomly corrupted variants of the files in `examples/`
(bit flips, truncation, inserted garbage, repeated ranges) gave only successes and `ConversionError`s.

### Full run over real files

Offline, on the development machine: every file of the document formats (PDF 1,613 · PPTX 811 · DOCX 333 · XLSX 256 · CSV 282 · HWP 43 · HWPX 7 · XLS 1) and a sample per format for images, MD, HTML and TXT — 4,906 files in total. 0 crashes and 0 timeouts. Each file was checked for leftover placeholders, NFC, control characters, table column counts, image decoding/size/format and picklability. The final run had 0 problem verdicts (excluding 2 lines where the source text itself starts with `|`), and all 16 rejections were correct (0-byte files, Word lock files `~$`, truncated PDFs, encrypted documents, files over 50 MB). The slowest file took 63 s (the time budget).
