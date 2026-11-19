## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How it was verified

<!-- The test you added, and what it failed with before the change. -->

## Checklist

- [ ] `uv run pytest -n auto -m "not timing"` and `uv run pytest -m timing` pass
- [ ] `uv run ruff check` passes
- [ ] A test pins the new behavior; sample documents are built in the test, not committed as files
- [ ] If this is a deliberate trade-off, a row was added to the matching file in `docs/decisions/`
- [ ] User-visible changes are noted in `CHANGELOG.md`
