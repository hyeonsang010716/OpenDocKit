"""What does a change do to the text of a folder of documents? Convert them before and after, and say what moved.

    uv run python -m evals.changes --documents <dir> [<dir> …] --save before.json.gz      # on main
    uv run python -m evals.changes --documents <dir> [<dir> …] --against before.json.gz   # with the change

The scorecards give a number for a corpus that has a reference. Most documents have none, and a number does not say what a
rule did to a document: a mean can stand still while forty texts change. This reads the change itself. Run on any folder of
documents, with the reader as it was and as it is, it lists

- the documents whose text changed, and their changed blocks by kind (a table that became text, text that became a table, a
  heading that appeared), with the first of each document to read;
- the words that are gone: a word a document held before and holds nowhere now. A word written fewer times is not gone —
  a value repeated for each row it spans is written once by a table that became paragraphs;
- the headings that appeared or went;
- the documents that are refused now and were converted before (the run ends with status 1 for those), and the other way;
- the warnings that changed;
- the twins — an HWP and an HWPX of one name in one folder, one document in two formats — that converted to the same text
  before and do not now;
- the time of the run on each side. One run on a machine that does other work is not a measurement: a difference under a
  few percent says nothing, and the cost of a rule is found by timing the function it lives in over every call the corpus
  makes of it (`evals/README.md`).

It judges nothing but the refusals. What it lists is what a pull request has to account for.
"""

import argparse
from collections import Counter
from difflib import SequenceMatcher
import gzip
import json
from pathlib import Path
import re
import sys
import time

from opendockit import ConversionError, convert_file


_WORD = re.compile(r"[^\W_]+")
_TABLE_MARKUP = re.compile(r"</?(?:table|tr|td|th)[^>]*>|<br>")
_HEADING = re.compile(r"(?m)^#{1,6} .*$")
_PIPE_ROW = re.compile(r"(?m)^\|.*\|$")
_TWIN_SUFFIXES = (".hwp", ".hwpx")
SHOWN = 12  # the documents listed in full without --show


def convert_folders(folders: list[Path]) -> dict:
    """The conversion of every file under `folders`: its text, or the name of the error that refused it, its warnings and
    the seconds it took. A file is named by its folder and its place in it, so two checkouts name it alike."""
    documents = {}
    for folder in folders:
        for path in sorted(item for item in folder.rglob("*") if item.is_file()):
            name = f"{folder.name}/{path.relative_to(folder).as_posix()}"
            start = time.perf_counter()
            try:
                result = convert_file(path)
                entry = {"text": result.text, "warnings": list(result.warnings)}
            except ConversionError as error:
                entry = {"refused": type(error).__name__}
            except Exception as error:  # noqa: BLE001
                # a defect of the reader, not a refusal: named apart, and listed with the refusals
                entry = {"refused": f"unexpected {type(error).__name__}"}
            entry["seconds"] = round(time.perf_counter() - start, 4)
            documents[name] = entry
    return {"documents": documents}


def _words(text: str) -> Counter[str]:
    return Counter(_WORD.findall(_TABLE_MARKUP.sub(" ", text)))


def _kind(block: str) -> str:
    return "an HTML table" if "<table>" in block else "a pipe table" if _PIPE_ROW.search(block) else "text" if block.strip() \
        else "nothing"


def changed_blocks(before: str, after: str) -> list[tuple[str, str]]:
    """The stretches of blocks (paragraphs, tables: what a blank line parts) that differ, each as (before, after)."""
    first, second = before.split("\n\n"), after.split("\n\n")
    matcher = SequenceMatcher(None, first, second, autojunk=False)
    return [("\n\n".join(first[a:b]), "\n\n".join(second[c:d])) for tag, a, b, c, d in matcher.get_opcodes() if tag != "equal"]


def _twins(documents: dict) -> list[tuple[str, str]]:
    names = set(documents)
    return sorted((name, name[:-len(".hwp")] + ".hwpx") for name in names
                  if name.endswith(".hwp") and name[:-len(".hwp")] + ".hwpx" in names)


def _alike(documents: dict, pair: tuple[str, str]) -> bool:
    first, second = (documents[name].get("text") for name in pair)
    return first is not None and first == second


def compare(before: dict, after: dict, shown: int = SHOWN) -> tuple[list[str], bool]:
    """The lines of the comparison, and whether a document that was converted is refused now."""
    old, new = before["documents"], after["documents"]
    common = [name for name in old if name in new]
    lines = [f"{len(common)} documents in both runs" + (f" ({len(old) - len(common)} only before, {len(new) - len(common)} only now)"
                                                         if len(old) != len(common) or len(new) != len(common) else "")]
    refused_now = [name for name in common if "refused" in new[name] and "refused" not in old[name]]
    read_now = [name for name in common if "refused" in old[name] and "refused" not in new[name]]
    lines.append(f"refused: {sum('refused' in old[name] for name in common)} before, {sum('refused' in new[name] for name in common)} now")
    lines.extend(f"  REFUSED NOW  {name}  {new[name]['refused']}" for name in refused_now)
    lines.extend(f"  read now  {name}  (was {old[name]['refused']})" for name in read_now)

    changed = [name for name in common if "text" in old[name] and "text" in new[name] and old[name]["text"] != new[name]["text"]]
    kinds: Counter[str] = Counter()
    gone: dict[str, Counter[str]] = {}
    headings = {}
    first_blocks = {}
    for name in changed:
        blocks = changed_blocks(old[name]["text"], new[name]["text"])
        kinds.update(f"{_kind(was)} -> {_kind(now)}" for was, now in blocks)
        first_blocks[name] = (len(blocks), *blocks[0])
        was_words, now_words = _words(old[name]["text"]), _words(new[name]["text"])
        lost = Counter({word: count for word, count in was_words.items() if word not in now_words})
        if lost:
            gone[name] = lost
        was_headings, now_headings = (Counter(_HEADING.findall(side[name]["text"])) for side in (old, new))
        if was_headings != now_headings:
            headings[name] = (sum((now_headings - was_headings).values()), sum((was_headings - now_headings).values()))

    lines.append(f"texts changed: {len(changed)}")
    lines.extend(f"  {count:5}  {kind}" for kind, count in kinds.most_common())
    lines.append(f"documents with a word that is gone: {len(gone)}")
    lines.extend(f"  {name}  {sum(lost.values())} gone: {' '.join(list(lost)[:8])}" for name, lost in list(gone.items())[:shown])
    lines.append(f"documents whose headings changed: {len(headings)}"
                 + (f" ({sum(added for added, _ in headings.values())} appeared, {sum(removed for _, removed in headings.values())} went)"
                    if headings else ""))
    lines.extend(f"  {name}  +{added} -{removed}" for name, (added, removed) in list(headings.items())[:shown])

    warned = [name for name in common if old[name].get("warnings", []) != new[name].get("warnings", [])
              and "refused" not in old[name] and "refused" not in new[name]]
    lines.append(f"documents whose warnings changed: {len(warned)}")
    lines.extend(f"  {name}" for name in warned[:shown])

    pairs = [pair for pair in _twins(old) if pair[0] in new and pair[1] in new]
    apart = [pair for pair in pairs if _alike(old, pair) and not _alike(new, pair)]
    if pairs:
        lines.append(f"twins (HWP and HWPX of one name): {len(pairs)} pairs, {sum(_alike(old, pair) for pair in pairs)} alike before, "
                     f"{sum(_alike(new, pair) for pair in pairs)} alike now")
        lines.extend(f"  APART NOW  {first}" for first, _ in apart[:shown])

    lines.append(f"seconds: {sum(old[name]['seconds'] for name in common):.1f} before, "
                 f"{sum(new[name]['seconds'] for name in common):.1f} now (one run each: read the module text before reading this)")
    for name in changed[:shown]:
        count, was, now = first_blocks[name]
        lines.append(f"--- {name}: {count} changed block(s); the first")
        lines.append("  was: " + was[:300].replace("\n", "\n       "))
        lines.append("  now: " + now[:300].replace("\n", "\n       "))
    if len(changed) > shown:
        lines.append(f"... and {len(changed) - shown} more (--show {len(changed)})")
    return lines, bool(refused_now)


def _read(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--documents", type=Path, nargs="+", required=True, help="folders of documents, read with their subfolders")
    parser.add_argument("--save", type=Path, help="write this run here (gzip JSON), to compare a later run with")
    parser.add_argument("--against", type=Path, help="a run saved with --save: say what changed since")
    parser.add_argument("--show", type=int, default=SHOWN, help="how many documents to list under each heading")
    args = parser.parse_args(argv)
    if (args.save is None) == (args.against is None):
        parser.error("give --save or --against")
    missing = [str(folder) for folder in args.documents if not folder.is_dir()]
    if missing:
        parser.error(f"no such folder: {', '.join(missing)}")

    run = convert_folders(args.documents)
    if args.save is not None:
        with gzip.open(args.save, "wt", encoding="utf-8") as handle:
            json.dump(run, handle, ensure_ascii=False)
        print(f"{len(run['documents'])} documents written to {args.save}")
        return 0
    lines, refused = compare(_read(args.against), run, args.show)
    print("\n".join(lines))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
