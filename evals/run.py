"""Score OpenDocKit's output for a folder of documents against their reference Markdown, and compare with a baseline.

    uv run python -m evals.run --documents <dir> --gold <dir> [--baseline <file>] [--update-baseline] [--out <dir>]
    uv run python -m evals.run --predictions <dir> --gold <dir>      # score Markdown another converter already wrote

A document `<name>.<ext>` is scored against `<gold>/<name>.md`. With `--baseline` the run ends with status 1 when the
mean of a metric fell by more than `--tolerance`, and lists the documents that moved either way, so a change to a rule
that guesses structure is judged by all the documents and not by the one it was written for (`evals/README.md`).
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import sys
import time

from evals.metrics import score
from opendockit import ConversionError, ConvertOptions, convert


METRICS = ("overall", "nid", "nid_s", "teds", "teds_s", "mhs", "mhs_s")
# Page markers and picture placeholders are OpenDocKit's own marks, not text of the document, so they are left out of
# what is scored; everything else is the default conversion.
OPTIONS = ConvertOptions(page_markers=False, extract_images=False)
WORKERS = 4
MOVED = 0.02  # a document is listed when one of its metrics moved by more than this


def _convert(path: Path) -> tuple[str, float]:
    started = time.perf_counter()
    try:
        text = convert(path.read_bytes(), path.name, OPTIONS).text
    except ConversionError:
        text = ""
    return text, time.perf_counter() - started


def _score_document(job: tuple[str, str | None, str | None, str, str | None]) -> tuple[str, dict[str, float | None], float]:
    name, document, prediction, gold, out = job
    if document is not None:
        text, seconds = _convert(Path(document))
        if out is not None:
            Path(out, f"{name}.md").write_text(text, encoding="utf-8")
    else:
        text, seconds = (Path(prediction).read_text(encoding="utf-8") if prediction and Path(prediction).is_file() else ""), 0.0
    return name, score(Path(gold).read_text(encoding="utf-8"), text).as_dict(), seconds


def _means(documents: dict[str, dict[str, float | None]]) -> dict[str, dict[str, float | int | None]]:
    means: dict[str, dict[str, float | int | None]] = {}
    for metric in METRICS:
        values = [scores[metric] for scores in documents.values() if scores[metric] is not None]
        means[metric] = {"mean": round(sum(values) / len(values), 4) if values else None, "documents": len(values)}
    return means


def run(gold: Path, documents: Path | None, predictions: Path | None, out: Path | None) -> dict:
    """Score every document that has a reference. Returns the scorecard."""
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    jobs = []
    for reference in sorted(gold.glob("*.md")):
        source = None
        if documents is not None:
            found = sorted(path for path in documents.glob(f"{reference.stem}.*") if path.suffix.lower() != ".md")
            if not found:
                continue
            source = str(found[0])
        prediction = str(predictions / reference.name) if predictions is not None else None
        jobs.append((reference.stem, source, prediction, str(reference), str(out) if out is not None else None))
    with ProcessPoolExecutor(WORKERS) as pool:
        results = list(pool.map(_score_document, jobs, chunksize=4))
    scored = {name: {metric: None if value is None else round(value, 4) for metric, value in scores.items()} for name, scores, _ in results}
    return {"means": _means(scored), "seconds": round(sum(seconds for _, _, seconds in results), 1), "documents": scored}


def compare(card: dict, baseline: dict, tolerance: float) -> tuple[list[str], bool]:
    """The lines of a comparison with `baseline`, and whether the mean of a metric fell by more than `tolerance`."""
    lines, fell = [], False
    lines.append(f"{'metric':8} {'baseline':>9} {'now':>9} {'change':>9}")
    for metric in METRICS:
        before, now = baseline["means"][metric]["mean"], card["means"][metric]["mean"]
        if before is None or now is None:
            continue
        mark = ""
        if now < before - tolerance:
            fell, mark = True, "  FELL"
        lines.append(f"{metric:8} {before:9.4f} {now:9.4f} {now - before:+9.4f}{mark}")
    moved = []
    for name, scores in card["documents"].items():
        old = baseline["documents"].get(name)
        if old is None:
            continue
        changes = [(scores[metric] - old[metric], metric) for metric in ("nid", "teds", "mhs")
                   if scores[metric] is not None and old[metric] is not None and abs(scores[metric] - old[metric]) > MOVED]
        if changes:
            moved.append((min(change for change, _ in changes), name, changes))
    moved.sort()
    worse = [entry for entry in moved if entry[0] < 0]
    lines.append(f"documents that moved by more than {MOVED}: {len(moved)} ({len(worse)} with a metric that fell)")
    for _, name, changes in moved:
        lines.append(f"  {name}  " + "  ".join(f"{metric} {change:+.3f}" for change, metric in changes))
    return lines, fell


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, required=True, help="folder of reference Markdown, one <name>.md per document")
    parser.add_argument("--documents", type=Path, help="folder of documents to convert with OpenDocKit")
    parser.add_argument("--predictions", type=Path, help="folder of Markdown already converted, scored as it is")
    parser.add_argument("--out", type=Path, help="folder to write the converted Markdown and scores.json to")
    parser.add_argument("--baseline", type=Path, help="scorecard to compare with")
    parser.add_argument("--update-baseline", action="store_true", help="write this run to --baseline instead of comparing")
    parser.add_argument("--tolerance", type=float, default=0.002, help="how far the mean of a metric may fall")
    args = parser.parse_args(argv)
    if (args.documents is None) == (args.predictions is None):
        parser.error("give one of --documents and --predictions")

    card = run(args.gold, args.documents, args.predictions, args.out)
    for metric in METRICS:
        mean = card["means"][metric]
        print(f"{metric:8} {'none' if mean['mean'] is None else format(mean['mean'], '.4f'):>7}  over {mean['documents']} documents")
    if args.out is not None:
        (args.out / "scores.json").write_text(json.dumps(card, indent=1) + "\n", encoding="utf-8")
    if args.baseline is None:
        return 0
    if args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps({key: card[key] for key in ("means", "documents")}, indent=1) + "\n", encoding="utf-8")
        return 0
    lines, fell = compare(card, json.loads(args.baseline.read_text(encoding="utf-8")), args.tolerance)
    print("\n".join(lines))
    return 1 if fell else 0


if __name__ == "__main__":
    sys.exit(main())
