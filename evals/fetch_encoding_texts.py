"""Write the texts of the encodings scorecard: one text in the languages of the legacy code pages (`evals/README.md`).

    uv run python -m evals.fetch_encoding_texts <target-dir>

The text is the Universal Declaration of Human Rights of the "UDHR in Unicode" project, read at the revision
`fetch_script_texts.py` reads it at. Each language of `evals.encodings.PAGES` is written as `<target-dir>/<code>.txt`, a
paragraph or a list item a line. `evals/encodings.py` writes these lines in the code pages each language was written in
and asks the reader which one it is. Nothing fetched here is committed.
"""

from pathlib import Path
import sys
import time
import unicodedata
import urllib.request
from xml.etree import ElementTree

from evals.encodings import PAGES
from evals.fetch_script_texts import SOURCE


_PAUSE = 1.0  # seconds between requests
_MAX_BYTES = 2 * 1024 * 1024
_MIN_CHARS = 20  # a shorter line is a number or a heading


def lines(xml: bytes) -> list[str]:
    """The paragraphs and list items of one text, each on a line."""
    found = []
    for element in ElementTree.fromstring(xml).iter():
        if element.tag.endswith(("}para", "}listitem")):
            line = unicodedata.normalize("NFC", " ".join("".join(element.itertext()).split()))
            if len(line) > _MIN_CHARS:
                found.append(line)
    return found


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m evals.fetch_encoding_texts <target-dir>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    for code in sorted({code for codes in PAGES.values() for code in codes}):
        path = target / f"{code}.txt"
        if path.is_file():
            continue
        request = urllib.request.Request(SOURCE.format(code=code), headers={"User-Agent": "document-conversion test corpus"})
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_text("\n".join(lines(response.read(_MAX_BYTES))) + "\n", encoding="utf-8")
        time.sleep(_PAUSE)
    print(f"{len(list(target.glob('*.txt')))} texts in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
