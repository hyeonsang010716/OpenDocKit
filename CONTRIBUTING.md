# Contributing to OpenDocKit

Thank you for helping. This page covers how to set up, what a good change looks like, and the few rules the project
does not bend on.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
git clone https://github.com/hyeonsang010716/OpenDocKit.git
cd OpenDocKit
uv sync                      # creates .venv with runtime, vision and development dependencies
uv run pytest -n auto -m "not timing"   # ~3,300 tests in parallel, about 20 seconds
uv run pytest -m timing                  # ~130 tests that measure time, one at a time, about 90 seconds
uv run ruff check            # lint
uv run opendockit some.pdf   # try a real document
```

`uv run pytest -n auto` also works for a quick look, but the timing tests can fail when every core is busy, which is why
they are run separately (as CI does).

Optional: `uvx pre-commit install` runs the linter before each commit. LibreOffice is optional too: without it the tests
that start a real LibreOffice are skipped, and `examples/run_examples.py` skips the `.doc` / `.ppt` samples.

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
├── cli.py           the `opendockit` command
└── extractors/      one module per format: (bytes, ConvertOptions) -> Extraction
tests/               one or more test modules per format; builder.py creates sample documents in memory
docs/                user documentation
docs/decisions/      decision records: every deliberate trade-off and the test that pins it
```

Dependency direction: extractors import only `markdown`, `models`, `exceptions` and the shared helpers in
`extractors/_shared.py`. Extractors produce sections; normalization, section headings, image handling and the
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
   ("4× the input takes less than 8× the time") over an absolute number of seconds.
5. **Update the docs** if users will notice: `docs/formats.md`, `docs/limitations.md`, `CHANGELOG.md`.

## Rules that do not bend

- **No AGPL or other copyleft dependencies in the default install.** PyMuPDF and pyhwp are excluded on purpose. New
  dependencies must be MIT / BSD / Apache-style and should ship wheels for Linux, macOS and Windows. Check what a
  wheel *bundles*, not only its own license: `pillow-heif` is BSD but ships GPL/LGPL codecs, which is why it lives
  behind the optional `heic` extra.
- **`convert` never touches the network** and is deterministic. Network access lives only in `describe.py`. The
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

## Style

- Ruff is the linter (`uv run ruff check`, line length 150). Match the surrounding code.
- Type hints on public functions. The package ships `py.typed`.
- Comments explain *why* — the document that broke, the measurement, the alternative that failed.
- Commit messages: a short imperative subject (`Fix merged header cells in borderless PDF tables`), details in the
  body if needed. Keep pull requests focused on one problem.

## Releasing (maintainers)

1. Update `version` in `pyproject.toml` and move the `Unreleased` entries in `CHANGELOG.md` under the new version.
2. Tag the commit `vX.Y.Z` and push the tag. The release workflow builds and publishes to PyPI.
