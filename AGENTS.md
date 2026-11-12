# AGENTS.md

Guide for AI coding agents (and people) working in this repository. User documentation is in [README.md](README.md)
and [docs/](docs/); contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Commands

```bash
uv sync                          # install everything
uv run pytest -n auto            # full test suite (about 30 seconds)
uv run pytest tests/test_pdf.py  # one module
uv run ruff check                # lint
uv run opendockit <file> [--json | --count] [--describe]   # --describe calls a vision model (OPENAI_API_KEY, costs money)
```

## Before changing behavior

Read the decision records first. Every row is a choice that was made on purpose after a real document broke; the
"Pinned by" column names the test that fails if it is undone. **Do not revert a recorded decision** without a new
document that justifies it.

- [docs/decisions/core.md](docs/decisions/core.md) — structure, dependency direction, limits, rendering, tables
- [docs/decisions/extractors.md](docs/decisions/extractors.md) — rules shared by all extractors
- [docs/decisions/](docs/decisions/) — one file per topic (`pdf-*.md`, `office-*.md`, `spreadsheet-*.md`, `hwp.md`,
  `html.md`, `mail.md`, `csv.md`, `encoding.md`, `detect.md`, `images.md`)

## Rules

- A change in behavior comes with a test that builds its document in memory (`tests/builder.py`). No binary fixtures.
- A deliberate trade-off gets a row in `docs/decisions/` and a `Pinned by:` comment at the code site.
- Extractors only produce sections; normalization, headings, images and limits belong to `converter.py`.
- Tables go through `render_table` / `TableGrid`.
- `convert` stays offline and deterministic. Only `describe.py` uses the network.
- Exception messages and warnings are fixed English sentences and never include document content or parser errors.
- No copyleft dependencies in the default install (`heic` is an opt-in extra for that reason). Never commit real documents or keys.
- Korean text in the source is data (patterns matched against documents, numbering tables, sample content in
  tests) — leave it. Everything the project itself says is English.
