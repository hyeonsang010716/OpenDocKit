"""Count, for each tool, the words of the corpus documents that are missing from what it wrote.

    PYTHONPATH=.. python recall_corpus.py <results-dir>

A DOCX, PPTX, XLSX or HWPX states its text in its own XML, and an HWP 5 file in the text records of its paragraphs. The
words read from there (`evals/recall.py::source_units`) are looked for in each tool's output of the `ordinary` suite: a
word that is nowhere in it was lost. Nobody has to know the right Markdown for that. It counts only the documents a tool
returned text for, and says how many those were. Writes <results-dir>/recall.json.
"""

import json
from pathlib import Path
import sys

from evals.recall import SUFFIXES, compare_text, source_units


NAMES = {".docx": "DOCX", ".pptx": "PPTX", ".xlsx": "XLSX", ".hwpx": "HWPX", ".hwp": "HWP"}


def main() -> None:
    results = Path(sys.argv[1])
    files = [item for item in json.loads((results / "ordinary.json").read_text()) if Path(item["path"]).suffix.lower() in SUFFIXES
             and Path(item["path"]).suffix.lower() in NAMES]
    sources = {}
    for item in files:
        try:
            sources[item["id"]] = source_units(Path(item["path"]))
        except Exception:  # noqa: BLE001 - a document whose own text cannot be read is no reference for anyone
            continue
    summary = {}
    for report in sorted((results / "ordinary").glob("*.json")):
        tool = report.stem
        replies = json.loads(report.read_text())["results"]
        per_format: dict[str, dict[str, int]] = {}
        for item in files:
            if item["id"] not in sources:
                continue
            bucket = per_format.setdefault(NAMES[Path(item["path"]).suffix.lower()], {"documents": 0, "read": 0, "words": 0, "missing": 0, "joined": 0})
            bucket["documents"] += 1
            output = results / "ordinary" / tool / f"{item['id']}.md"
            if replies.get(item["id"], {}).get("outcome") != "ok" or not output.exists():
                continue
            counted = compare_text(sources[item["id"]], output.read_text(encoding="utf-8", errors="replace"))
            bucket["read"] += 1
            bucket["words"] += counted["words"]
            bucket["missing"] += len(counted["missing"])
            bucket["joined"] += len(counted["joined"])
        summary[tool] = per_format
        print(f"{tool:13}", per_format, flush=True)
    (results / "recall.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
