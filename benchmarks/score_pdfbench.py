"""Score what each tool wrote for the 200 PDFs of opendataloader-bench against that benchmark's references.

    PYTHONPATH=.. python score_pdfbench.py <results-dir> <references-dir>

The scorer is OpenDocKit's scorecard (`evals/`): reading order (NID), tables (TEDS) and headings (MHS) as that benchmark
defines them, and their mean. `evals/README.md` says how it was checked against the per-document scores the benchmark
publishes. A document a tool gave no text for is scored as an empty text. Writes <results-dir>/pdfbench-scores.json.
"""

import json
from pathlib import Path
import sys

from evals.run import run


def main() -> None:
    results, gold = Path(sys.argv[1]), Path(sys.argv[2])
    scores = {}
    for folder in sorted(path for path in (results / "pdfbench").iterdir() if path.is_dir()):
        card = run(gold, None, folder, None)
        scores[folder.name] = {"means": card["means"], "documents": card["documents"]}
        print(f"{folder.name:13}", {metric: value["mean"] for metric, value in card["means"].items()}, flush=True)
    (results / "pdfbench-scores.json").write_text(json.dumps(scores, indent=1))


if __name__ == "__main__":
    main()
