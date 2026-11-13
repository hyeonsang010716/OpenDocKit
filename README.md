<h1 align="center">OpenDocKit</h1>

<p align="center">
  <b>Turn any document into clean, LLM-ready Markdown.</b><br>
  PDF · DOCX · PPTX · XLSX · HWP/HWPX · ODF · RTF · EPUB · HTML · email · images — one function, no GPU, MIT licensed.
</p>

<p align="center">
  <a href="https://pypi.org/project/opendockit/"><img alt="PyPI" src="https://img.shields.io/pypi/v/opendockit"></a>
  <a href="https://pypi.org/project/opendockit/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/opendockit"></a>
  <a href="https://github.com/hyeonsang010716/OpenDocKit/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/hyeonsang010716/OpenDocKit/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/hyeonsang010716/OpenDocKit/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

<p align="center">
  <b>English</b> ·
  <a href="https://github.com/hyeonsang010716/OpenDocKit/blob/main/README.ko.md">한국어</a> ·
  <a href="https://github.com/hyeonsang010716/OpenDocKit/blob/main/README.zh-CN.md">简体中文</a>
</p>

---

```python
from opendockit import convert_file

result = convert_file("quarterly-report.hwp")

result.text        # Markdown: headings, tables, lists, footnotes, LaTeX equations
result.sections    # per page / slide / sheet — for chunking and citations ("page 3")
result.warnings    # what was skipped or approximated, in plain sentences
```

## Why OpenDocKit

Most converters handle the happy path. OpenDocKit is built for the files people actually upload.

- **Structure, not just text.** Merged table cells, multi-column PDFs, borderless tables, nested lists, footnotes,
  speaker notes, native charts as data tables, SmartArt as nested lists, Word / PowerPoint / Hancom equations as LaTeX,
  links and bold / italic as Markdown.
- **First-class Korean documents.** A from-scratch HWP 5.x parser, HWPX, CP949 detection, NFC normalization, and the
  form-heavy tables Korean offices produce. Most general-purpose converters cannot read these at all.
- **Detects by content, not extension.** An HTML table saved as `.xls`, a CSV with a title block, an MHTML page named
  `.doc` — each is read as what it really is, and a warning tells you.
- **Safe on untrusted uploads.** Zip bombs, runaway merged-cell counts, recursive PDF objects and multi-gigabyte images
  are bounded. `IsolatedConverter` runs every file in a disposable worker process with memory and time limits.
- **Images in place.** Pictures, charts and scanned pages are extracted, filtered, and — if you plug in a vision
  model — described right where they appeared in the document.
- **A hard character budget.** `max_chars` *stops* work instead of truncating afterwards, so a file that will be
  rejected anyway costs neither parsing time nor vision-model calls.
- **Permissive by default.** MIT licensed; everything a plain `pip install opendockit` brings in is MIT / BSD /
  Apache-style (no PyMuPDF, no pyhwp), with no ML models and no GPU. The only copyleft code is behind the opt-in
  `heic` extra.

## Installation

```bash
pip install opendockit                # conversion only — never touches the network
pip install "opendockit[openai]"      # + image description with a vision model
pip install "opendockit[heic]"        # + HEIC/HEIF images (pulls in GPL/LGPL-licensed codec libraries)
pip install "opendockit[langchain]"   # + LangChain loader
pip install "opendockit[llamaindex]"  # + LlamaIndex reader
pip install "opendockit[mcp]"         # + MCP server for agents
```

Requires Python 3.12 or newer on Linux or macOS. Windows is not supported yet for HTML, email and EPUB
([why](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/limitations.md#platforms)).

Optional: install [LibreOffice](https://www.libreoffice.org/) (`brew install --cask libreoffice`, `apt install libreoffice`)
to read legacy `.doc` and `.ppt` files. Every other format is read by OpenDocKit itself.

## Quick start

### Convert

```python
from opendockit import convert, convert_file

result = convert_file("report.pdf")              # from a path
result = convert(data, "report.pdf")             # from bytes (the filename is only a hint)

print(result.text)
print(result.char_count, result.exceeded)        # size of the output, and whether the limit stopped it
```

`convert` is synchronous, offline and deterministic (the one exception: `.doc` and `.ppt` start a local LibreOffice
process). Pictures are collected in `result.images` and the text keeps an
`[Image]` placeholder where each one was.

### Describe images with a vision model

```python
from opendockit import OpenAIImageDescriber, aconvert

result = await aconvert(data, "report.pdf", describer=OpenAIImageDescriber())   # reads OPENAI_API_KEY
```

Charts become data tables, diagrams become lists, scanned pages are transcribed, and decorative images are dropped.
`OpenAIImageDescriber(model)` accepts any LangChain chat model, and anything with an
`async describe(image) -> str` method works as a describer. See [docs/images.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/images.md).

### Serve it

```python
from opendockit import ConversionError, IsolatedConverter

async with IsolatedConverter(workers=2) as converter:      # 2 GB and 2 minutes per file by default
    try:
        result = await converter.convert(data, filename)
    except ConversionError as exc:
        print(exc.code, exc)                               # stable code + a message safe to show users
```

One bad file ends one worker; your server and the next upload carry on. In a script, run this under
`if __name__ == "__main__":` — the workers import the main module again. See [docs/server.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/server.md).

### Command line

```bash
opendockit report.pdf                 # Markdown on stdout, warnings on stderr
opendockit report.pdf --json          # sections, metadata, warnings, images
opendockit report.pdf --count         # just the character count
opendockit scan.pdf --describe        # describe images with a vision model (uses OPENAI_API_KEY)
```

### LangChain, LlamaIndex and MCP

```python
from opendockit.integrations.langchain import OpenDocKitLoader
from opendockit.integrations.llama_index import OpenDocKitReader

docs = OpenDocKitLoader("report.hwp", mode="sections").load()       # one Document per page / slide / sheet
docs = OpenDocKitReader(mode="sections").load_data("report.hwp")
```

```bash
opendockit-mcp --root ~/Documents     # MCP server: Claude, Cursor and other agents read your documents as Markdown
```

Every piece carries its page, slide or sheet number in the metadata, for citations. See [docs/integrations.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/integrations.md).

## Supported formats

| Format | Sections | Highlights |
| --- | --- | --- |
| PDF | page | reading order across columns, ruled and borderless tables, form field values, headings from font size, repeated header/footer removal, scanned pages via vision model |
| DOCX | document | heading styles, list numbering, merged cells, charts → tables, SmartArt, footnotes/endnotes, equations → LaTeX, comments |
| PPTX | slide | on-screen order, bullet levels, tables, charts → tables, SmartArt, equations, speaker notes |
| XLSX · XLS · Excel 2003 XML · CSV | sheet | several tables per sheet, merged cells, number and date formats, charts, cell comments |
| HWP · HWPX | document | outline headings, numbering, merged cells, captions, footnotes, equations → LaTeX |
| ODT · ODS · ODP | document / sheet / slide | read directly from the package: headings, lists, merged cells, charts, formulas → LaTeX, speaker notes |
| RTF | document | built-in parser: legacy code pages (CP949), tables with merged cells, lists, footnotes, pictures |
| EPUB | document | chapters in reading order, pictures stored in the book, metadata |
| DOC · PPT | document / slide | converted through LibreOffice when it is installed, then read like DOCX / PPTX |
| HTML · MHTML | document | tables with rowspan/colspan, layout tables unwrapped, MathML → LaTeX, hidden elements removed |
| Email (`.eml`, mbox) | document | headers, HTML or plain body, forwarded messages, calendar invites |
| TXT · Markdown | document | encoding detection (UTF-8, CP949, UTF-16, Windows-1252) |
| Images | document / page | JPEG, PNG, WebP, GIF, BMP, TIFF, AVIF, and HEIC with the `heic` extra — described by a vision model |

Legacy `.doc` and `.ppt` need LibreOffice installed; everything else is read by OpenDocKit itself. The full list of
what each format restores is in [docs/formats.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/formats.md); the honest list of what it cannot do is in
[docs/limitations.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/limitations.md).

## How it compares

<p align="center">
  <img alt="Structure checks passed: OpenDocKit 49 of 49, Docling 36, MarkItDown 31, Kreuzberg 29, Unstructured 12"
       src="https://github.com/hyeonsang010716/OpenDocKit/raw/main/benchmarks/charts/structure-checks.svg" width="720">
</p>

We ran OpenDocKit, MarkItDown, Kreuzberg, Unstructured and Docling on the same files on a standard GitHub runner
([method, raw results and how to rerun it](https://github.com/hyeonsang010716/OpenDocKit/blob/main/benchmarks/README.md)):

- **Structure** — OpenDocKit keeps the most: 49 of 49 checks such as "is the merged cell still attached to its row",
  against 36 for the next tool. The checks are ours, and 0.2.0 was written with them known; the report says what that
  means.
- **Formats** — it reads 26 of the 31 formats tested without any model or service, the most of the five, and is the
  only one that reads HWP and HWPX.
- **Not the fastest, and no OCR** — scans and images are read only through a vision model, where Docling and
  Unstructured read them offline. Kreuzberg is about thirty times faster, and legacy `.doc` / `.ppt` cost about a
  second each because they go through LibreOffice.

## Examples

[`examples/`](https://github.com/hyeonsang010716/OpenDocKit/tree/main/examples/) has one sample document per format and the Markdown OpenDocKit produces from each — with and
without a vision model — so you can judge the output before installing anything.

## Documentation

| | |
| --- | --- |
| [Formats](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/formats.md) | What is extracted from each format and how it is rendered |
| [Options](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/options.md) | `ConvertOptions`, exceptions and error codes |
| [Images](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/images.md) | Extraction, filtering, vision models, custom describers |
| [Running on a server](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/server.md) | `IsolatedConverter`, resource limits, the character budget |
| [Integrations](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/integrations.md) | LangChain, LlamaIndex and the MCP server |
| [Integration guide](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/integration-guide.md) | What an upload UI should tell its users |
| [Design](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/design.md) | Why it is built this way, and how it was verified |
| [Limitations](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/limitations.md) | Known gaps, stated plainly |
| [Decision records](https://github.com/hyeonsang010716/OpenDocKit/tree/main/docs/decisions/) | Every deliberate trade-off, with the test that pins it |

## Contributing

Contributions are welcome — bug reports with a minimal reproducing document are especially valuable.
Start with [CONTRIBUTING.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/CONTRIBUTING.md).

```bash
git clone https://github.com/hyeonsang010716/OpenDocKit.git && cd OpenDocKit
uv sync
uv run pytest -n auto -m "not timing"   # the bulk of the suite, in parallel
uv run pytest -m timing                  # tests that measure time, one at a time
uv run ruff check
```

## License

[MIT](https://github.com/hyeonsang010716/OpenDocKit/blob/main/LICENSE)
