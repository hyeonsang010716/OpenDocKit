"""Write the references of the scripts scorecard: one text in the scripts of the world (`evals/README.md`).

    uv run python evals/fetch_script_texts.py <target-dir>

The text is the Universal Declaration of Human Rights, which the "UDHR in Unicode" project keeps as XML in several
hundred languages (github.com/eric-muller/udhr, read at a fixed revision): a title, a preamble and thirty articles of
paragraphs and numbered lists. Each language of `LANGUAGES` is written as `<target-dir>/<code>.md` — the title, a
heading for the preamble and for each article, the paragraphs and the lists — and `make_twins.py` then writes each as a
DOCX and a PDF. The readers are so scored on the same document in Arabic, Hebrew, Persian, Urdu, Hindi, Bengali,
Tamil, Thai, Khmer, Burmese, Amharic, Georgian, Armenian, Russian, Greek, Japanese, Chinese, Korean and Vietnamese,
whose right answer is known. Nothing fetched here is committed.
"""

from pathlib import Path
import sys
import time
import unicodedata
import urllib.request
from xml.etree import ElementTree


REVISION = "e6a1b6f8cb048fba115d6b81bab47875a4eca0d8"
SOURCE = f"https://raw.githubusercontent.com/eric-muller/udhr/{REVISION}/data/udhr/udhr_{{code}}.xml"
# ISO 639-3 code of each text, by script
# The name of each text in that project (an ISO 639-3 code, some with a variant), by script
LANGUAGES = ("arb", "heb", "pes_1", "urd",                  # written from right to left
             "hin", "ben", "tam", "tha", "khm", "mya",      # marks set on letters, clusters
             "amh", "kat", "hye", "rus", "ell_monotonic",   # other alphabets
             "jpn", "cmn_hans", "kor", "vie")               # Han, kana, Hangul; Latin with two marks on a letter
_ARTICLES = 12  # a reference holds the preamble and this many articles: two pages, with lists among them
_PAUSE = 1.0  # seconds between requests
_MAX_BYTES = 2 * 1024 * 1024
_NAMESPACE = "{http://efele.net/udhr}"


def _line(element: ElementTree.Element) -> str:
    return unicodedata.normalize("NFC", " ".join("".join(element.itertext()).split()))


def _blocks(part: ElementTree.Element) -> list[str]:
    """The paragraphs and lists of a preamble or an article as blocks of Markdown."""
    blocks = []
    for child in part:
        if child.tag == f"{_NAMESPACE}para":
            blocks.append(_line(child))
        elif child.tag == f"{_NAMESPACE}orderedlist":
            items = [_line(item) for item in child.findall(f"{_NAMESPACE}listitem")]
            blocks.append("\n".join(f"{number}. {item}" for number, item in enumerate(items, 1)))
    return [block for block in blocks if block]


def reference(xml: bytes) -> str:
    """The Markdown of the start of one text."""
    root = ElementTree.fromstring(xml)
    blocks = [f"# {_line(root.find(f'{_NAMESPACE}title'))}"]
    parts = [*root.findall(f"{_NAMESPACE}preamble"), *root.findall(f"{_NAMESPACE}article")[:_ARTICLES]]
    for part in parts:
        title = part.find(f"{_NAMESPACE}title")
        if title is not None and _line(title):
            blocks.append(f"## {_line(title)}")
        blocks += _blocks(part)
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: fetch_script_texts.py <target-dir>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    for code in LANGUAGES:
        path = target / f"{code}.md"
        if path.is_file():
            continue
        request = urllib.request.Request(SOURCE.format(code=code), headers={"User-Agent": "document-conversion test corpus"})
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_text(reference(response.read(_MAX_BYTES)), encoding="utf-8")
        time.sleep(_PAUSE)
    print(f"{len(list(target.glob('*.md')))} references in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
