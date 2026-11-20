# Options and exceptions

This page is the reference for `ConvertOptions` (limits and what to include), the parameters of `OpenAIImageDescriber`, how option values are validated, and the exceptions a conversion can raise together with their `code` values. How the limits behave in a server is explained in [server.md](server.md).

```python
from opendockit import ConvertOptions, convert

options = ConvertOptions(max_chars=200_000, max_images=20, include_speaker_notes=False)
result = convert(data, "deck.pptx", options)
```

## `ConvertOptions`

| Key | Default | Description |
| --- | --- | --- |
| `max_file_bytes` | 50 MB | Above this, `DocumentTooLargeError` |
| `max_uncompressed_bytes` | 500 MB | Total decompressed size (zip-bomb defense). Applies to ZIP-based formats, HWP body and picture streams, and PDF streams (excluding pictures and attached files). It also bounds the total bytes of the prepared (re-encoded) pictures of one document: pictures past it keep their `[Image]` placeholder, with a warning. For XLSX, when the sum over all picture references exceeds it, the file is read in streaming mode, which reads each picture only once (no merged-cell information). An HWP whose number of internal records exceeds this value / 128 (about 4.1 million by default) is also rejected, with a message saying the document has too many internal records |
| `max_chars` | 500,000 | Character limit (image descriptions included). When exceeded, conversion stops with `exceeded=True` and a warning. `None` means no limit |
| `max_pages` | none | Only the first N pages of a PDF |
| `pdf_time_budget` | 60 s | Time budget for PDF structure analysis. When exceeded, the remaining pages are read by plain pdfium extraction (no table or heading detection; the text is preserved) |
| `max_sheet_cols` | 100 | Column limit per sheet (a warning when exceeded). Counted from the leftmost column that has a value; for large XLSX read in streaming mode, XLSX with formulas that have no value, Excel 2003 XML and CSV it is counted from column A. The number of rows is not limited — the only size limit is `max_chars` |
| `include_speaker_notes` | `True` | Speaker notes (PPTX, ODP, PPT) |
| `include_hidden_sheets` | `False` | Hidden sheets (XLSX, XLS, ODS) |
| `include_headers_footers` | `False` | Headers and footers of DOCX, ODT, RTF, DOC, HWP and HWPX (usually noise). HWP and HWPX headers that hold words are written even when off — Hangul forms put their number and title there — without their pictures |
| `include_footnotes` | `True` | Footnotes as `[^n]: …` at the end of the document (PDF: not applied — its notes stay at the bottom of each page) |
| `keep_links` | `True` | Hyperlinks as `[text](url)` (http, https and mailto only). `False`: only the link text |
| `keep_emphasis` | `True` | Bold and italic as `**bold**` / `*italic*`. `False`: plain text |
| `remove_repeated_page_lines` | `True` | Remove repeated PDF page headers, footers and page numbers |
| `page_markers` | `True` | Page-boundary comments (`<!-- Page N -->`) between the pages of a PDF and of a multi-page TIFF |
| `extract_images` | `True` | Image extraction. `False` ignores images completely: pictures inside a document leave no placeholder (HTML, mail and EPUB pictures that have alt text keep `[Image: alt text]`), and an image file uploaded on its own is not read (empty result plus a warning). However, OLE objects whose content is not read (a pasted Excel sheet and the like) still leave an `[OLE object]` placeholder and a warning (DOCX, PPTX, HWP, HWPX) |
| `decode_images` | `True` | Picture decoding. `False` is for text only: no picture is decoded, downscaled or returned (`images` is empty), and each picture inside a document keeps the placeholder a conversion without description gives it — `[Image: alt text]`, `[Image]`, nothing for a decoration, a repeated logo or a scanned page — told from the picture's header. PDF pictures and pages, which would have to be drawn first, and an image file uploaded on its own are read as with `extract_images=False`. Use it when only the text is wanted: the alt text authors give pictures stays, at a fraction of the time and memory |
| `max_images` | none | Number of images to describe per document (cost cap). Without it, all are described |
| `min_image_side` | 64 | Images smaller than this (px) are discarded |
| `repeated_image_threshold` | 3 | A picture repeated this many times is treated as a logo and discarded |
| `image_max_side` | 2048 | Long side (px) of images sent to the vision model |
| `pdf_render_scale` | 2.0 | PDF rendering scale (2.0 = 144 dpi) |

## `OpenAIImageDescriber`

```python
OpenAIImageDescriber(model=None, *, model_name="gpt-5.6-terra", detail="high", timeout=120.0, max_retries=2)
```

Requires the optional extra: `pip install "opendockit[openai]"`.

- `model`: any LangChain chat model built by the caller. Its own settings (key, retries, timeout) are used. When omitted, a `ChatOpenAI` for `model_name` is created, which reads `OPENAI_API_KEY` from the environment.
- `model_name`: the model to create when `model` is not given (default `describe.DEFAULT_MODEL`, `gpt-5.6-terra`).
- `detail`: `"low"` is cheaper but cannot read small text or tables.
- `timeout` (seconds) and `max_retries`: used only for the model the describer creates itself, that is, when `model` is not passed.

The number of concurrent calls is the `max_concurrency` argument (default 8) of `aconvert`, `IsolatedConverter.convert` and `describe_images`.

To use a different vision model, implement the `ImageDescriber` protocol; see [images.md](images.md#using-another-vision-model).

## Environment

| Variable | Effect |
| --- | --- |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Read by `OpenAIImageDescriber()` when no model is passed in |
| `OPENDOCKIT_SOFFICE` | Path of the LibreOffice program used for `.doc` / `.ppt`. Empty: never start LibreOffice. A path that is not a file also switches the bridge off — there is no fallback to an installed LibreOffice |

## Option validation

Values are checked when `ConvertOptions` is created.

- A negative limit, or a meaningless 0 (`max_pages`, `max_sheet_cols`, `max_file_bytes` and so on), raises `ValueError`.
- For `max_chars`, `max_images`, `min_image_side` and `pdf_time_budget`, 0 means "allow none" and is accepted.
- `None` means "no limit" and is accepted for `max_chars`, `max_pages`, `max_images` and `pdf_time_budget`.
- A value of the wrong type — `None` for any other limit, `2.5`, `"100"`, `True` — raises `TypeError`.
- The switches (`page_markers`, `extract_images`, `decode_images`, `keep_links`, `keep_emphasis`, `include_speaker_notes`, `include_hidden_sheets`,
  `include_headers_footers`, `include_footnotes`, `remove_repeated_page_lines`) accept only `True` and `False`. Anything else —
  `0`, `1`, `None`, `"false"` — raises `TypeError`: a string would always count as true, and a number hides a mix-up with a limit.
- A whole number written as a float (`max_chars=5e5`) is accepted as an integer.
- NaN and infinity (`float("nan")`, `float("inf")`) raise `ValueError`. To remove the time limit use `pdf_time_budget=None`.

## Exceptions

All exceptions are subclasses of `ConversionError`; branch on `code`. The messages are fixed wording that can be shown to end users as is, and never contain document content or parser error text. A failed image description is a warning, not an exception.

| Exception | `code` | When |
| --- | --- | --- |
| `UnsupportedFormatError` | `unsupported_format` | Legacy .doc and .ppt when LibreOffice is not installed, switched off or cannot be started; HWP 3.x, Hangul XML (.hml), single-file ODF XML, other OpenDocument kinds (drawing, formula, database), .xlsb, SVG, Word XML, DOCX / PPTX / XLSX saved as Strict Open XML (the message advises saving again in the normal format), unknown binary |
| `EncryptedDocumentError` | `encrypted_document` | Password-protected PDF, Office (`.doc` / `.ppt` included), OpenDocument and HWP documents, an EPUB with DRM, a ZIP-based document (DOCX, PPTX, XLSX, HWPX, OpenDocument, EPUB) protected with a ZIP password (a protected XLS that Excel saved with its default password VelvetSweatshop is read normally), encrypted mail (S/MIME, PGP), an HWP distribution document whose body is encrypted when it has no preview either |
| `DocumentTooLargeError` | `document_too_large` | File size or decompressed size over the limit; the conversion ran out of memory, or the time or memory limit of `IsolatedConverter` was exceeded or its worker died ("too large or too complex"); a `.doc` / `.ppt` LibreOffice did not finish within 60 seconds |
| `CorruptedDocumentError` | `corrupted_document` | Empty file, broken structure, internal parser error, an image file that cannot be decoded (uploaded on its own), a `.doc` / `.ppt` LibreOffice could not convert |
| `ConversionError` | `conversion_failed` | Not a property of the document: the temporary files of a `.doc` / `.ppt` conversion could not be written or read (a full disk, a read-only temporary folder) |

```python
from opendockit import ConversionError, convert

try:
    result = convert(data, filename)
except ConversionError as exc:
    print(exc.code, str(exc))
```
