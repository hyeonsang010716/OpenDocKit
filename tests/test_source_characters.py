"""The sources hold no character that cannot be seen: such a character is written as an escape (`"\\u200b"`) in Python
and named by its code point (U+200B) in prose.

A zero-width space, a mark that steers the direction of text, a private-use character or a no-break space looks like
nothing or like an ordinary space in an editor and in a review, so a pattern or a test that holds one says something
else than it shows — and text typed as an escape into a tool can arrive in the file as the character itself."""

from pathlib import Path
import subprocess
import unicodedata

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_TEXT = (".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".sh", ".tsv")
# Converted documents kept to be compared with, not written by hand
_DATA = ("examples/output/", "benchmarks/results/")


def _hidden(character: str) -> bool:
    """A control character other than tab and the line ends, a format character (zero-width and direction marks, soft
    hyphen), a private-use or unassigned character, or a space that is not the space."""
    if character in "\t\n\r ":
        return False
    return unicodedata.category(character) in ("Cc", "Cf", "Co", "Cn", "Zl", "Zp", "Zs")


def _sources() -> list[str]:
    listed = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        pytest.skip("not a checkout of the repository")
    return [name for name in listed.stdout.splitlines() if name.endswith(_TEXT) and not name.startswith(_DATA)]


def test_no_source_holds_a_character_that_cannot_be_seen():
    found = []
    for name in _sources():
        path = _ROOT / name
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            found += [f"{name}:{number} U+{ord(character):04X}" for character in line.rstrip("\r") if _hidden(character)]

    assert found == []


@pytest.mark.parametrize("character", ["\u200b", "\u200e", "\u202e", "\u2066", "\xad", "\xa0", "\u3000", "\ue000", "\x07", "\ufeff"])
def test_what_cannot_be_seen(character):
    assert _hidden(character)


@pytest.mark.parametrize("character", ["a", " ", "\t", "\n", "가", "é", "ع", "ิ", "—", "✓", "ﬁ"])
def test_what_can(character):
    assert not _hidden(character)
