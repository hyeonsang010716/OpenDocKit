# Contributing to OpenDocKit

Thank you for helping. This page covers how to set up, what a good change looks like, and the few rules the project
does not bend on.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
git clone https://github.com/hyeonsang010716/OpenDocKit.git
cd OpenDocKit
uv sync                      # creates .venv with runtime, vision and development dependencies
uv run pytest -n auto -m "not timing"   # the bulk of the tests in parallel, under a minute
uv run pytest -m timing                  # ~400 tests that measure time, one at a time, about 5 minutes
uv run ruff check            # lint
uv run opendockit some.pdf   # try a real document
```

`uv run pytest -n auto` also works for a quick look, but the timing tests can fail when every core is busy, which is why
they are run separately (as CI does).

On Windows, `uv run python .github/scripts/code_page_949.py -m pytest -m "not timing"` runs the tests in code page 949,
the default of Korean Windows, whatever the code page of the machine, as the `code page 949` job of CI does. Add
`-n auto` only where the path of the checkout is ASCII: the workers of pytest-xdist read their code in the code page.

Optional: `uvx pre-commit install` runs the linter before each commit. LibreOffice is optional too: without it the tests
that start a real LibreOffice are skipped, and `examples/run_examples.py` skips the `.doc` / `.ppt` samples.

On Windows the sample documents, the shell scripts and the Dockerfile are checked out with LF whatever `core.autocrlf`
says (`.gitattributes`), so the readers are run on the bytes Git stores. A clone made before that keeps them with CR LF
until they are checked out again, which a plain `git checkout` does not do. Commit or stash any change to them, then, in
Git Bash:

```bash
files() { git ls-files -z -- examples/samples benchmarks/cases '*.sh' benchmarks/Dockerfile; }
files | xargs -0 rm -- && files | xargs -0 git checkout --
```

Windows also runs programs in a legacy code page unless "Beta: Use Unicode UTF-8 for worldwide language support" is
set: 949 on Korean Windows, by default. There two tools stumble over a checkout whose full path is not ASCII, such as
the Desktop that OneDrive keeps as `바탕 화면`:

- **Python 3.12 does not start `.venv`** (`Fatal Python error: init_import_site`). It reads the `.pth` file of the
  editable install, which holds the path of `src`, in the code page; Python 3.13 and later read it as UTF-8.
  `PYTHONUTF8=1` does not change this.
- **`pytest -n auto` cannot start its workers.** execnet sends them their code as UTF-8 and they read it in the code
  page. `PYTHONUTF8=1` fixes this one.

A checkout under a path that is ASCII avoids both. Otherwise develop on Python 3.13 or later (`uv sync --python 3.13`)
and run the parallel tests with `PYTHONUTF8=1`. The tests pass in code page 949 either way: what they check does not
depend on it.

Tests that call a real vision model are skipped by default because they cost money. To run them (after changing
`describe.py` or its prompts):

```bash
OPENDOCKIT_LIVE_TESTS=1 OPENAI_API_KEY=... uv run pytest -m live     # OPENAI_BASE_URL may point at a compatible endpoint
```

`examples/` holds sample documents and their committed outputs; run `uv run python examples/run_examples.py` after a
change to see what moved.

Questions and ideas are welcome in [Discussions](https://github.com/hyeonsang010716/OpenDocKit/discussions); bugs go to
[Issues](https://github.com/hyeonsang010716/OpenDocKit/issues).

## Reporting a problem

The most useful bug report is **a script that builds a small document showing the problem**. Real documents are often
private or copyrighted, so please do not attach them unless you own them and they contain nothing sensitive.
`tests/builder.py` has helpers for every format; a failing test in a draft pull request is the ideal report.

Crashes, hangs and memory blow-ups caused by crafted files are security issues — see [SECURITY.md](SECURITY.md).

## Project layout

```
src/opendockit/
├── __init__.py      public API
├── converter.py     size check → detect → extract → normalize → images → render
├── isolated.py      IsolatedConverter: one worker process per file, memory and time limits
├── detect.py        format detection by content (magic bytes), not by extension
├── markdown.py      clean_text, render_table, TableGrid — shared by every format
├── image.py         decode, filter, downscale and re-encode images
├── describe.py      vision-model descriptions; the only module that uses the network
├── models.py        ConvertOptions, ConversionResult, Section, DocumentImage
├── exceptions.py    ConversionError and its subclasses
├── cli.py           the `opendockit` command (`__main__.py`: `python -m opendockit`)
├── mcp_server.py    the `opendockit-mcp` command: an MCP server with two tools
├── open_webui.py    the `opendockit-open-webui` command: Open WebUI's external content extraction engine
├── integrations/    the LangChain loader, the LlamaIndex reader and the LightRAG parser engine
└── extractors/      one module per format: (bytes, ConvertOptions) -> Extraction; `_name.py` modules are stages and shared pieces
tests/               one or more test modules per format; builder.py creates sample documents in memory
docs/                user documentation
docs/decisions/      decision records: every deliberate trade-off and the test that pins it
examples/            sample documents and their conversions (`examples/run_examples.py`)
benchmarks/          the comparison with other converters and its results
claude-plugin/, hermes-plugin/, dify-plugin/   plugins that run the MCP server or the converter in those applications
packages/opendockit-mcp/                       the `opendockit-mcp` launcher package
packages/opendockit-xlsx-core/                 the optional native reader of large XLSX sheets
```

Dependency direction: extractors import only `markdown`, `models`, `exceptions` and the shared helpers of
`extractors/` (the modules whose names start with `_`). Extractors produce sections; normalization, section headings, image handling and the
character limit belong to `converter.py`. The details are in [docs/decisions/core.md](docs/decisions/core.md).

## What a good change looks like

1. **Start from a failing test.** Build the smallest document that shows the problem with `tests/builder.py`
   (or the format's own library), convert it, and assert on the Markdown. Tests never read binary fixtures from disk —
   everything is generated in memory, which keeps the repository small and free of other people's documents.
2. **Fix it in the right layer.** Table rendering goes through `render_table` / `TableGrid` so merged-cell rules are
   identical across formats. Images are registered raw with `Extraction.add_image()`; decoding and filtering happen
   once, in the converter.
3. **Record deliberate trade-offs.** If the change chooses one behavior over another on purpose — or accepts a known
   gap — add a row to the matching file in `docs/decisions/`: the decision, why, and the test that pins it. A comment
   at the code site should name the same test (`Pinned by: test_x.py::TestY::test_z`). These records are how the
   project avoids re-breaking the same documents.
4. **Mark tests that measure time.** A test that asserts on elapsed time or waits on a real timeout gets
   `@pytest.mark.timing`. CI runs those one at a time (`uv run pytest -m timing`) after the parallel run
   (`uv run pytest -n auto -m "not timing"`), so a busy machine cannot fail them. Prefer comparing two input sizes
   ("4× the input takes less than 8× the time") over an absolute number of seconds, and measure each size with
   `tests.timing.fastest` (or `Fastest` when the input has to be made anew for every run): one stall of a shared CI
   runner otherwise reads as slow code. A failed timing test is run again up to twice (`tests/conftest.py`, at most 10
   reruns a session); do not loosen a bound to make one pass. A timing test that starts processes or threads and checks
   how they end also gets `@pytest.mark.no_rerun`: a failure there can be a real race, and a rerun would hide it.
5. **Update the docs** if users will notice: `docs/formats.md`, `docs/limitations.md`, `CHANGELOG.md`.

## Rules that do not bend

- **No AGPL or other copyleft dependencies in the default install.** PyMuPDF and pyhwp are excluded on purpose. New
  dependencies must be MIT / BSD / Apache-style and should ship wheels for Linux, macOS and Windows. Check what a
  wheel *bundles*, not only its own license: `pillow-heif` is BSD but ships GPL/LGPL codecs, which is why it lives
  behind the optional `heic` extra.
- **`convert` never touches the network** and is deterministic. Network access lives only in `describe.py` (the servers,
  `mcp_server.py` and `open_webui.py`, only answer the requests they are sent). The
  one process it starts is LibreOffice for `.doc` / `.ppt` (`extractors/legacy.py`), with network access switched off.
- **Untrusted input.** Never trust a size, count, span or offset stored in a file. Bound it, and add a test with a
  small crafted file that would otherwise take seconds or gigabytes.
- **User-facing text is fixed English wording.** Exception messages and warnings may be shown to end users, so they
  never contain document content or the text of a parser exception (chain the cause with `raise … from exc`
  instead). Warnings may name a sheet or a page number to locate the problem.
- **The character limit is exact.** `exceeded` must equal "the full conversion would be longer than `max_chars`".
  Early-stop checks may only undercount. See the decision records before touching this.
- **Never commit real documents**, API keys, or anything under `data/`.

## Adding a new format

1. Add a value to `models.DocumentFormat`.
2. Teach `detect.py` to recognize it by content.
3. Write `extractors/<format>.py` and register it in `extractors/__init__.py`.
4. Add a sample builder to `tests/builder.py` and tests in `tests/test_<format>.py`.
5. Document it in `docs/formats.md`, `docs/limitations.md`, `docs/integration-guide.md`, `docs/images.md`, the option and
   error tables of `docs/options.md`, the README table and the `pyproject.toml` keywords.

A new format works in the integrations (`docs/integrations.md`) without any change: they read sections, not formats.

The README has Korean and Simplified Chinese translations (`README.ko.md`, `README.zh-CN.md`). Change all three
together; a test compares their code, links, figures and structure. Everything else stays in English.

## Style

- Ruff is the linter (`uv run ruff check`, line length 150). Match the surrounding code.
- Type hints on public functions. The package ships `py.typed`.
- Comments explain *why* — the document that broke, the measurement, the alternative that failed.
- Commit messages: a short imperative subject (`Fix merged header cells in borderless PDF tables`), details in the
  body if needed. Keep pull requests focused on one problem.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml`, in `packages/opendockit-mcp/` (`pyproject.toml` with its pin, `server.json`)
   and in `dify-plugin/` (`manifest.yaml`, `requirements.txt`) — the tests compare them — and move the `Unreleased`
   entries in `CHANGELOG.md` under the new version.
2. Tag the commit `vX.Y.Z` and push the tag. The release workflow builds and publishes to PyPI and the MCP Registry,
   then makes the GitHub release from the version's `CHANGELOG.md` section. A tag without one stops before publishing.
