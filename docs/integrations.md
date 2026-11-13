# Integrations

OpenDocKit's result can be handed straight to LangChain, to LlamaIndex, or to an agent through the Model Context Protocol.
Each integration is an optional extra: none of them is installed, or imported, unless you ask for it.

```bash
pip install "opendockit[langchain]"     # OpenDocKitLoader, OpenDocKitParser
pip install "opendockit[llamaindex]"    # OpenDocKitReader
pip install "opendockit[mcp]"           # the opendockit-mcp server
```

All three cut a document the same way and use the same metadata, so an index built with one cites the same pages as an
index built with another.

## One piece per file, or per page / slide / sheet

`mode="single"` (the default) gives one piece per file, holding the full Markdown. `mode="sections"` gives one piece per
page (PDF, multi-page TIFF), slide (PPTX, ODP, PPT) or sheet (XLSX, XLS, ODS). Formats without physical units — DOCX, HWP,
HWPX, ODT, RTF, EPUB, HTML, mail, text — are one piece in either mode; split those by their headings with your
framework's Markdown splitter.

In `sections` mode a slide or a sheet starts with the heading the full text gives it (`## Slide 2: Goals`,
`## Sheet: Sales`), so the piece says what it is when it is read alone. A page has no marker in its text: its number is
in the metadata.

| Metadata key | Value |
| --- | --- |
| `source` | The path that was given |
| `format` | `pdf`, `docx`, `hwp`, … (detected from the content) |
| `title`, `author`, `page_count`, `slide_count`, `sheet_count`, `encoding`, … | What the document says about itself, when it does |
| `section_kind`, `section_index`, `section_title` | In `sections` mode: `page` / `slide` / `sheet`, its number from 1, and its title when it has one |
| `warnings` | One line per warning the converter raised (a hidden sheet left out, a scanned page without text, …). Absent when there are none |
| `exceeded` | `True` when `max_chars` stopped the conversion. Absent otherwise |

Every value is a string, an integer or a boolean, because vector stores reject anything else.

## LangChain

```python
from opendockit.integrations.langchain import OpenDocKitLoader

docs = OpenDocKitLoader("report.hwp").load()                              # one Document
docs = OpenDocKitLoader(["a.pdf", "b.xlsx"], mode="sections").load()      # one per page and per sheet
```

`lazy_load()` converts each file only when it is reached. For bytes that are not on disk, or with LangChain's blob
loaders, use the parser:

```python
from langchain_core.document_loaders import Blob
from opendockit.integrations.langchain import OpenDocKitParser

docs = OpenDocKitParser(mode="sections").parse(Blob.from_data(data, path="upload.pptx"))
```

## LlamaIndex

```python
from llama_index.core import SimpleDirectoryReader
from opendockit.integrations.llama_index import OpenDocKitReader

reader = OpenDocKitReader(mode="sections")
docs = reader.load_data("report.hwp")

# or by extension, for a whole folder
extensions = [".hwp", ".hwpx", ".pdf", ".docx", ".pptx", ".xlsx"]
docs = SimpleDirectoryReader("inbox", file_extractor=dict.fromkeys(extensions, reader)).load_data()
```

## Options and errors

Both take `options=ConvertOptions(...)` ([options.md](options.md)) — `max_chars`, `keep_links`, `include_speaker_notes`
and the rest work as they do for `convert`. A file that cannot be converted raises the same `ConversionError` subclasses
`convert_file` raises; nothing is skipped silently. Conversion is offline: pictures stay `[Image]` placeholders. To have
them described by a vision model, convert with `aconvert(..., describer=...)` yourself and build the documents from the
result ([images.md](images.md)).

When the files come from the public, convert them through `IsolatedConverter` ([server.md](server.md)) instead of
calling a loader in your server process.

## MCP server

`opendockit-mcp` lets an agent — Claude Desktop, Claude Code, Cursor, or anything else that speaks the
[Model Context Protocol](https://modelcontextprotocol.io) — read local documents as Markdown.

```json
{
  "mcpServers": {
    "opendockit": {
      "command": "uvx",
      "args": ["--from", "opendockit[mcp]", "opendockit-mcp", "--root", "/path/to/documents"]
    }
  }
}
```

With the package installed, the command is simply `opendockit-mcp`.

| Tool | What it returns |
| --- | --- |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of some of its pages / slides / sheets: `sections="3"` or `"2-5"`, counted from 1. Warnings follow the text |
| `document_outline(path)` | Format, title, size, and the list of pages / slides / sheets with their titles and lengths — no text |

- **The answer is bounded.** One call returns at most `max_chars` characters (100,000 unless the agent asks for more or
  the server is started with `--max-chars`). A longer document is cut there with a note telling the agent to read the
  outline and ask for `sections`.
- **Which files it may read.** Without `--root`, any file the user running the server can read. With `--root DIR`
  (repeatable; or the `OPENDOCKIT_MCP_ROOTS` environment variable, separated like `PATH`) only files under those
  folders; symbolic links are followed before the check. Give it the folders the agent is meant to see.
- **One file cannot take the server down.** On Linux and macOS every conversion runs in a disposable worker process with
  a 2 GB and 2-minute limit. On Windows it runs in a thread of the server, without those limits.
- **Offline.** No vision model and nothing fetched: pictures and scanned pages stay placeholders, with a warning.
  Legacy `.doc` / `.ppt` need LibreOffice, as everywhere else.

Design notes: [decisions/integrations.md](decisions/integrations.md).
