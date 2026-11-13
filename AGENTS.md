# AGENTS.md

Guide for AI coding agents (and people) working in this repository. User documentation is in [README.md](README.md)
and [docs/](docs/); contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Commands

```bash
uv sync                          # install everything
uv run pytest -n auto -m "not timing"   # the bulk of the suite in parallel (about 20 seconds)
uv run pytest -m timing                  # tests that measure time, serially (about 40 seconds)
uv run pytest tests/test_pdf.py  # one module
uv run ruff check                # lint
uv run python examples/run_examples.py   # reconvert the sample documents; `git diff examples/output` shows what moved
uv run opendockit <file> [--json | --count] [--describe]   # --describe calls a vision model (OPENAI_API_KEY, costs money)
```

## Before changing behavior

Read the decision records first. Every row is a choice that was made on purpose after a real document broke; the
"Pinned by" column names the test that fails if it is undone. **Do not revert a recorded decision** without a new
document that justifies it.

- [docs/decisions/core.md](docs/decisions/core.md) — structure, dependency direction, limits, rendering, tables
- [docs/decisions/extractors.md](docs/decisions/extractors.md) — rules shared by all extractors
- [docs/decisions/](docs/decisions/) — one file per topic (`pdf-*.md`, `office-*.md`, `spreadsheet-*.md`, `hwp.md`,
  `html.md`, `mail.md`, `csv.md`, `encoding.md`, `detect.md`, `images.md`, `odf.md`, `rtf.md`, `epub.md`,
  `legacy-office.md`, `inline-formatting.md`)

## Rules

- A change in behavior comes with a test that builds its document in memory (`tests/builder.py`). No binary fixtures.
- A test that asserts on elapsed time or waits on a real timeout is marked `@pytest.mark.timing` (CI runs those serially).
  Tests that call a real vision model are marked `live` and skipped unless `OPENDOCKIT_LIVE_TESTS=1`; never run them unasked.
- A deliberate trade-off gets a row in `docs/decisions/` and a `Pinned by:` comment at the code site.
- Extractors only produce sections; normalization, headings, images and limits belong to `converter.py`.
- Tables go through `render_table` / `TableGrid`.
- `convert` stays offline and deterministic. Only `describe.py` uses the network.
- Exception messages and warnings are fixed English sentences and never include document content or parser errors.
- No copyleft dependencies in the default install (`heic` is an opt-in extra for that reason). Never commit real documents or keys.
- Korean text in the source is data (patterns matched against documents, numbering tables, sample content in
  tests) — leave it. Everything the project itself says is English.
