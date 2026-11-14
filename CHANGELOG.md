# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.3] - 2026-11-14

A review of the whole source for defects. Ordinary documents convert as before (the sample outputs in `examples/` are
unchanged); what changed is what happens with damaged and hostile files.

### Security

- **Format detection** no longer takes time quadratic in the file size on a file with `%PDF-` after its first byte (40 KB
  took 5 seconds; every upload passes through this check).
- **PDF**: a stream compressed twice (`/Filter [/FlateDecode /FlateDecode]`) is charged against `max_uncompressed_bytes` at
  every stage. A 1.1 KB file used to inflate to 200 MB under a 10 MB limit.
- **Images**: pictures inside documents are opened with a fixed list of raster formats, so an EPS no longer reaches Pillow's
  Ghostscript plugin; a progressive JPEG over the pixel limit is refused instead of decoded (1.5 MB on disk took 946 MB).
- **XLSX**: row numbers the file skips cost nothing (a 14 KB file made ten million empty rows, 2.6 GB), and the streaming
  checks cannot be avoided by storing a sheet outside `xl/worksheets/`.
- About forty places where a small crafted file took tens of seconds (regular expressions that backtracked, work repeated
  for every reference to the same part, searches that rescanned the page) now take time proportional to the input, in PDF,
  HTML, mail, spreadsheets, DOCX, PPTX, ODF and HWPX. The decision records list each one, with what remains.

### Fixed

- **PDF**: the first line of a wrapped cell in a borderless table at the top of every page was deleted as a repeated header
  in documents of three or more pages, and the table fell apart; on two-column pages such a table could come out twice.
- **PDF**: a document was rejected when pdfium could not open one page, and a dense page was charged twice against the
  decompression limit; one unreadable annotation dropped every form value of its page.
- **HTML**: an element with `backface-visibility:hidden` was removed as hidden; an outer table took the caption of a table
  nested in a cell; a `DOCTYPE` inside the body came out as text; `data:` pictures written with capitals were skipped.
- **Mail**: a subject or attachment name that is not a decodable encoded word rejected the whole mail.
- **DOCX**: a header shared by several sections was rendered once per section (`1. Company … 4. Company`); a `gridSpan` of 0
  lost the next cell; documents without a body, with generically typed style parts or with a field begun inside a
  superscript were rejected.
- **Spreadsheets, charts, HWP, ODF**: single values no longer reject the whole document (a large negative elapsed time, a
  date past the calendar, attributes of thousands of digits, a surrogate in an HWP auto number, `ss:Index="0"`).
- **`IsolatedConverter`**: conversions under way when `aclose()` is called end with the closed error instead of "too large",
  and the pools are shut down once; the converter can be used from one `asyncio.run` after another and after a fork.
- Text behind a byte order mark that had to be decoded with replacement characters now gives the usual warning.

### Changed

- A chart or section referenced several times in one PPTX, DOCX or HWPX is written in full once; later references give the
  chart's title line.
- Number formats longer than Excel allows (255 characters for charts, 1,024 for cells) are not applied.
- The warning about detached package parts names only plain part names, at most four; attachment names in the mail warning
  are kept on one line and cut at 100 characters.
- A Hancom equation with a repeated script on one term (`x^2^3`) keeps the original script instead of LaTeX that does not
  compile.
- The release workflow runs the tests before it builds.
- The extractor modules are split by topic: no module is longer than 1,300 lines (`pdf.py` had 9,400). The public API and the
  output are unchanged. Code that imported internals must follow them: `extractors._shared` is gone, and XLS, Excel 2003 XML
  and CSV are read by `extractors.xls`, `extractors.xml_spreadsheet` and `extractors.csv_text`.

## [0.3.2] - 2026-11-13

### Added

- **`opendockit-mcp` on PyPI**: a launcher package with no code of its own, so that the MCP server starts with
  `uvx opendockit-mcp`. It installs `opendockit[mcp]` at the same version. `pip install "opendockit[mcp]"` and the
  `opendockit-mcp` command it brings work as before.
- The MCP server is listed in the [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.hyeonsang010716/opendockit`.

## [0.3.1] - 2026-11-13

### Added

- **Hermes Agent plugin** (`hermes-plugin/`): a portable Agent Plugins v1 package that gives Hermes the `opendockit-mcp`
  tools and a skill for reading long documents in parts. It is not part of the Python package.

### Fixed

- **MCP server**: it no longer announces prompts and resources, which it does not have. Clients that build the agent's
  tool list from the announcement (Hermes adds four tools per server) showed tools that could only return empty lists.

## [0.3.0] - 2026-11-13

### Added

- **LangChain**: `opendockit.integrations.langchain.OpenDocKitLoader` and `OpenDocKitParser`
  (`pip install "opendockit[langchain]"`).
- **LlamaIndex**: `opendockit.integrations.llama_index.OpenDocKitReader` (`pip install "opendockit[llamaindex]"`), usable
  as a `file_extractor` of `SimpleDirectoryReader`.
- **MCP server**: `opendockit-mcp` (`pip install "opendockit[mcp]"`) lets an agent read local documents as Markdown, whole
  or by page / slide / sheet, with a bounded answer and an optional `--root` restriction.
- All three give one piece per file or one per page / slide / sheet (`mode="sections"`) with the same metadata.
- Korean and Simplified Chinese READMEs.

### Fixed

- `IsolatedConverter`: an idle worker no longer outlives a server process that was killed (SIGKILL, the out-of-memory
  killer). It used to stay for ever, together with multiprocessing's fork server and resource tracker.

## [0.2.0] - 2026-11-13

### Added

- **OpenDocument** (`.odt`, `.ods`, `.odp`), read directly from the package: headings, lists, merged cells, footnotes,
  charts, formulas as LaTeX, several tables per sheet, speaker notes.
- **RTF**, with a built-in parser: legacy code pages (CP949 and others), tables with merged cells, lists, footnotes,
  pictures.
- **EPUB**: chapters in reading order, pictures stored in the book, title and author metadata.
- **Word 97-2003 (`.doc`) and PowerPoint 97-2003 (`.ppt`)** through LibreOffice when it is installed. The
  `OPENDOCKIT_SOFFICE` environment variable sets the program path; an empty value disables the bridge.
- **Links and emphasis.** `ConvertOptions.keep_links` and `keep_emphasis` (both on by default) keep hyperlinks as
  `[text](url)` and bold / italic as `**bold**` / `*italic*` in DOCX, PPTX, HTML, ODT, ODP, RTF and EPUB.
- `DocumentFormat` has the new members `odt`, `ods`, `odp`, `epub`, `rtf`, `doc` and `ppt`; code that branches on
  `result.format` will see them.
- A comparison benchmark against four other converters in `benchmarks/`.

### Changed

- **Default output changes** for documents that contain links, bold or italic: the marks and addresses are now part of
  the text and of `char_count`. Pass `keep_links=False, keep_emphasis=False` for the 0.1 output.
- RTF, ODF, EPUB, `.doc` and `.ppt` files are no longer rejected with `UnsupportedFormatError` (`.doc` / `.ppt` still are
  when LibreOffice is not installed).

### Fixed

- PPTX: a text box on the notes page that holds only a field (the slide number) is no longer written into the speaker
  notes as a `<number>` line.
- Text that has the shape of an internal placeholder is no longer taken for one. Cell text imitating the nested-table
  placeholder made a table inside a table be drawn again for every copy (a document under 1 KB could run for minutes),
  such text was also counted against `max_chars` although it is never written, which could cut a table short without
  `exceeded`; and `⟦IMG:1⟧` written in an imported DOCX chunk (`w:altChunk`) was replaced by the chunk's first picture.


## [0.1.0] - 2026-11-12

First public release.

### Added

- `convert`, `convert_file` and `aconvert`: PDF, DOCX, PPTX, XLSX, XLS, Excel 2003 XML, CSV, HWP, HWPX, HTML/MHTML,
  email (`.eml`, mbox), TXT, Markdown and image files to Markdown.
- `ConversionResult` with per-page / slide / sheet sections, metadata, warnings, extracted images and a character count.
- `ConvertOptions` with a character budget (`max_chars`) that stops conversion instead of truncating its output.
- `IsolatedConverter`: conversion in disposable worker processes with memory and time limits.
- `describe_images` and `OpenAIImageDescriber` (optional `openai` extra) to describe pictures and transcribe scanned pages;
  `OpenAIImageDescriber.check()` to fail fast on a missing extra or API key.
- `opendockit` command-line tool.
- `examples/`: sample documents for every format with their converted output.
- Optional `heic` extra for HEIC/HEIF images (kept out of the default install because its wheels bundle GPL/LGPL codecs).

[Unreleased]: https://github.com/hyeonsang010716/OpenDocKit/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.3
[0.3.2]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.2
[0.3.1]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.1
[0.3.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.0
[0.2.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.2.0
[0.1.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.1.0
