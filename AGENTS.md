# AGENTS.md

Guide for AI coding agents (and people) working in this repository. User documentation is in [README.md](README.md)
and [docs/](docs/); contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Commands

```bash
uv sync                          # install everything
uv run pytest -n auto -m "not timing"   # the bulk of the suite in parallel (under a minute)
uv run pytest -m timing                  # tests that measure time, serially (about 5 minutes)
uv run pytest tests/test_pdf.py  # one module
uv run ruff check                # lint
uv run python -m evals.run --documents <pdfs> --gold <references> --baseline evals/baselines/pdf-structure.json   # structure scores against the baseline (evals/README.md)
uv run python examples/run_examples.py   # reconvert the sample documents (needs LibreOffice for the .doc / .ppt samples); `git diff examples/output` shows what moved
uv run opendockit <file> [--json | --count] [--describe]   # --describe calls a vision model (OPENAI_API_KEY, costs money)
```

## Before changing behavior

Read the decision records first. Every row is a choice that was made on purpose after a real document broke; the
"Pinned by" column names the test that fails if it is undone. **Do not revert a recorded decision** without a new
document that justifies it.

- [docs/decisions/core.md](docs/decisions/core.md) — structure, dependency direction, limits, rendering, tables
- [docs/decisions/extractors.md](docs/decisions/extractors.md) — rules shared by all extractors
- [docs/decisions/](docs/decisions/) — one file per topic (`pdf*.md`, `office*.md`, `spreadsheet*.md`, `hwp.md`,
  `html.md`, `mail.md`, `csv.md`, `encoding.md`, `detect.md`, `images.md`, `odf.md`, `rtf.md`, `epub.md`,
  `legacy-office.md`, `inline-formatting.md`, `integrations.md`)

## Rules

- A change in behavior comes with a test that builds its document in memory (`tests/builder.py`). No binary fixtures.
- A test that asserts on elapsed time or waits on a real timeout is marked `@pytest.mark.timing` (CI runs those serially,
  and a failed one is run again up to twice). A test comparing two measurements times each with `tests.timing.fastest`;
  one that starts processes or threads and checks how they end is also marked `no_rerun`.
  Tests that call a real vision model are marked `live` and skipped unless `OPENDOCKIT_LIVE_TESTS=1`; never run them unasked.
- A change to how structure is guessed from a PDF (headings, tables, reading order) is judged by the scorecard
  (`evals/README.md`), not by the document it was written for: it comes with the comparison against the baseline, no
  mean may fall on the benchmark, the Korean pairs, the Korean holdout or the PDF twins (tagged and untagged), and the baselines are updated in
  the same change. The holdout is run, not read: a rule is not written from its documents (`evals/README.md`). The comparison also names
  the documents whose text changed without a score moving: look at them before saying what a change did.
  A change to how DOCX, ODT, RTF, HTML or EPUB write headings, lists or tables is run against their twins the same way.
  A change to how the characters, the marks or the direction of a line are read is run against the script twins
  (nineteen languages, `evals/README.md`): no mean may fall there either.
- A deliberate trade-off gets a row in `docs/decisions/` and a `Pinned by:` comment at the code site.
- Extractors only produce sections; normalization, headings, images and limits belong to `converter.py`.
- Tables go through `render_table` / `TableGrid`.
- `convert` stays offline and deterministic. Only `describe.py` uses the network.
- Exception messages and warnings are fixed English sentences and never include document content or parser errors.
- No source holds a character that cannot be seen (a zero-width or direction mark, a soft hyphen, a private-use character,
  a space that is not the space): write it as an escape in Python (`"\u200b"`) and name it by its code point in prose
  (U+200B). A character typed as an escape into a tool can arrive in the file as the character itself; `tests/test_source_characters.py` fails when one does.
- No copyleft dependencies in the default install (`heic` is an opt-in extra for that reason). Never commit real documents or keys.
- Korean text in the source is data (patterns matched against documents, numbering tables, sample content in
  tests) — leave it. Everything the project itself says is English. The one exception is the translated READMEs,
  `README.ko.md` and `README.zh-CN.md`: change them together with `README.md`
  (`tests/test_readme_translations.py` fails when their code, links or figures drift apart).
