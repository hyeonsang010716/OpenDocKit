"""Score OpenDocKit's output for a folder of documents against their reference Markdown, and compare with a baseline.

    uv run python -m evals.run --documents <dir> --gold <dir> [--baseline <file>] [--update-baseline] [--out <dir>]
    uv run python -m evals.run --predictions <dir> --gold <dir>      # score Markdown another converter already wrote

A document `<name>.<ext>` is scored against `<gold>/<name>.md`. A reference that is not Markdown is a document itself —
the same text in a format that states its structure, the HWPX of a PDF — and is converted first, so its score says how
far the two readers agree, which is as good as the reader of the reference is. With `--baseline` the run ends with status 1 when the
mean of a metric fell by more than `--tolerance`, and lists the documents that moved either way, so a change to a rule
that guesses structure is judged by all the documents and not by the one it was written for (`evals/README.md`). It
also says how many documents came out as another text than the baseline's, moved or not: a mean can stand still while
the documents under it change.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time

from evals.metrics import score
from opendockit import ConversionError, ConvertOptions, convert
from opendockit.cli import _write_output_as_utf8_lines


METRICS = ("overall", "nid", "nid_s", "teds", "teds_s", "mhs", "mhs_s", "words")
# Page markers and picture placeholders are OpenDocKit's own marks, not text of the document, so they are left out of
# what is scored; everything else is the default conversion.
OPTIONS = ConvertOptions(page_markers=False, extract_images=False)
# For a reference written without them (`make_twins.py`): no emphasis and no links either
PLAIN_OPTIONS = ConvertOptions(page_markers=False, extract_images=False, keep_emphasis=False, keep_links=False)
WORKERS = 4
MOVED = 0.02  # a document is listed when one of its metrics moved by more than this
CHANGED_SHOWN = 40  # how many of the documents whose text changed without a score moving are named
ABSENT_SHARE = 0.1  # the share of a baseline's documents a run may lack and still be compared (2 of 60 Korean pairs are gone)


def _convert(path: Path, plain: bool = False) -> tuple[str, float]:
    started = time.perf_counter()
    try:
        text = convert(path.read_bytes(), path.name, PLAIN_OPTIONS if plain else OPTIONS).text
    except ConversionError:
        text = ""
    return text, time.perf_counter() - started


def _digest(text: str) -> str:
    """What stands for the text of a document in a scorecard: enough of its SHA-256 to tell two texts apart."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _score_document(job: tuple[str, str | None, str | None, str, str | None, bool]) -> tuple[str, dict[str, float | None], float, str]:
    name, document, prediction, gold, out, plain = job
    if document is not None:
        text, seconds = _convert(Path(document), plain)
        if out is not None:
            Path(out, f"{name}.md").write_text(text, encoding="utf-8")
    else:
        text, seconds = (Path(prediction).read_text(encoding="utf-8") if prediction and Path(prediction).is_file() else ""), 0.0
    reference = Path(gold).read_text(encoding="utf-8") if gold.endswith(".md") else _convert(Path(gold), plain)[0]
    return name, score(reference, text).as_dict(), seconds, _digest(text)


def _means(documents: dict[str, dict[str, float | None]]) -> dict[str, dict[str, float | int | None]]:
    means: dict[str, dict[str, float | int | None]] = {}
    for metric in METRICS:
        # a baseline written before a metric was added has no number for it
        values = [scores[metric] for scores in documents.values() if scores.get(metric) is not None]
        means[metric] = {"mean": round(sum(values) / len(values), 4) if values else None, "documents": len(values)}
    return means


def named(listing: Path) -> frozenset[str]:
    """The document names of a list: one a line, blank lines and lines that start with `#` left out."""
    lines = (line.strip() for line in listing.read_text(encoding="utf-8").splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


def run(gold: Path, documents: Path | None, predictions: Path | None, out: Path | None, plain: bool = False,
        only: frozenset[str] | None = None, without: frozenset[str] = frozenset()) -> dict:
    """Score every document that has a reference — of the names in `only` when it is given, and none of those in
    `without` (the two halves of a benchmark: the documents rules may be written on, and the holdout). Returns the
    scorecard."""
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
    jobs = []
    for reference in sorted(path for path in gold.iterdir() if path.is_file() and not path.name.startswith(".")):
        if reference.stem in without or (only is not None and reference.stem not in only):
            continue
        source = None
        if documents is not None:
            found = sorted(path for path in documents.glob(f"{reference.stem}.*") if path.suffix.lower() != ".md")
            if not found:
                continue
            source = str(found[0])
        prediction = str(predictions / f"{reference.stem}.md") if predictions is not None else None
        jobs.append((reference.stem, source, prediction, str(reference), str(out) if out is not None else None, plain))
    with ProcessPoolExecutor(WORKERS) as pool:
        results = list(pool.map(_score_document, jobs, chunksize=4))
    scored = {name: {metric: None if value is None else round(value, 4) for metric, value in scores.items()}
              for name, scores, _, _ in results}
    return {"means": _means(scored), "seconds": round(sum(seconds for _, _, seconds, _ in results), 1), "documents": scored,
            "texts": {name: digest for name, _, _, digest in results}}


def compare(card: dict, baseline: dict, tolerance: float, gate: tuple[str, ...] = METRICS) -> tuple[list[str], bool]:
    """The lines of a comparison with `baseline`, and whether the mean of a metric in `gate` fell by more than `tolerance`."""
    lines, fell = [], False
    # A document of the baseline that is not in this run (korea.kr no longer serves it, a folder was left out), or one of
    # the run that is not in the baseline, would move a mean without the reader having changed: the means are then those
    # of the documents in both, on both sides, and the line above the table names the others.
    before_means, now_means = baseline["means"], card["means"]
    absent = sorted(baseline["documents"].keys() - card["documents"].keys())
    added = sorted(card["documents"].keys() - baseline["documents"].keys())
    if absent or added:
        both = baseline["documents"].keys() & card["documents"].keys()
        before_means = _means({name: baseline["documents"][name] for name in both})
        now_means = _means({name: card["documents"][name] for name in both})
        for count, names, where in ((len(absent), absent, "of the baseline not in this run"),
                                    (len(added), added, "of this run not in the baseline")):
            if count:
                shown = ", ".join(names[:CHANGED_SHOWN]) + (f" and {count - CHANGED_SHOWN} more" if count > CHANGED_SHOWN else "")
                lines.append(f"documents {where}: {count} ({shown})")
        # a fetch that broke, or a folder of other documents, would be compared on what little is left, and pass
        if not both or len(absent) > len(baseline["documents"]) * ABSENT_SHARE:
            lines.append(f"too few documents of the baseline in this run to compare: {len(both)} of {len(baseline['documents'])}")
            return lines, True
        lines.append(f"the means below are those of the {len(both)} documents in both")
    lines.append(f"{'metric':8} {'baseline':>9} {'now':>9} {'change':>9}")
    for metric in METRICS:
        # a baseline written before a metric was added has no number for it
        before, now = before_means.get(metric, {}).get("mean"), now_means[metric]["mean"]
        if before is None or now is None:
            continue
        mark = ""
        if now < before - tolerance:
            fell, mark = fell or metric in gate, "  FELL" if metric in gate else "  fell (not gated)"
        lines.append(f"{metric:8} {before:9.4f} {now:9.4f} {now - before:+9.4f}{mark}")
    moved = []
    for name, scores in card["documents"].items():
        old = baseline["documents"].get(name)
        if old is None:
            continue
        changes = [(scores[metric] - old[metric], metric) for metric in ("nid", "teds", "mhs", "words")
                   if scores[metric] is not None and old.get(metric) is not None and abs(scores[metric] - old[metric]) > MOVED]
        if changes:
            moved.append((min(change for change, _ in changes), name, changes))
    moved.sort()
    worse = [entry for entry in moved if entry[0] < 0]
    lines.append(f"documents that moved by more than {MOVED}: {len(moved)} ({len(worse)} with a metric that fell)")
    for _, name, changes in moved:
        lines.append(f"  {name}  " + "  ".join(f"{metric} {change:+.3f}" for change, metric in changes))
    # The text of each document against the baseline's (`texts`, absent from a baseline written before it was kept): a
    # document whose text changed and whose scores did not move by `MOVED` is named, the others are in the list above
    before = baseline.get("texts")
    if before is not None and "texts" in card:
        changed = sorted(name for name, digest in card["texts"].items() if name in before and before[name] != digest)
        quiet = [name for name in changed if name not in {name for _, name, _ in moved}]
        lines.append(f"documents whose text changed: {len(changed)} of {len(card['texts'])} ({len(quiet)} without a score that moved)")
        lines.extend(f"  {name}" for name in quiet[:CHANGED_SHOWN])
        if len(quiet) > CHANGED_SHOWN:
            lines.append(f"  … and {len(quiet) - CHANGED_SHOWN} more")
    return lines, fell


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, required=True, help="folder of references, one <name>.md (or a document to convert) per document")
    parser.add_argument("--documents", type=Path, help="folder of documents to convert with OpenDocKit")
    parser.add_argument("--predictions", type=Path, help="folder of Markdown already converted, scored as it is")
    parser.add_argument("--out", type=Path, help="folder to write the converted Markdown and scores.json to")
    parser.add_argument("--baseline", type=Path, help="scorecard to compare with")
    parser.add_argument("--update-baseline", action="store_true", help="write this run to --baseline instead of comparing")
    parser.add_argument("--tolerance", type=float, default=0.002, help="how far the mean of a metric may fall")
    parser.add_argument("--plain", action="store_true", help="convert without emphasis and links, for a reference written without them")
    parser.add_argument("--gate", default=",".join(METRICS), help="the metrics whose fall fails the run, comma-separated (default: all)")
    parser.add_argument("--only", type=Path, help="score only the documents named in this list, one name a line")
    parser.add_argument("--without", type=Path, help="score no document named in this list")
    args = parser.parse_args(argv)
    if (args.documents is None) == (args.predictions is None):
        parser.error("give one of --documents and --predictions")

    card = run(args.gold, args.documents, args.predictions, args.out, args.plain,
               None if args.only is None else named(args.only), frozenset() if args.without is None else named(args.without))
    for metric in METRICS:
        mean = card["means"][metric]
        print(f"{metric:8} {'none' if mean['mean'] is None else format(mean['mean'], '.4f'):>7}  over {mean['documents']} documents")
    if args.out is not None:
        (args.out / "scores.json").write_text(json.dumps(card, indent=1) + "\n", encoding="utf-8")
    if args.baseline is None:
        return 0
    if args.update_baseline:
        try:  # a baseline written from a run that lacks documents compares later runs without them
            dropped = sorted(json.loads(args.baseline.read_text(encoding="utf-8"))["documents"].keys() - card["documents"].keys())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):  # none yet, or not one: it is written anew
            dropped = []
        if dropped:
            shown = ", ".join(dropped[:CHANGED_SHOWN]) + (f" and {len(dropped) - CHANGED_SHOWN} more" if len(dropped) > CHANGED_SHOWN else "")
            print(f"documents of the old baseline not in the new one: {len(dropped)} ({shown})")
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps({key: card[key] for key in ("means", "documents", "texts")}, indent=1) + "\n", encoding="utf-8")
        return 0
    lines, fell = compare(card, json.loads(args.baseline.read_text(encoding="utf-8")), args.tolerance, tuple(args.gate.split(",")))
    print("\n".join(lines))
    return 1 if fell else 0


if __name__ == "__main__":
    _write_output_as_utf8_lines()   # UTF-8 whatever the code page, as the command line (Pinned by: test_evals_output.py)
    sys.exit(main())
