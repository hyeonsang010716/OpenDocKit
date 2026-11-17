# Integrations

OpenDocKit's result can be handed straight to LangChain, to LlamaIndex, to Open WebUI, or to an agent through the Model Context Protocol.
Each integration is an optional extra: none of them is installed, or imported, unless you ask for it.

```bash
pip install "opendockit[langchain]"     # OpenDocKitLoader, OpenDocKitParser
pip install "opendockit[llamaindex]"    # OpenDocKitReader
pip install "opendockit[mcp]"           # the opendockit-mcp server
pip install "opendockit[open-webui]"    # the opendockit-open-webui document loader
```

They all cut a document the same way and use the same metadata, so an index built with one cites the same pages as an
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
| `section_kind`, `section_index`, `section_title` | In `sections` mode: `page` / `slide` / `sheet`, its number from 1, and its title when it has one. A page, sheet or slide without text is left out (a slide that is only a title is kept), so the numbers can have gaps |
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
`convert_file` raises; the adapters skip nothing silently. (`SimpleDirectoryReader` itself prints and skips a file that
fails unless you pass `raise_on_error=True`.) A file that converts but has no text to hand on — a scanned PDF, an empty
workbook — gives no document, and a warning naming the file is logged on the `opendockit.integrations` logger. Conversion is offline: pictures stay `[Image]` placeholders. To have
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
      "args": ["opendockit-mcp", "--root", "/path/to/documents"]
    }
  }
}
```

`opendockit-mcp` on PyPI is a launcher without code: it installs `opendockit[mcp]` at the same version, so that the
server can be started by its name. With `opendockit[mcp]` installed, the command is simply `opendockit-mcp`. The server
is listed in the [MCP Registry](https://registry.modelcontextprotocol.io) as `io.github.hyeonsang010716/opendockit`.

| Tool | What it returns |
| --- | --- |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of some of its pages / slides / sheets: `sections="3"` or `"2-5"`, the numbers `document_outline` shows. Warnings follow the text |
| `document_outline(path)` | Format, title, size, and the list of pages / slides / sheets with their titles and lengths — no text |

- **The answer is bounded.** One call returns at most `max_chars` characters of the document (100,000 unless the agent
  asks for more or the server is started with `--max-chars`), followed by a short note and the converter's warnings when
  there are any. A longer document is cut there, and the note tells the agent to read the outline and ask for `sections`.
- **Which files it may read.** Without `--root`, any file the user running the server can read. With `--root DIR`
  (repeatable; or the `OPENDOCKIT_MCP_ROOTS` environment variable, separated like `PATH`) only files under those
  folders; symbolic links are followed before the check, and an empty value is an error rather than "everything". Give it the folders the agent is meant to see.
- **One file cannot take the server down.** On Linux and macOS every conversion runs in a disposable worker process that
  is ended after 2 minutes, and on Linux also when it passes 2 GB of memory. On Windows it runs in a thread of the
  server, without those limits.
- **Read-only.** Both tools are marked read-only for the client, and neither writes anything.
- **The text of a document is data.** A document can contain sentences addressed to the agent. The server returns what
  the file says; whether the agent acts on it is the agent's and the client's business, so let it read only the folders
  you would hand it yourself.
- **Offline.** No vision model and nothing fetched: pictures and scanned pages stay placeholders, with a warning.
  Legacy `.doc` / `.ppt` need LibreOffice, as everywhere else.

### Hermes Agent plugin

[`hermes-plugin/`](../hermes-plugin) packages the same server for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
as a portable Agent Plugins v1 package: the server declaration (`mcp.json`, pinned to one OpenDocKit release) and a skill
that teaches the agent to read the outline first and long documents by `sections`. It is listed in the
[Hermes plugin catalog](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/opendockit.yaml), which installs the commit the entry pins:

```bash
hermes plugins install opendockit
hermes plugins enable opendockit
```

A commit newer than the catalog's installs from this repository: `hermes plugins install hyeonsang010716/OpenDocKit#hermes-plugin`.

The server is started without `--root`; set `OPENDOCKIT_MCP_ROOTS` in the shell or in the Hermes profile's `.env` to
restrict it. What the plugin does on the machine is listed in its [README](../hermes-plugin/README.md).

## Dify plugin

[`dify-plugin/`](../dify-plugin) is a tool plugin for [Dify](https://github.com/langgenius/dify) and its
Marketplace. Its one tool, **Convert to Markdown**, takes a file from a Workflow or Chatflow and returns the Markdown
(`text`), one document per page, slide or sheet with the metadata above (`documents`), and the `warnings`. Dify reads
no HWP or HWPX by itself; to accept them, a file input needs **Other file types** with `.hwp` and `.hwpx`.

Each file is converted in a worker process of its own (`IsolatedConverter`), since every plugin of a Dify shares one
runtime. Dify does not accept `.hwp` or `.hwpx` uploads into a knowledge base yet, so the plugin serves Workflows and
Chatflows. It is on the [Dify Marketplace](https://marketplace.dify.ai/plugin/hyeonsang010716/opendockit): in Dify, open **Plugins** and install OpenDocKit from the Marketplace. Its
[README](../dify-plugin/README.md) is the page shown there.

## Open WebUI document loader

`opendockit-open-webui` is a document loader for [Open WebUI](https://github.com/open-webui/open-webui): its
**External** content extraction engine sends every uploaded file to it, and indexes what comes back — one document per
page, slide or sheet with the metadata above, a PDF page also with `page`, so a citation says "page 3" and opens the
PDF there. Open WebUI's own loaders read no HWP or HWPX, and its slim image reads only text formats without an
external one.

```bash
pip install "opendockit[open-webui]"
export OPENDOCKIT_OPEN_WEBUI_API_KEY=$(openssl rand -hex 32)
opendockit-open-webui                  # http://127.0.0.1:8765; --host 0.0.0.0 to be reached from a container
```

In Open WebUI, **Admin Settings → Documents**: set **Content Extraction Engine** to **External**, the URL to the
loader (`http://host.docker.internal:8765` when Open WebUI runs in Docker on the same machine) and the API key to the
same key. Open WebUI calls a loader only when a key is set, and falls back to its own loaders without a word when it
is not. The same settings as environment variables of Open WebUI:

```bash
CONTENT_EXTRACTION_ENGINE=external
EXTERNAL_DOCUMENT_LOADER_URL=http://opendockit:8765
EXTERNAL_DOCUMENT_LOADER_API_KEY=<the same key>
```

Next to Open WebUI in Docker Compose, with nothing to build:

```yaml
  opendockit:
    image: ghcr.io/astral-sh/uv:python3.12-bookworm-slim
    command: uvx --from "opendockit[open-webui]>=0.3.12" opendockit-open-webui --host 0.0.0.0
    environment:
      OPENDOCKIT_OPEN_WEBUI_API_KEY: ${OPENDOCKIT_OPEN_WEBUI_API_KEY}
```

The first release with the loader is 0.3.12; pin the version you have tested (`==`) in production.

- Each file is converted offline in a worker process of its own, ended after 2 minutes or (on Linux) at 2 GB; `--workers`
  files at once (2), `--queue` more waiting (16), and a file past that is refused with 503 at once.
- Pictures keep their placeholders, alt text included; nothing is sent to a vision model, so a scanned PDF fails with
  "No text could be extracted from this file." Legacy `.doc` and `.ppt` need LibreOffice in the loader's image.
- Every failure is a fixed sentence Open WebUI shows to the user (`The file is too large (maximum 50MB).`, `The Office
  document is password-protected. …`), and the loader's log names no file. `--max-chars N` refuses longer documents
  instead of indexing their beginning only; there is no limit by default.
- The key comes from the environment only, never the command line, and is at least 16 characters. The loader listens on
  `127.0.0.1` unless told otherwise; put it on a network only Open WebUI can reach.

Design notes: [decisions/integrations.md](decisions/integrations.md).
