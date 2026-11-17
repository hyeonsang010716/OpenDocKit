# OpenDocKit for Claude Code

Lets [Claude Code](https://code.claude.com) read the documents on your machine as Markdown: Word, PowerPoint, Excel,
CSV, **HWP / HWPX**, OpenDocument, RTF, EPUB, HTML, email and PDF. The Read tool opens PDFs and images; this opens
the rest, and keeps tables in rows and columns, with merged cells repeated in every row they cover. A long document
can be read one page, slide or sheet at a time, so it does not fill the context.

The plugin bundles two things around the MCP server of [OpenDocKit](https://github.com/hyeonsang010716/OpenDocKit)
(MIT):

- `.mcp.json`, the `opendockit` MCP server, started with every session while the plugin is enabled.
- `skills/opendockit/`, which tells Claude when to use which tool, how to read a long document in parts, and what a
  scanned page or a warning means.

## Install

```text
/plugin install opendockit --marketplace hyeonsang010716/OpenDocKit
```

On Claude Code older than 2.1.275, add the marketplace first:

```text
/plugin marketplace add hyeonsang010716/OpenDocKit
/plugin install opendockit@opendockit
```

Then start a new session and ask Claude to read a file:

```text
Summarise ~/Documents/quarterly-report.hwp and list the figures in its tables.
```

No account and no API key.

## What you get

| Tool | What it returns |
| --- | --- |
| `document_outline(path)` | Format, title, size, and the numbered pages / slides / sheets with their titles and lengths, without the text |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of the pages / slides / sheets asked for (`"3"` or `"2-5"`), followed by the converter's warnings |

Claude Code names them `mcp__plugin_opendockit_opendockit__document_outline` and
`mcp__plugin_opendockit_opendockit__convert_document`. Both are marked read-only.

One call returns at most `max_chars` characters of the document. A longer one is cut with a note that tells Claude to
read the outline and ask for sections, which the bundled skill teaches it to do first.

## What it does on your machine

- **Runs a local process.** `uvx --python ">=3.12" --exclude-newer 2026-11-17T05:44:00Z --from "opendockit[mcp]==0.3.12" opendockit-mcp`,
  over stdio, for as long as the session lasts. Each file is converted in a worker process that is ended after 2
  minutes, or on Linux at 2 GB.
- **Downloads once.** The first start fetches `opendockit` 0.3.12 and its dependencies from PyPI into uv's cache, and
  Python 3.12 or newer if the machine has none. After that it starts from the cache. It needs
  [uv](https://docs.astral.sh/uv/) on the `PATH`.
- **Reads, never writes.** The tools only read the file they are given. No network access during conversion and no
  vision model: pictures stay `[Image]` placeholders and scanned pages have no text.
- **Reads any file you can read**, unless you restrict it. The server inherits the environment of Claude Code: export
  `OPENDOCKIT_MCP_ROOTS` in the shell you start `claude` from (folders separated like `PATH`), and it reads only files
  under those folders, links resolved:

  ```bash
  export OPENDOCKIT_MCP_ROOTS="$HOME/Documents:$HOME/Downloads"
  ```

## Version pinning

`.mcp.json` pins `opendockit[mcp]==0.3.12` and resolves its dependencies as of 2026-11-17, so the plugin installs the
same code until this file changes. The plugin's version is the OpenDocKit release it starts.

## Limits

- Scanned PDFs and photos: no OCR. Claude's Read tool can look at such a page as an image.
- Legacy `.doc` and `.ppt` need LibreOffice. On Windows, HTML, email and EPUB are not supported.
- The server reads local files only, not URLs.

## License

MIT, like OpenDocKit. This is an independent community plugin, not affiliated with Anthropic.
