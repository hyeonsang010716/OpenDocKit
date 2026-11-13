# opendockit-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an agent — Claude Desktop, Claude Code, Cursor, VS Code, or
anything else that speaks the protocol — read the documents on your machine as Markdown: PDF, Word, PowerPoint, Excel,
CSV, **HWP / HWPX**, OpenDocument, RTF, EPUB, HTML and email. Tables keep their rows and columns, merged cells are
repeated in every row they cover, and a long document can be read one page, slide or sheet at a time, so it does not fill
the context.

The server is part of [OpenDocKit](https://github.com/hyeonsang010716/OpenDocKit) (MIT, pure Python, no models, offline).
This package holds no code: it installs `opendockit[mcp]` at the same version and gives the server a name that can be
run directly.

<!-- mcp-name: io.github.hyeonsang010716/opendockit -->

## Use

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

`--root` (repeatable, or the `OPENDOCKIT_MCP_ROOTS` environment variable, separated like `PATH`) names the only folders
the server may read. Without it the server can read any file your user can read.

| Tool | What it returns |
| --- | --- |
| `document_outline(path)` | Format, title, size, and the numbered pages / slides / sheets with their titles and lengths — no text |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of the pages / slides / sheets asked for (`"3"` or `"2-5"`), followed by the converter's warnings |

- **Read-only and offline.** Both tools are marked read-only, nothing is written, and nothing is sent anywhere. No API
  key.
- **The answer is bounded.** One call returns at most `max_chars` characters of the document; a longer one is cut with a
  note that tells the agent to read the outline and ask for sections.
- **One file cannot take the server down.** On Linux and macOS every conversion runs in a disposable worker process that
  is ended after 2 minutes, and on Linux also when it passes 2 GB of memory. On Windows it runs inside the server,
  without those limits.
- **The text of a document is data.** A document can contain sentences addressed to the agent. The server returns what
  the file says; let it read only the folders you would hand the agent yourself.

## Limits

- No OCR: a scanned PDF or a photo comes back as a placeholder with a warning.
- Legacy `.doc` / `.ppt` need LibreOffice installed.
- On Windows, HTML, email and EPUB are not supported.

Everything else — the tools in detail, the design notes, the limits — is in the
[integrations guide](https://github.com/hyeonsang010716/OpenDocKit/blob/main/docs/integrations.md).
