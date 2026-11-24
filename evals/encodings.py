"""Is text without a stated charset read in the code page it was written in? Encode real text in each legacy code page
of its language and ask the reader.

    uv run python -m evals.fetch_encoding_texts <dir>                      # once: one text in 59 languages
    uv run python -m evals.encodings --texts <dir> [--baseline <file>] [--update-baseline] [--show <n>]

A text file, a CSV, an HTML page or a mail part that names no charset and is not UTF-8 is read in a code page the reader
has to guess (`_encoding.decode_text`). A wrong guess is silent and looks like text: `5 µm` came out as `5 ľm`. No
scorecard of documents sees it, because every corpus of documents is UTF-8 or states its charset.

Each text of the folder (`<language>.txt`, a paragraph a line: `evals/fetch_encoding_texts.py`) is written in the code
pages of `PAGES` its language was written in, cut five ways - the start of a paragraph, a paragraph, four paragraphs,
the whole text, and one such start among rows of ASCII - as plain text and as a page of HTML, and the reader's text is
compared with what was encoded. A sample that a code page cannot write is left out.

The guess goes wrong where a text is not of one language, so the starts of the first eight paragraphs are also written
with a name or a sign of another language behind them (`NAMES_AND_SIGNS`: `Žižek`, `5 µm`, `£5`): the first rule tried here read French
with a Slavic name in it as Czech, and no text of one language showed it.

The statistical part of the guess (charset-normalizer) gives some samples two code pages that are nearly as likely, and
which comes first is not the same under every version of Python: 12 of 47,684 samples are read otherwise under 3.13 and
3.14 than under 3.12. The baseline is written under Python 3.12, which is what the workflow runs
(`uv run --python 3.12 python -m evals.encodings …`), and the card says the versions it was made with.

A run is compared with a baseline, which keeps of every sample whether it was read right, and ends with status 1 when a
sample that was read right is read wrong, or a code page and language of the baseline is not in the run. The numbers
alone would not do: the rule that read French with a Slavic name as Czech read more French right than it lost, and
every count of French went up. `--show` prints that many of the samples each code page and language lost, as the
reader read them.
"""

import argparse
from collections.abc import Iterator
from importlib import metadata
import json
from pathlib import Path
import sys
import unicodedata

from opendockit.extractors._encoding import decode_text


# The code pages each language is written in here: Windows, ISO 8859, DOS and Macintosh
PAGES = {
    "cp1252": ("fra", "deu_1996", "spa", "ita", "por_PT", "por_BR", "nld", "swe", "dan", "nob", "nno", "fin", "isl", "cat", "glg",
               "eus", "gle", "als", "ind", "eng", "afr", "fao", "ltz", "sco", "bre", "cym", "smo", "swh", "tgl", "hau_NG", "som",
               "zul"),
    "iso8859_15": ("fra", "fin", "spa"), "cp850": ("fra", "spa", "deu_1996"), "mac_roman": ("fra", "deu_1996"),
    "cp1250": ("pol", "ces", "slk", "hun", "hrv", "slv", "ron_2006", "bos_latn"),
    "iso8859_2": ("pol", "ces", "hun", "hrv", "ron_2006"),
    "cp1257": ("lit", "lav", "est"), "iso8859_13": ("lit", "lav"),
    "cp1254": ("tur",), "iso8859_9": ("tur",), "iso8859_3": ("mlt", "epo"),
    "cp1258": ("vie",),
    "cp1251": ("rus", "ukr", "bul", "srp_cyrl", "bel", "mkd"), "koi8_r": ("rus",),
    "cp1253": ("ell_monotonic",), "cp1255": ("heb",), "cp1256": ("arb",), "cp874": ("tha",),
    "cp932": ("jpn",), "gbk": ("cmn_hans",),
}
# What stands in a text of another language: names of people and places, and signs
NAMES_AND_SIGNS = ("5 µm", "Miloš", "Žižek", "Søren", "Jørgensen", "Hämäläinen", "Müller", "naïf", "Škoda", "Dvořák", "Łódź",
                   "Wałęsa", "São Paulo", "Málaga", "Zürich", "Citroën", "Nestlé", "Håkon", "Þór", "Erdoğan", "Björk",
                   "Françoise", "Señor", "Ångström", "m³", "£5", "±2", "25°C", "©", "®", "½", "«x»", "§ 5", "€10", "“x”",
                   "it’s", "–", "…", "™", "×", "Győr", "Ibáñez", "Gdańsk", "Kraków", "Plzeň", "Šiauliai", "Rīga", "Tõnu")
_PARAGRAPHS = 40   # of a text, from its start
_WITH_A_NAME = 8   # starts of paragraphs each name and sign is written behind
_START_CHARS = 90
_ASCII_ROWS = [f"Item {number};Quantity {number * 3};Unit price {number + 10}.00;Status OK" for number in range(1, 21)]
PLAIN, MIXED = "one language", "with a name or a sign"


def _start(line: str) -> str:
    """The start of a paragraph, cut at a space."""
    if len(line) <= _START_CHARS:
        return line
    head = line[:_START_CHARS]
    return head[:head.rfind(" ")] if " " in head else head


def samples(lines: list[str]) -> Iterator[tuple[str, str]]:
    """The cuts of one text as (`PLAIN` or `MIXED`, text)."""
    lines = lines[:_PARAGRAPHS]
    for line in lines:
        yield PLAIN, _start(line)
        yield PLAIN, line
    for first in range(0, len(lines) - 3, 4):
        yield PLAIN, "\n\n".join(lines[first:first + 4])
    yield PLAIN, "\n\n".join(lines)
    for line in lines[:_PARAGRAPHS // 2]:
        yield PLAIN, "\n".join([*_ASCII_ROWS[:10], _start(line), *_ASCII_ROWS[10:]])
    for line in lines[:_WITH_A_NAME]:
        for name in NAMES_AND_SIGNS:
            yield MIXED, f"{_start(line)} {name} ok."


def encoded(text: str, page: str) -> bytes | None:
    """`text` in the code page, None when the code page cannot write it. Windows-1258 writes most tone marks of Vietnamese as
    characters of their own behind the vowel."""
    try:
        return text.encode(page)
    except UnicodeEncodeError:
        if page != "cp1258":
            return None
    data = bytearray()
    for character in text:
        try:
            data += character.encode(page)
        except UnicodeEncodeError:
            parts = unicodedata.normalize("NFD", character)
            try:
                data += unicodedata.normalize("NFC", parts[:-1]).encode(page) + parts[-1].encode(page)
            except UnicodeEncodeError:
                return None
    return bytes(data)


def _read_right(data: bytes, page: str, *, markup: bool) -> bool:
    text, _ = decode_text(data, markup=markup)
    return unicodedata.normalize("NFC", text) == unicodedata.normalize("NFC", data.decode(page))


def run(texts: Path) -> tuple[dict, dict[str, dict[str, dict[int, str]]]]:
    """The card of the folder - for each code page and language, and for the samples of one language and those with a name
    or a sign, which samples were read right (`_marks`: a letter a sample, in the order `samples` gives them, as text and
    then as HTML), and the versions the run was made with - and what the reader made of the samples read wrong, by code page and
    language, kind and place among the marks. The card holds no text."""
    cells: dict[str, dict[str, str]] = {}
    wrong: dict[str, dict[str, dict[int, str]]] = {}
    for page, languages in PAGES.items():
        for language in languages:
            path = texts / f"{language}.txt"
            if not path.is_file():
                continue
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            marks: dict[str, list[str]] = {PLAIN: [], MIXED: []}
            for kind, text in samples(lines):
                data = encoded(text, page)
                if data is None or data.isascii():
                    continue
                html = encoded("<html><head><title>x</title></head><body>"
                               + "".join(f"<p>{line}</p>" for line in text.split("\n")) + "</body></html>", page)
                for payload, markup in ((data, False), (html, True)):
                    right = _read_right(payload, page, markup=markup)
                    marks[kind].append(_RIGHT if right else _WRONG)
                    if not right:
                        wrong.setdefault(f"{page} {language}", {}).setdefault(kind, {})[len(marks[kind]) - 1] = \
                            decode_text(payload, markup=markup)[0][:120]
            if not marks[PLAIN] and not marks[MIXED]:
                continue   # a text none of whose samples the code page can write
            cells[f"{page} {language}"] = {kind: "".join(marks[kind]) for kind in (PLAIN, MIXED)}
    totals = {kind: [sum(cell[kind].count(_RIGHT) for cell in cells.values()), sum(len(cell[kind]) for cell in cells.values())]
              for kind in (PLAIN, MIXED)}
    return {"cells": cells, "totals": totals, "versions": _versions()}, wrong


def _versions() -> dict[str, str]:
    """What the statistical part of the guess is run with."""
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "charset-normalizer": metadata.version("charset-normalizer")}


_RIGHT, _WRONG = "1", "0"


def compare(card: dict, baseline: dict) -> tuple[list[str], dict[str, dict[str, list[int]]]]:
    """The lines of the comparison (the totals, and each code page and language that moved) and those that are worse, each
    with the places of the samples it lost by kind: a sample that was read right in the baseline and is read wrong, in
    either kind. A cell whose samples are not those of the baseline (another text, another list of names) and a cell of
    the baseline that the run does not have (a text missing from the folder) are said to be, and count as worse without a
    place: the baseline has to be written again with the texts it was made from."""
    lines: list[str] = []
    worse: dict[str, dict[str, list[int]]] = {}
    if baseline.get("versions") != card["versions"]:
        # Two code pages nearly as likely are not ranked alike under every version: a few samples go the other way
        lines.append(f"the baseline was written with {_said(baseline.get('versions'))}, this run has {_said(card['versions'])}: "
                     "run it with those of the baseline before believing a lost sample")
    for kind in (PLAIN, MIXED):
        before, now = baseline["totals"].get(kind, [0, 0]), card["totals"][kind]
        lines.append(f"{kind:<26}{before[0]:>8} of {before[1]:<8}{now[0]:>8} of {now[1]:<8}{now[0] - before[0]:>+8}")
    for name in sorted(set(card["cells"]) | set(baseline["cells"])):
        before, now = baseline["cells"].get(name), card["cells"].get(name)
        if before is None:
            lines.append(f"  {name:<24}new")
            continue
        if now is None:
            lines.append(f"  {name:<24}only in the baseline")
            worse[name] = {}
            continue
        moved, lost, other = [], {}, False
        for kind in (PLAIN, MIXED):
            if len(before[kind]) != len(now[kind]):
                moved.append(f"{kind}: {len(before[kind])} samples in the baseline, {len(now[kind])} now")
                other = True
            elif before[kind] != now[kind]:
                gone = [place for place, (was, is_) in enumerate(zip(before[kind], now[kind], strict=True))
                        if was == _RIGHT and is_ == _WRONG]
                moved.append(f"{kind}: {before[kind].count(_RIGHT)} -> {now[kind].count(_RIGHT)} of {len(now[kind])}"
                             + (f", {len(gone)} lost" if gone else ""))
                if gone:
                    lost[kind] = gone
        if moved:
            lines.append(f"  {name:<24}" + "; ".join(moved))
        if lost or other:
            worse[name] = lost
    return lines, worse


def _said(versions: dict | None) -> str:
    return ", ".join(f"{name} {version}" for name, version in sorted((versions or {}).items())) or "versions it does not name"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--texts", type=Path, required=True, help="folder of <language>.txt (evals.fetch_encoding_texts)")
    parser.add_argument("--baseline", type=Path, help="the card to compare with")
    parser.add_argument("--update-baseline", action="store_true", help="write this run to --baseline instead of comparing")
    parser.add_argument("--show", type=int, default=0, help="how many of the samples it lost to print of each code page and language")
    arguments = parser.parse_args(argv)
    if not arguments.texts.is_dir():
        parser.error(f"no such folder: {arguments.texts}")

    card, wrong = run(arguments.texts)
    if not card["cells"]:
        parser.error(f"no text of a language of the scorecard in {arguments.texts}")
    for kind in (PLAIN, MIXED):
        right, number = card["totals"][kind]
        print(f"{kind}: {right} of {number} read right")
    if arguments.baseline is None:
        return 0
    if arguments.update_baseline:
        arguments.baseline.write_text(json.dumps(card, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    lines, worse = compare(card, json.loads(arguments.baseline.read_text(encoding="utf-8")))
    print(f"{'':<26}{'baseline':>20}{'now':>20}")
    print("\n".join(lines))
    for name, lost in worse.items():
        shown = [(kind, place) for kind, places in lost.items() for place in places][:arguments.show]
        for kind, place in shown:
            print(f"--- {name}, {kind}: {wrong[name][kind][place]!r}")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
