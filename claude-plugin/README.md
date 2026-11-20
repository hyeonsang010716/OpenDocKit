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

## Examples

The sample files are in the repository, under
[`examples/samples`](https://github.com/hyeonsang010716/OpenDocKit/tree/main/examples/samples). Download them, then
ask with their path on your computer:

1. `Read score-table.hwpx and give each student's total score.` An HWPX file (Korean word processor) with a table;
   the totals are 232, 231, 264 and 198.
2. `What does slide 2 of launch-plan.pptx say, including the speaker notes?` A deck read slide by slide: the outline
   first, then only slide 2.
3. `In sales.xlsx, which region had the largest share, and what were its quarterly figures?` A workbook with two
   tables and a chart on one sheet; the answer is North, $120k, $135k, $150k and $170k.
4. `Summarise the revenue by region in quarterly-report.docx.` A Word table whose category cells are merged; each row
   keeps its category.

## What you get

| Tool | What it returns |
| --- | --- |
| `document_outline(path)` | Format, title, size, and the numbered pages / slides / sheets with their titles and lengths, without the text |
| `convert_document(path, sections=None, max_chars=100000)` | The Markdown of the file, or of the pages / slides / sheets asked for (`"3"` or `"2-5"`), followed by the converter's warnings |

Claude Code names them `mcp__plugin_opendockit_opendockit__document_outline` and
`mcp__plugin_opendockit_opendockit__convert_document`. Both are marked read-only.

One call returns at most `max_chars` characters of the document. A longer one is cut with a note that tells Claude to
read the outline and ask for sections, which the bundled skill teaches it to do first; a document without pages, slides
or sheets has nothing to ask for, and its note says to raise `max_chars` instead.

## What it does on your machine

- **Runs a local process.** `uvx --python ">=3.12" --exclude-newer 2026-11-19T07:46:00Z --from "opendockit[mcp]==0.3.14" opendockit-mcp`,
  over stdio, for as long as the session lasts. Each file is converted in a worker process that is ended after 2
  minutes, or on Linux and Windows at 2 GB (macOS accepts no memory limit).
- **Downloads once.** The first start fetches `opendockit` 0.3.14 and its dependencies from PyPI into uv's cache, and
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

`.mcp.json` pins `opendockit[mcp]==0.3.14` and resolves its dependencies as of 2026-11-19, so the plugin installs the
same code until this file changes. The plugin's version is the OpenDocKit release it starts.

## Limits

- Scanned PDFs and photos: no OCR. Claude's Read tool can look at such a page as an image.
- Legacy `.doc` and `.ppt` need LibreOffice.
- The server reads local files only, not URLs.

## Troubleshooting

- **`/mcp` shows the `opendockit` server as failed.** It is started with `uvx`, part of [uv](https://docs.astral.sh/uv/).
  Check that `uvx --version` works in the shell Claude Code runs in, then start a new session.
- **The first call takes long.** The first start downloads the package, and Python if needed. Later sessions start
  from uv's cache.
- **"The file is outside the folders this server may read."** `OPENDOCKIT_MCP_ROOTS` is set and the file is not
  under it. Add the folder, or unset the variable, and start a new session.
- **A scanned PDF or a photo comes back without text.** There is no OCR; Claude's Read tool can look at such a page.
- **A short fixed message instead of the text** (password-protected, damaged, too large, unsupported format, timed
  out after 2 minutes) says why the file could not be converted. Retrying does not change it.

## Support and privacy

- Questions and bugs: [GitHub issues](https://github.com/hyeonsang010716/OpenDocKit/issues).
- Security problems: report them privately, as [SECURITY.md](https://github.com/hyeonsang010716/OpenDocKit/blob/main/SECURITY.md) explains.
- What the plugin does with your files: [PRIVACY.md](PRIVACY.md). In short, nothing leaves your computer.

## License

MIT, like OpenDocKit. This is an independent community plugin, not affiliated with Anthropic.
