"""The GitHub release notes are made from CHANGELOG.md by `.github/scripts/release_notes.py` in the release workflow.

Two releases (0.3.12, 0.3.13) reached PyPI without a GitHub release, because that step was done by hand, and the
CHANGELOG's links to their release pages were dead. The workflow now makes the release, and refuses a tag whose version
has no CHANGELOG section before anything is published.
"""

import importlib.util
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="the release scripts are not part of the source distribution")


def _script():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-01-02

A lead paragraph wrapped
over two lines.

### Added

- **One**: a list item wrapped
  over two lines.
- **Two**: on one line.

| A | B |
| --- | --- |
| 1 | 2 |

```bash
pip install x
  --flag
```

## [1.1.0] - 2026-01-01

First.
"""


class TestNotes:
    def test_paragraphs_and_list_items_become_one_line_each(self):
        notes = _script().notes(CHANGELOG, "1.2.0")

        assert notes.startswith("A lead paragraph wrapped over two lines.\n\n### Added\n\n")
        assert "- **One**: a list item wrapped over two lines.\n- **Two**: on one line.\n" in notes
        assert "| A | B |\n| --- | --- |\n| 1 | 2 |\n" in notes
        assert "```bash\npip install x\n  --flag\n```" in notes
        assert notes.endswith("\n\nFull list: [CHANGELOG](https://github.com/hyeonsang010716/OpenDocKit/blob/main/CHANGELOG.md).\n")
        assert "First." not in notes

    def test_last_section_ends_at_the_end_of_the_file(self):
        assert _script().notes(CHANGELOG, "1.1.0").startswith("First.\n\n")

    @pytest.mark.parametrize("version", ["1.3.0", "1.2", "Unreleased"])
    def test_a_version_without_a_section_is_refused(self, version):
        with pytest.raises(ValueError, match="no section"):
            _script().notes(CHANGELOG, version)

    def test_an_empty_section_is_refused(self):
        with pytest.raises(ValueError, match="empty"):
            _script().notes("## [2.0.0] - 2026-02-02\n\n## [1.0.0] - 2026-01-01\n\nx\n", "2.0.0")


class TestThisRepository:
    def test_the_version_being_released_has_notes(self):
        """The workflow checks this when the tag is pushed; here it fails in the release pull request already."""
        version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE).group(1)
        notes = _script().notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)

        assert not any(line.startswith("  ") for line in notes.split("\n") if not line.startswith("```"))

    def test_the_workflow_makes_the_release_last_and_checks_the_notes_before_publishing(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        build = workflow[workflow.index("\n  build:"):workflow.index("\n  publish:")]
        release = workflow[workflow.index("\n  github-release:"):]

        assert "release_notes.py" in build
        assert "needs: registry" in release and "contents: write" in release and "gh release create" in release
