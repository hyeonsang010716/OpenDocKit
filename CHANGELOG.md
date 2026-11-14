# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.4] - 2026-11-14

A second review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **Reading files**: `convert_file`, the CLI and the LangChain and LlamaIndex adapters read a pipe or a device (`/dev/zero`)
  only up to `max_file_bytes`; before, the size check came after reading it to the end. `IsolatedConverter` refuses an
  oversized or empty input before it is sent to a worker.
- **XLS**: merge ranges written back to front no longer cancel out the merged-cell limit (a 461 KB file took 16 seconds).
- **PDF**: a small crafted file no longer holds one page for tens of seconds through thousands of FreeText annotations, one
  long letter-spaced line, thousands of check-box shapes, faint watermark characters, shading beside a ruled table, a long
  zebra table, many highlighted lines, a borderless "table" of a thousand columns, a long paragraph or stacked text drawn
  many times. The running character limit also counts sideways text and form values, so such a document stops at the limit.
- **PDF layout analysis**: pdfminer's grouping of lines into text boxes and of text boxes into reading order is bounded per
  page (the page itself and every form drawn on it); past the bound the page is read in pdfminer's plain order with the
  existing "read without text-box reading-order analysis" warning. A 20 KB page took 62 seconds, a 68 KB page two minutes.
  A single huge character, or two characters far apart, no longer makes pdfminer fill millions of index cells.
- **XLSX**: scanning a sheet for formula results, the shared strings and the sheet parser no longer keep every row in memory
  (a 600,000-row sheet took 948 MB more), and a sheet without `<dimension>` is no longer parsed twice to measure it.
- **Mail**: mail forwarded inside forwarded mails is limited to 100 levels, counted along the structure the mail parser
  follows (part headers quoted in a body or in a text attachment are text).
- **Release**: the PyPI publish action is pinned to a commit.

### Fixed

- **PDF**: a row of a borderless table that pdfminer grouped differently came out in two tables, splitting the table in
  two, and the header of the lower of two stacked borderless tables also ended the table above as a row of its own;
  columns to the right of a nested table were shifted; FreeText labels were lost on pages whose MediaBox does not start
  at 0,0 or that are rotated, and image clips were ignored when the MediaBox starts right of 0; text at a non-finite
  position rejected the whole document or leaked off-page copies into the body; a ruled table continued on the next page
  without its header lost the text of a merged cell left open at the page end.
- **DOCX/PPTX**: one damaged picture member rejected the whole document; an empty equation wrote `$$`, which swallowed the
  following text, and `$ % # { } \` typed in an equation were read as LaTeX; a footnote reference with no note left a
  dangling `[^1]`; a struck superscript ending in a space gave `~~^2 ~~`; a comment quoted moved-away, deleted or hidden text.
- **Spreadsheets**: an integer too large for a float rejected the whole XLSX; an unclosed quote in a small CSV swallowed the
  rest of the file without a warning; a large hyperlink range forced streaming and lost merges; `1E+400` in Excel 2003 XML
  became `inf`; a literal between decimal places (`0.0\E+0`) was lost.
- **HTML**: `visibility:visible` content inside a `visibility:hidden` element, and pages hidden with
  `<body style="visibility:hidden">` until a script runs, came out empty; a page title written over several lines broke the
  heading; a `# comment` in a code block kept the title out; `~~`, `^` and `_` marks were put inside code; an empty list item
  holding a nested list lost its indentation; `rowspan="0"` spanned one row; alt text could close `[Image: …]` and form a
  link; struck elements next to each other (`<del>a</del><del>b</del>`) gave `~~a~~~~b~~`, which is not strikethrough for a
  renderer.
- **Mail**: one HTML-only mail left a whole `.txt` mailbox undecoded; an HTML attachment was read instead of the body of an
  MHTML-shaped mail; a forwarded mail that is itself a forward lost its body; meeting invitations took the reminder's title and
  attendees, and a recurrence rule ending in `;` stayed raw; short broken UTF-8 could be read as UTF-16; a multipart mail
  with an empty boundary (`boundary=""`) was rejected, as was a mail whose parts were counted deeper than they nest.
- **ODF**: hidden text (hidden spans, hidden text fields and paragraphs, hidden conditional sections) came out as body text;
  a nested list with its own style never restarted its numbering; a heading's number was glued to it (`# 1.Intro`), and a
  heading in a numbered list was not counted; `text:continue-list` now continues the list it names.
- **HWP/HWPX**: a broken DocInfo stream, or an HWPX member with an unknown compression method or the encryption flag,
  rejected the whole document instead of only that part.
- **RTF**: a code page that does not read ASCII as ASCII (EBCDIC) turned the text into noise; a `\u` fallback that a writer
  counted in characters left half a double-byte character.
- `detect_format` called on its own raises `CorruptedDocumentError` instead of library exceptions.
- `describe_images` no longer re-renders the document every few pictures when many are judged decorative near `max_chars`.

### Changed

- HWP documents protected with DRM or a public certificate raise `EncryptedDocumentError`.
- The MCP server keeps its last four results (by file content), so `document_outline` followed by `convert_document` by
  sections converts the file once.
- `detect_format` raises `TypeError` for content that is not bytes, bytearray or memoryview, and for a file name that is
  not a `str`, as `convert` does.

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

[Unreleased]: https://github.com/hyeonsang010716/OpenDocKit/compare/v0.3.4...HEAD
[0.3.4]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.4
[0.3.3]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.3
[0.3.2]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.2
[0.3.1]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.1
[0.3.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.0
[0.2.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.2.0
[0.1.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.1.0
