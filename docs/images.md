# Image handling

OpenDocKit extracts the pictures, charts and scanned pages inside a document, filters and normalizes them, and — when you pass a describer — has a vision model turn them into text that is put back at the place where the image was. This page covers what is extracted per format, what is filtered out, how images are normalized, how to cap cost, what the model is asked, what the output looks like, and how failures are handled. Design notes are in [decisions/images.md](decisions/images.md).

## Pipeline

```
extractor ──(original bytes + placeholder ⟦IMG:<key>:n⟧)──> converter ──(filter, normalize)──> result.images ──> describer (vision model) ──> filled in place
```

Conversion and description are separate steps. Without a describer, `convert(data, filename)` never calls a vision model: images are returned in `result.images` and the text only contains `[Image]` or `[Image: alt text]`.

```python
from opendockit import OpenAIImageDescriber, aconvert, convert, describe_images

# One step
result = await aconvert(data, "report.hwp", describer=OpenAIImageDescriber())

# Two steps: convert now, describe later
result = convert(data, "report.hwp")
result = await describe_images(result, OpenAIImageDescriber())
```

The built-in describer needs the optional extra, `pip install "opendockit[openai]"`. `OpenAIImageDescriber()` reads `OPENAI_API_KEY` from the environment; `OpenAIImageDescriber(model)` accepts any LangChain chat model you have already built. Its parameters are listed in [options.md](options.md#openaiimagedescriber). On the command line, `opendockit file.hwp --describe [--model NAME]` does the same.

## Which images are extracted

| Format | Images extracted |
| --- | --- |
| PDF | Pages without text (scans), and pages of a scanned document whose text layer has no real body text (no Hangul and only sparse characters), are rendered as a **whole page** for OCR. On other pages the **picture regions are cropped** from the rendered page |
| DOCX | Pictures in the body, inside tables and in text boxes (`a:blip`, legacy `v:imagedata`), with their alt text (`descr`) |
| PPTX | Picture shapes and picture placeholders (including those inside groups) |
| XLSX, XLS | Pictures attached to a sheet (placed after the table) |
| HWP, HWPX | Pictures in the body, inside tables and in grouped objects (`BinData`) |
| ODT, ODS, ODP | Pictures stored in the package or written inline (`office:binary-data`), with their title or description as alt text; in a sheet they follow the table. Linked pictures are never fetched |
| RTF | The data of every `\pict` group (PNG, JPEG; WMF and EMF stay placeholders), where the picture stands. The copy kept for old readers (`\nonshppict`) is skipped |
| EPUB | Pictures stored in the book and shown by a chapter, up to 20 MB each and 64 MB per book. SVG pictures and remote pictures keep only their alt text |
| DOC, PPT | As DOCX and PPTX, from the file LibreOffice converts them to |
| HTML | Pictures embedded as `data:` URIs, and picture parts saved inside MHTML. **External URLs are never fetched** (SSRF prevention) — only the alt text is kept |
| MD | Pictures embedded as `![alt](data:image/…;base64,…)` or `<img src="data:image/…;base64,…" alt="alt">`. Inside code blocks and inline code they are left untouched, whether line endings are LF, CRLF or CR. Non-base64 data: URIs and SVG keep only the alt text, as in HTML. External URLs are never fetched |
| Image files | JPG, PNG, WEBP, GIF, BMP, TIFF, AVIF and HEIC uploaded on their own (HEIC/HEIF needs `pip install "opendockit[heic]"`; without it such a file is rejected with `unsupported_format` and the install hint). A multi-page TIFF (fax or scan bundle) gives one image per page, up to 1,000 pages. When the pixel total of the pages exceeds about 1.79 billion (about 200 color A4 pages at 300 dpi), only the leading pages are read and a warning is added |

## Filtering before the vision model

The following are not sent to the vision model:

- images whose short side is under 64 px (icons, bullets);
- images with an aspect ratio above 20:1 whose short side is under 256 px (divider lines);
- a picture repeated 3 or more times in the document (logos, backgrounds);
- duplicates of an identical picture. Below the repetition threshold, the picture is described once and the same description is inserted at every place it appears.

The thresholds are `min_image_side` and `repeated_image_threshold` in [`ConvertOptions`](options.md).

## Normalization

- Every image is re-encoded as PNG (when it has transparency) or JPEG and its long side is reduced to 2048 px (`image_max_side`). OpenAI downsizes images anyway, so anything larger only slows the upload.
- EXIF rotation is corrected.
- Very long images such as product detail pages and long screen captures (long side > short side × 4 and long side > 4096 px) lose their text when shrunk as a whole. For these, tiles cut along the long side are also produced (`DocumentImage.tiles`, up to 8), and `OpenAIImageDescriber` sends them in order within a single query.

## Cost cap

By default there is no limit: every image is described (a scanned PDF costs one call per page). To bound cost, set `max_images` to a number. When there are more images than the cap, whole-page images of scanned pages are chosen first (they are the only content of their page); the rest stay as `[Image]` placeholders and a warning is added.

## Prompts by image kind

The instruction depends on the kind of image (`ImageKind`, in `describe.py`).

- **Figure** (a picture inside a document): a one-line summary first; text is transcribed verbatim; a chart becomes a table of its values; a diagram is described by its connections.
  - If the picture is decoration such as a logo, the model answers `SKIP` and the picture is removed from the text. However, if the author supplied alt text, it stays as `[Image: alt text]`.
  - When a "no information" answer comes back (`SKIP`, `(illegible)`, "empty image"), one more confirmation query that only transcribes text is sent, and the picture is removed only when both find nothing. Real models occasionally answer "illegible" for a picture of perfectly clear text. Only in this case is there one extra call.
  - If the first answer was a description that did read the picture (it overlaps with the text transcribed by the confirmation query), the first answer is kept; text that the first answer lacks is appended after it.
- **Office auto-generated alt text** ("…이(가) 표시된 사진 / 자동 생성된 설명", "Description automatically generated") is not treated as author alt text: it is neither sent to the describer nor left in the text.
- **Scanned page**: the content is transcribed in full.
- **Standalone image file**: description and text transcription together.
- **Context**: the 400 characters of body text just before the picture, and the alt text, are sent along as context.

## Output shape

- A picture is inserted as a `> [Image] summary` block quote.
- A picture inside a table cell is folded onto one line (`[Image] …<br>…`).
- A scanned page is inserted as body text, marked with `<!-- Text read from image -->`.

## Failure and retry

**Conversion succeeds even when description fails.** An image whose vision-model call failed is left as its placeholder text and a warning is added (`DocumentImage.failed` is set). Calling `await describe_images(result, describer)` again on the returned result retries **only the failed images**, and the warning disappears once they succeed.

Description also respects the character limit: after each image finishes the length is counted again, and the remaining calls are cancelled once the limit is certainly exceeded. See [server.md](server.md#character-limit).

**Other OpenAI-compatible endpoints.** `OpenAIImageDescriber()` builds a `langchain_openai.ChatOpenAI`, which reads
`OPENAI_API_KEY` and `OPENAI_BASE_URL`. Pointing `OPENAI_BASE_URL` at an Azure OpenAI `https://<resource>.openai.azure.com/openai/v1/`
endpoint with the Azure key works without code changes (that is how the live tests were run); `model_name` is then the Azure
deployment name. For anything else, build the chat model yourself and pass it in: `OpenAIImageDescriber(model)`.

**Setup problems look like failures too.** Because every failed call is reported as a warning on that image, a missing
`opendockit[openai]` extra or a missing `OPENAI_API_KEY` also shows up only as "could not be described". Call
`OpenAIImageDescriber().check()` once at startup (inside a running event loop) to make such a problem raise immediately.
The `opendockit --describe` command does this and exits with an error message.

## Using another vision model

Implement the `ImageDescriber` protocol and pass it as `describer`:

```python
from opendockit import DocumentImage, aconvert

class MyDescriber:
    async def describe(self, image: DocumentImage) -> str:
        ...  # image.data, image.mime_type, image.kind, image.alt, image.context, image.tiles

result = await aconvert(data, "report.pdf", describer=MyDescriber())
```

## Privacy

> Images inside documents — including scanned ID cards and signatures — are sent to OpenAI (or to whichever model your describer calls). If your service handles sensitive documents, check your operating policy first.
