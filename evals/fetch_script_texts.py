"""Write the references of the scripts scorecard: one text in the scripts of the world (`evals/README.md`).

    uv run python evals/fetch_script_texts.py <target-dir>

The text is the Universal Declaration of Human Rights, which the "UDHR in Unicode" project keeps as XML in several
hundred languages (github.com/eric-muller/udhr, read at a fixed revision): a title, a preamble and thirty articles of
paragraphs and numbered lists. Each language of `LANGUAGES` is written as `<target-dir>/<code>.md` — the title, a
heading for the preamble and for each article, the paragraphs and the lists, and at the end a table of the first
articles (number, heading, first words) — and `make_twins.py` then writes each as a DOCX and a PDF. The readers are so
scored on the same document in Arabic, Hebrew, Persian, Urdu, Hindi, Bengali, Tamil, Thai, Khmer, Burmese, Amharic,
Georgian, Armenian, Russian, Greek, Japanese, Chinese, Korean and Vietnamese, whose right answer is known. Nothing
fetched here is committed.
"""

from pathlib import Path
import sys
import time
import unicodedata
import urllib.request
from xml.etree import ElementTree

from opendockit.cli import _write_output_as_utf8_lines


REVISION = "e6a1b6f8cb048fba115d6b81bab47875a4eca0d8"
SOURCE = f"https://raw.githubusercontent.com/eric-muller/udhr/{REVISION}/data/udhr/udhr_{{code}}.xml"
# ISO 639-3 code of each text, by script
# The name of each text in that project (an ISO 639-3 code, some with a variant), by script
LANGUAGES = ("arb", "heb", "pes_1", "urd",                  # written from right to left
             "hin", "ben", "tam", "tha", "khm", "mya",      # marks set on letters, clusters
             "amh", "kat", "hye", "rus", "ell_monotonic",   # other alphabets
             "jpn", "cmn_hans", "kor", "vie")               # Han, kana, Hangul; Latin with two marks on a letter
_ARTICLES = 12  # a reference holds the preamble and this many articles: two pages, with lists among them
_TABLE_ROWS = 6  # the articles listed in the table at the end
# The start of an article in its row: this many words, cut after this many characters. Not 30: the column of a twin holds
# ten Han characters, and a cell cut to three full lines ends where its line does, which no reader of a page can see
_CELL_WORDS, _CELL_CHARS = 4, 24
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


def _start(text: str) -> str:
    """The first words of a text for the cell of a table, cut after `_CELL_CHARS` characters where a text has no spaces
    (Japanese, Thai), never between a letter and the marks set on it, and not after a zero-width joiner."""
    cell = " ".join(text.split()[:_CELL_WORDS])
    if len(cell) <= _CELL_CHARS:
        return cell
    end = _CELL_CHARS
    while end < len(cell) and unicodedata.category(cell[end]) in ("Mn", "Mc", "Me"):
        end += 1
    while end and (cell[end - 1].isspace() or unicodedata.category(cell[end - 1]) == "Cf"):
        end -= 1  # a zero-width joiner at the end of a cell joins nothing
    return cell[:end]


def _table(articles: list[ElementTree.Element]) -> str:
    """A table of the first articles — number, heading, first words — which no text has, so that the tables of every
    script are scored too. Its first row is its header."""
    rows = []
    for number, article in enumerate(articles[:_TABLE_ROWS], 1):
        title = article.find(f"{_NAMESPACE}title")
        first = next(iter(_blocks(article)), "")
        cells = [str(number), _line(title) if title is not None else "", _start(first.partition("\n")[0].removeprefix("1. "))]
        rows.append("| " + " | ".join(cell.replace("|", "/") for cell in cells) + " |")
    return "\n".join([rows[0], "| --- | --- | --- |", *rows[1:]]) if len(rows) >= 2 else ""


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
    table = _table(root.findall(f"{_NAMESPACE}article"))
    if table:
        blocks.append(table)
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
    _write_output_as_utf8_lines()   # UTF-8 whatever the code page, as the command line (Pinned by: test_evals_output.py)
    sys.exit(main())
