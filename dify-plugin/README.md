# OpenDocKit

Read **HWP and HWPX** (Hangul) documents in Dify, along with PDF and Office files: one tool turns an uploaded file into
clean Markdown whose tables keep their rows and columns.

Dify's built-in extractors do not read HWP or HWPX, the formats most Korean public-sector and business documents are
written in. This plugin does, with no API key and no outside service.

## Features

- **HWP 5.x and HWPX**, plus PDF, DOCX, PPTX, XLSX, XLS, CSV, ODT, ODS, ODP, RTF, EPUB, HTML and EML.
- **Tables stay tables.** A merged cell is repeated in every row it covers, so each row still makes sense on its own.
- **Headings, lists, footnotes and equations** are kept as Markdown (`#`, `-`, `[^1]`, `$…$`).
- **One document per page, slide or sheet** in `documents`, with its number in the metadata, for chunking and
  citations.
- **Offline and contained.** Nothing leaves your Dify. Each file is converted in a worker process of its own, limited
  to 2 GB of memory and 2 minutes, so a file built to blow up fails on its own. A runtime that cannot run worker
  processes at all converts the file in the plugin process, without those limits.

## Use it

1. Add a **File** variable to the **Start** node of a Workflow or Chatflow. HWP and HWPX are not in the **Document**
   list: under **Upload File Types**, choose **Other file types** and add `.hwp` and `.hwpx`.
2. Add the tool **OpenDocKit → Convert to Markdown** and pass it the file.
3. Use its `text` in an LLM node, or `documents` to handle each page, slide or sheet.

| Output | Contents |
| --- | --- |
| `text` | The whole document as Markdown |
| `documents` | One item per page, slide or sheet (one for a document without them, such as HWP or DOCX), each with `page_content` and `metadata`: `source` (the file name), `format`, the document's own fields such as `title` and `author`, `section_kind`, `section_index` and `section_title` for a page, slide or sheet, and `warnings` and `exceeded` when they apply |
| `warnings` | What was skipped or approximated, in plain sentences |

No setup or credentials are needed.

## Limits

- **No OCR.** Scanned pages and images have no text to extract; the tool fails with "No text could be extracted from
  this file".
- **Legacy `.doc` and `.ppt`** are not read: they need LibreOffice, which the plugin runtime does not have.
- **Knowledge base uploads:** Dify does not accept `.hwp` or `.hwpx` files there yet, so use the tool in Workflows and
  Chatflows.
- **Self-hosted Dify:** if the tool fails with "Invalid file URL" or a connection error, the plugin runtime cannot
  download the uploaded file from Dify. Set `FILES_URL`, or `INTERNAL_FILES_URL` for the address the plugin runtime
  uses (for example `http://api:5001` with Docker Compose), and restart.

Tested with Dify 1.19.0.

## Privacy

The file is converted inside your Dify's plugin runtime and nothing is sent anywhere else. See
[PRIVACY.md](PRIVACY.md).

## About

Built on [OpenDocKit](https://github.com/hyeonsang010716/OpenDocKit), an MIT-licensed Python library. Questions and
documents that come out wrong are welcome as [issues](https://github.com/hyeonsang010716/OpenDocKit/issues).
