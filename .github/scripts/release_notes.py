"""Print the GitHub release notes of one version: its CHANGELOG.md section, un-wrapped, and a link to the full list.

Usage: python3 .github/scripts/release_notes.py 1.2.3 [CHANGELOG.md]

The CHANGELOG is wrapped at 120 columns for reading in an editor, and a GitHub release page renders each line break
of a list item as written, so the lines of a paragraph or a list item are joined. The release workflow runs it before
publishing, so a tag without a CHANGELOG section stops there (Pinned by: test_release_notes.py).
"""

import re
import sys


FULL_LIST = "Full list: [CHANGELOG](https://github.com/hyeonsang010716/OpenDocKit/blob/main/CHANGELOG.md)."
_BLOCK_START = re.compile(r"(#|\||```|[-*] |\d+\. )")
_LIST_ITEM = re.compile(r"([-*] |\d+\. )")


def section(changelog: str, version: str) -> str:
    """The body of `## [version] - date`, up to the next `## [` heading."""
    match = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n", changelog, re.MULTILINE)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}.")
    body = changelog[match.end():]
    end = re.search(r"^## \[", body, re.MULTILINE)
    body = (body[:end.start()] if end else body).strip("\n")
    if not body.strip():
        raise ValueError(f"The CHANGELOG.md section for {version} is empty.")
    return body


def unwrapped(text: str) -> str:
    """Join each paragraph and list item into one line; headings, tables, code and blank lines stay as they are."""
    lines: list[str] = []
    in_code = False
    for line in text.split("\n"):
        previous = lines[-1] if lines else ""
        starts_block = line.startswith("```") or _BLOCK_START.match(line) is not None
        if line.startswith("```"):
            in_code = not in_code
        # A heading, a table row or a code fence takes no continuation; a list item does.
        previous_closed = _BLOCK_START.match(previous) is not None and not _LIST_ITEM.match(previous)
        if in_code or starts_block or not line.strip() or not previous.strip() or previous_closed:
            lines.append(line)
        else:
            lines[-1] = f"{previous} {line.strip()}"
    return "\n".join(lines)


def notes(changelog: str, version: str) -> str:
    return f"{unwrapped(section(changelog, version))}\n\n{FULL_LIST}\n"


if __name__ == "__main__":
    path = sys.argv[2] if len(sys.argv) > 2 else "CHANGELOG.md"
    with open(path, encoding="utf-8") as file:
        changelog = file.read()
    try:
        sys.stdout.write(notes(changelog, sys.argv[1]))
    except ValueError as error:
        sys.exit(str(error))
