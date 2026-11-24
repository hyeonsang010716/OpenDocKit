"""Score the raw results and write `results/summary.json` (and print the tables).

    python report.py [results-dir]

Scoring is separate from running, so the probes can be re-read and re-applied to the saved outputs without converting
anything again. Every number in benchmarks/README.md comes out of this script.
"""

import json
from pathlib import Path
import statistics
import sys

from probes import COVERAGE, PROBES, contains


TOOLS = ["opendockit", "markitdown", "kreuzberg", "unstructured", "docling", "kordoc"]
MARK = {"pass": "✅", "partial": "🟡", "fail": "❌", "missing": "·"}


def load(results: Path, suite: str, tool: str) -> dict:
    path = results / suite / f"{tool}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def output(results: Path, suite: str, tool: str, file_id: str) -> str:
    path = results / suite / tool / f"{file_id}.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def coverage(results: Path, tools: list[str]) -> dict:
    table = {}
    for case in COVERAGE:
        row = {}
        for tool in tools:
            reply = load(results, "cases", tool).get("results", {}).get(case.file)
            if reply is None:
                row[tool] = {"status": "missing"}
            elif reply["outcome"] != "ok":
                row[tool] = {"status": "fail", "why": reply.get("error") or reply["outcome"]}
            else:
                text = output(results, "cases", tool, case.file)
                found = [needle for needle in case.expect if contains(text, needle)]
                status = "pass" if len(found) == len(case.expect) else ("partial" if found else "fail")
                row[tool] = {"status": status, "found": f"{len(found)}/{len(case.expect)}"}
                if not found:
                    row[tool]["why"] = "output has none of the expected text"
        table[case.file] = row
    return table


def probes(results: Path, tools: list[str]) -> dict:
    table = {}
    for probe in PROBES:
        row = {}
        for tool in tools:
            reply = load(results, "cases", tool).get("results", {}).get(probe.file)
            if reply is None:
                row[tool] = None
                continue
            try:
                row[tool] = bool(reply["outcome"] == "ok" and probe.check(output(results, "cases", tool, probe.file)))
            except Exception:  # noqa: BLE001 — a probe that cannot read an output counts as not satisfied
                row[tool] = False
        table[probe.id] = row
    return table


def corpus(results: Path, suite: str, tools: list[str]) -> dict:
    listing = results / f"{suite}.json"
    if not listing.exists():
        return {}
    formats = {item["id"]: item["format"] for item in json.loads(listing.read_text())}
    summary = {}
    for tool in tools:
        data = load(results, suite, tool)
        if not data:
            continue
        per_format: dict[str, dict] = {}
        for file_id, reply in data["results"].items():
            bucket = per_format.setdefault(formats[file_id], {"files": 0, "ok": 0, "empty": 0, "error": 0, "timeout": 0, "crash": 0,
                                                              "memory": 0, "seconds": [], "peak_rss_mb": 0.0})
            bucket["files"] += 1
            bucket[reply["outcome"]] += 1
            bucket["seconds"].append(reply.get("seconds", 0.0))
            bucket["peak_rss_mb"] = max(bucket["peak_rss_mb"], reply.get("peak_rss_mb", 0.0))
        for bucket in per_format.values():
            seconds = bucket.pop("seconds")
            bucket["total_seconds"] = round(sum(seconds), 1)
            bucket["median_seconds"] = round(statistics.median(seconds), 3)
            bucket["max_seconds"] = round(max(seconds), 1)
            bucket["peak_rss_mb"] = round(bucket["peak_rss_mb"])
        summary[tool] = {"version": data.get("version"), "import_seconds": round(data.get("import_seconds") or 0, 2),
                         "wall_seconds": round(data.get("wall_seconds", 0), 1), "formats": per_format}
    return summary


def saved(results: Path, name: str) -> dict:
    path = results / name
    return json.loads(path.read_text()) if path.exists() else {}


def pdfbench(results: Path, tools: list[str]) -> dict:
    """The mean scores of `score_pdfbench.py` beside what happened to the 200 documents and how long they took."""
    scores = saved(results, "pdfbench-scores.json")
    summary = {}
    for tool in tools:
        data = load(results, "pdfbench", tool)
        if not data or tool not in scores:
            continue
        replies = list(data["results"].values())
        outcomes: dict[str, int] = {}
        for reply in replies:
            outcomes[reply["outcome"]] = outcomes.get(reply["outcome"], 0) + 1
        summary[tool] = {"version": data.get("version"), "means": scores[tool]["means"], "outcomes": outcomes,
                         "total_seconds": round(sum(reply.get("seconds", 0.0) for reply in replies), 1),
                         "peak_rss_mb": round(max((reply.get("peak_rss_mb", 0.0) for reply in replies), default=0.0))}
    return summary


def main() -> None:
    results = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "results")
    tools = [tool for tool in TOOLS if (results / "cases" / f"{tool}.json").exists()]
    summary = {"tools": {tool: load(results, "cases", tool).get("version") for tool in tools},
               "coverage": coverage(results, tools), "probes": probes(results, tools),
               "ordinary": corpus(results, "ordinary", tools), "hostile": corpus(results, "hostile", tools),
               "pdfbench": pdfbench(results, tools), "recall": saved(results, "recall.json"), "install": saved(results, "install.json"),
               "caches": saved(results, "caches.json")}
    (results / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))

    print("== coverage ==".ljust(44), "  ".join(f"{tool[:6]:6}" for tool in tools))
    for case in COVERAGE:
        print(f"{case.label[:42]:44}", "      ".join(MARK[summary["coverage"][case.file][tool]["status"]] for tool in tools))
    print("passes".ljust(44), "  ".join(f"{sum(row[tool]['status'] == 'pass' for row in summary['coverage'].values()):6}" for tool in tools))
    print("\n== probes ==".ljust(45), "  ".join(f"{tool[:6]:6}" for tool in tools))
    for probe in PROBES:
        row = summary["probes"][probe.id]
        print(f"{probe.id[:42]:44}", "      ".join("·" if row[tool] is None else ("✅" if row[tool] else "❌") for tool in tools))
    print("passes".ljust(44), "  ".join(f"{sum(bool(row[tool]) for row in summary['probes'].values()):6}" for tool in tools), f"of {len(PROBES)}")
    for suite in ("ordinary", "hostile"):
        for tool, data in summary[suite].items():
            totals = {key: sum(bucket[key] for bucket in data["formats"].values()) for key in ("files", "ok", "empty", "error", "timeout", "crash", "memory")}
            print(f"{suite:9} {tool:13} {totals}  {sum(b['total_seconds'] for b in data['formats'].values()):8.1f}s")
    for tool, data in summary["pdfbench"].items():
        print(f"pdfbench  {tool:13} {({metric: value['mean'] for metric, value in data['means'].items()})}  {data['outcomes']}  {data['total_seconds']:8.1f}s")
    print("install  ", summary["install"])


if __name__ == "__main__":
    main()
