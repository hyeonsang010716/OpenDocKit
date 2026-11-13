# OpenDocKit for Hermes Agent

Lets [Hermes Agent](https://github.com/NousResearch/hermes-agent) read the documents on your machine as Markdown:
PDF, Word, PowerPoint, Excel, CSV, **HWP / HWPX**, OpenDocument, RTF, EPUB, HTML and email. Tables keep their rows and
columns, merged cells are repeated in every row they cover, and a long document can be read one page, slide or sheet at
a time, so it does not fill the context.

It is a portable [Agent Plugins v1](https://agent-plugins.org) package around the MCP server of
[OpenDocKit](https://github.com/hyeonsang010716/OpenDocKit) (MIT). One install ships:

- `mcp.json` — the `opendockit` MCP server, started for every new session while the plugin is enabled. No
  `mcp_servers` edits in `config.yaml`.
- `skills/opendockit/` — when to use which tool, how to read a long document in parts, and what a scanned page or a
  warning means.

This is an independent community plugin. It is not affiliated with Nous Research.

## Install

```bash
hermes plugins install opendockit      # from the Hermes plugin catalog
hermes plugins enable opendockit
```

Before it is listed, or to try a newer commit, install from this repository:

```bash
hermes plugins install hyeonsang010716/OpenDocKit#hermes-plugin
```

Then start a new Hermes session and ask it to read a file:

```text
Summarise ~/Documents/quarterly-report.hwp and list the figures in its tables.
```

No account and no API key.

## What you get

| Tool | What it returns |
| --- | --- |
| `document_outline(path)` | Format, title, size, and the numbered pages / slides / sheets with their titles and lengths — no text |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of the pages / slides / sheets asked for (`"3"` or `"2-5"`), followed by the converter's warnings |

Hermes exposes them as `mcp__opendockit__document_outline` and `mcp__opendockit__convert_document`. Both are marked
read-only.

One call returns at most `max_chars` characters of the document. A longer one is cut with a note that tells the agent
to read the outline and ask for sections, which is what the bundled skill teaches it to do first.

## What it does on your machine

- **Runs a local process.** `uvx --python ">=3.12" --exclude-newer 2026-11-14T00:00:00Z --from "opendockit[mcp]==0.3.0" opendockit-mcp`, over stdio, for as
  long as the session lives. `uvx` is the one Hermes manages.
- **Downloads once.** The first start fetches `opendockit` 0.3.0 and its dependencies from PyPI into uv's cache, and a
  Python 3.12 or newer if the machine has none. The `opendockit` version is pinned, and `--exclude-newer` keeps its
  dependencies at the releases that existed on that date, so every install resolves the same set.
- **Reads files, writes none.** By default the server can read any file your user can read, like the agent's own file
  tools. To restrict it, set `OPENDOCKIT_MCP_ROOTS` in your shell or in the profile's `.env` to the folders it may read
  (separated like `PATH`); files outside them, also through symbolic links, are refused.
- **No network when converting.** No telemetry, no vision model, nothing uploaded. Legacy `.doc` / `.ppt` start a local
  LibreOffice if one is installed and are refused if not.
- **One file cannot take the session down.** On Linux and macOS every conversion runs in a disposable worker process
  that is ended after 2 minutes, and on Linux also when it passes 2 GB of memory. On Windows it runs inside the server,
  without those limits.
- **The text of a document is data.** A document can contain sentences addressed to the agent. The server returns what
  the file says, and the skill tells the agent to quote such text, not to follow it.
- **No credentials** are read or stored.

## Limits

- No OCR. A scanned PDF or a photo comes back as a placeholder with a warning; the skill tells the agent to say so.
- Pictures are `[Image]` placeholders. Charts built from data in an Office file come back as data tables.
- On Windows, HTML, email and EPUB are not supported.
- The full list is in [docs/limitations.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/limitations.md).

## Health check without Hermes

```bash
uvx --python ">=3.12" --exclude-newer 2026-11-14T00:00:00Z --from "opendockit[mcp]==0.3.0" opendockit-mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}
EOF
```

A line of JSON naming the server `opendockit` means it starts.

## Version pinning

`mcp.json` pins `opendockit[mcp]==0.3.0` and resolves its dependencies as of 2026-11-14. A newer OpenDocKit, or a
newer release of a dependency, reaches catalog installs only through a commit here that
moves the pin, followed by a reviewed SHA bump in the
[Hermes plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog). The package never
updates itself.

## License

MIT, like OpenDocKit.
