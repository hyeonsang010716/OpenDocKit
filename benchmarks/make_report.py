"""Write benchmarks/README.md from results/summary.json, so every number in the report comes from the saved results.

    python make_report.py

The prose lives here; the figures are filled in. If a result changes, rerun this and read the diff.
"""

import json
from pathlib import Path

from probes import COVERAGE, PROBES


ROOT = Path(__file__).parent
TOOLS = ["opendockit", "markitdown", "kreuzberg", "unstructured", "docling"]
NAMES = {"opendockit": "OpenDocKit", "markitdown": "MarkItDown", "kreuzberg": "Kreuzberg", "unstructured": "Unstructured",
         "docling": "Docling"}
LINKS = {"opendockit": "https://github.com/hyeonsang010716/OpenDocKit", "markitdown": "https://github.com/microsoft/markitdown",
         "kreuzberg": "https://kreuzberg.dev", "unstructured": "https://github.com/Unstructured-IO/unstructured",
         "docling": "https://github.com/docling-project/docling"}
INSTALL = {"opendockit": "`opendockit[heic]`", "markitdown": "`markitdown[all]`", "kreuzberg": "`kreuzberg[all]`",
           "unstructured": "`unstructured[all-docs]`", "docling": "`docling` + `docling-slim[format-opendocument,format-office,format-email]`"}
MARK = {"pass": "✅", "partial": "🟡", "fail": "❌", "missing": "·"}
AREAS = [("Word", ("docx-",)), ("PowerPoint", ("pptx-",)), ("Excel", ("xlsx-",)), ("PDF", ("pdf-",)), ("HTML", ("html-",)),
         ("Email", ("eml-", "mbox-")), ("Korean documents", ("hwp-", "hwpx-", "cp949-")), ("Mislabeled file", ("mislabeled-",))]
FORMATS = ["DOCX", "PPTX", "XLSX", "XLS", "PDF", "HWP", "HWPX", "DOC", "PPT"]


def table(header: list[str], rows: list[list[str]], align: str = "") -> str:
    marks = [":---" if index == 0 or align == "left" else "---:" if align == "right" else ":---:" for index in range(len(header))]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(marks) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def bold_best(values: dict[str, float], text: dict[str, str], *, lowest: bool = False) -> dict[str, str]:
    best = (min if lowest else max)(values.values())
    return {tool: f"**{text[tool]}**" if values[tool] == best else text[tool] for tool in values}


def main() -> None:
    summary = json.loads((ROOT / "results" / "summary.json").read_text())
    environment = json.loads((ROOT / "results" / "environment.json").read_text())
    vision = json.loads((ROOT / "results" / "cases" / "opendockit-vision.json").read_text()) if (
        ROOT / "results" / "cases" / "opendockit-vision.json").exists() else None
    tools = [tool for tool in TOOLS if tool in summary["tools"]]
    head = [NAMES[tool] for tool in tools]

    # ---- numbers ---------------------------------------------------------------------------------------------------
    coverage_pass = {tool: sum(row[tool]["status"] == "pass" for row in summary["coverage"].values()) for tool in tools}
    probe_pass = {tool: sum(bool(row[tool]) for row in summary["probes"].values()) for tool in tools}
    ordinary = summary["ordinary"]
    files = sum(bucket["files"] for bucket in ordinary[tools[0]]["formats"].values())

    def total(tool: str, key: str, suite: str = "ordinary") -> float:
        return sum(bucket[key] for bucket in summary[suite][tool]["formats"].values())

    seconds = {tool: total(tool, "total_seconds") for tool in tools}
    hostile_files = int(total(tools[0], "files", "hostile"))

    from probes import contains
    from report import output  # the vision column is scored with the same rule as every other cell
    vision_status = {}
    for case in COVERAGE:
        if vision and case.file in vision["results"]:
            text = output(ROOT / "results", "cases", "opendockit-vision", case.file)
            found = sum(contains(text, needle) for needle in case.expect)
            vision_status[case.file] = "pass" if found == len(case.expect) else "partial" if found else "fail"
    vision_total = sum((vision_status.get(case.file) or summary["coverage"][case.file]["opendockit"]["status"]) == "pass" for case in COVERAGE)

    # ---- tables ----------------------------------------------------------------------------------------------------
    versions = table(["Tool", "Version", "Installed as", "License"], [
        [f"[{NAMES[tool]}]({LINKS[tool]})", summary["tools"][tool], INSTALL[tool],
         {"opendockit": "MIT", "markitdown": "MIT", "kreuzberg": "MIT", "unstructured": "Apache-2.0", "docling": "MIT"}[tool]] for tool in tools], "left")

    coverage_rows, group = [], None
    for case in COVERAGE:
        if case.group != group:
            group = case.group
            coverage_rows.append([f"**{group}**"] + [""] * (len(tools) + 1))
        cells = [MARK[summary["coverage"][case.file][tool]["status"]] for tool in tools]
        with_vision = MARK[vision_status[case.file]] if case.file in vision_status else ""
        coverage_rows.append([case.label] + cells[:1] + [with_vision] + cells[1:])
    coverage_rows.append(["**Formats read**"] + [f"**{coverage_pass[tools[0]]}**", f"**{vision_total}**"] + [f"**{coverage_pass[tool]}**" for tool in tools[1:]])
    coverage_table = table(["Format", head[0], "… + vision model", *head[1:]], coverage_rows)

    area_rows = []
    for name, prefixes in AREAS:
        chosen = [probe for probe in PROBES if probe.id.startswith(prefixes)]
        values = {tool: sum(bool(summary["probes"][probe.id][tool]) for probe in chosen) for tool in tools}
        shown = bold_best(values, {tool: str(values[tool]) for tool in tools})
        area_rows.append([f"{name} ({len(chosen)})"] + [shown[tool] for tool in tools])
    shown = bold_best(probe_pass, {tool: str(probe_pass[tool]) for tool in tools})
    area_rows.append([f"**All ({len(PROBES)})**"] + [shown[tool] for tool in tools])
    area_table = table(["Checks", *head], area_rows)

    probe_table = table(["Check", "Document", *head], [
        [probe.question, f"`{probe.file}`"] + ["✅" if summary["probes"][probe.id][tool] else "❌" for tool in tools] for probe in PROBES])

    corpus_rows = []
    for name in FORMATS:
        count = ordinary[tools[0]]["formats"][name]["files"]
        values = {tool: ordinary[tool]["formats"][name]["ok"] for tool in tools}
        shown = bold_best(values, {tool: str(values[tool]) for tool in tools}) if max(values.values()) else dict.fromkeys(tools, "0")
        corpus_rows.append([f"{name} ({count})"] + [shown[tool] for tool in tools])
    values = {tool: int(total(tool, "ok")) for tool in tools}
    corpus_rows.append([f"**All ({files})**"] + [f"**{values[tool]}**" for tool in tools])
    corpus_table = table(["Returned text", *head], corpus_rows)

    problem_rows = [[label] + [str(int(total(tool, key))) for tool in tools]
                    for label, key in (("Error (refused or failed)", "error"), ("No text returned", "empty"), ("Timed out (120 s)", "timeout"),
                                       ("Crashed the process", "crash"))]
    problem_table = table(["Of the same files", *head], problem_rows)

    time_rows = []
    for name in FORMATS:
        values = {tool: ordinary[tool]["formats"][name]["total_seconds"] for tool in tools}
        time_rows.append([f"{name}"] + [f"{values[tool]:,.1f}" for tool in tools])
    shown = bold_best(seconds, {tool: f"{seconds[tool]:,.0f}" for tool in tools}, lowest=True)
    time_rows.append(["**All, seconds**"] + [shown[tool] for tool in tools])
    time_rows.append(["Median PDF, seconds"] + [f"{ordinary[tool]['formats']['PDF']['median_seconds']:.2f}" for tool in tools])
    time_table = table(["Time to convert, seconds", *head], time_rows, "right")

    hostile_rows = [[label] + [str(int(total(tool, key, "hostile"))) for tool in tools]
                    for label, key in (("Returned text", "ok"), ("Refused with an error", "error"), ("No text returned", "empty"),
                                       ("Timed out (60 s)", "timeout"), ("Crashed the process", "crash"), ("Stopped above 6 GB of memory", "memory"))]
    hostile_rows.append(["Slowest single file, seconds"] + [f"{max(b['max_seconds'] for b in summary['hostile'][tool]['formats'].values()):.1f}" for tool in tools])
    hostile_rows.append(["Peak memory on a single file, MB"] + [f"{max(b['peak_rss_mb'] for b in summary['hostile'][tool]['formats'].values()):,}" for tool in tools])
    hostile_table = table([f"{hostile_files} hostile files", *head], hostile_rows)

    footprint_table = table(["", *head], [
        ["Installed size"] + [{"opendockit": "0.1 GB", "markitdown": "0.3 GB", "kreuzberg": "5.7 GB", "unstructured": "6.5 GB", "docling": "5.9 GB"}[tool] for tool in tools],
        ["Needs system packages"] + [{"opendockit": "LibreOffice, only for `.doc` / `.ppt`", "markitdown": "no", "kreuzberg": "Tesseract for OCR", "docling": "no",
                                      "unstructured": "libmagic, Poppler, Tesseract, LibreOffice, pandoc"}[tool] for tool in tools],
        ["Runs ML models locally"] + [{"opendockit": "no", "markitdown": "no", "kreuzberg": "optional", "unstructured": "yes", "docling": "yes"}[tool] for tool in tools],
    ])

    order = sorted(tools, key=lambda tool: -probe_pass[tool])
    speed_order = sorted(tools, key=lambda tool: seconds[tool])
    widest = max(coverage_pass.values())
    widest_tools = [NAMES[tool] for tool in tools if coverage_pass[tool] == widest]
    offline_gaps = [case.label for case in COVERAGE if summary["coverage"][case.file]["opendockit"]["status"] != "pass"]
    if coverage_pass["opendockit"] == widest:
        formats_note = (f"No tool reads more ({', '.join(widest_tools)})." if len(widest_tools) > 1 else "That is the most of the five.")
    else:
        formats_note = f"**It is not the widest:** {' and '.join(widest_tools)} read{'s' if len(widest_tools) == 1 else ''} {widest}."
    formats_note += (" Offline, OpenDocKit does not read " + ", ".join(offline_gaps) + " — it has no OCR of its own." if offline_gaps else "")
    failed_questions = list(dict.fromkeys(probe.question for probe in PROBES if not summary["probes"][probe.id]["opendockit"]))
    if failed_questions:
        failed = "OpenDocKit fails these:\n\n" + "\n".join(f"- {question}" for question in failed_questions)
    else:
        failed = f"OpenDocKit passes all {len(PROBES)}. Read that with the note on author bias at the end of this page in mind."
    legacy = {name: ordinary["opendockit"]["formats"][name] for name in ("DOC", "PPT")}
    legacy_ok, legacy_files = sum(b["ok"] for b in legacy.values()), sum(b["files"] for b in legacy.values())
    legacy_seconds = sum(b["total_seconds"] for b in legacy.values())
    hostile_us = summary["hostile"]["opendockit"]["formats"]
    us_hostile_slowest = max(b["max_seconds"] for b in hostile_us.values())
    us_hostile_memory = max(b["peak_rss_mb"] for b in hostile_us.values())
    over_cap = [NAMES[tool] for tool in tools if total(tool, "memory", "hostile")]
    bad_hostile = [NAMES[tool] for tool in tools if total(tool, "crash", "hostile") or total(tool, "timeout", "hostile")]
    text = TEMPLATE.format(
        cpu=environment["cpu"], cores=environment["cores"], memory=round(environment["memory_mb"] / 1024), platform=environment["platform"],
        commit=environment["commit"][:7], versions=versions, coverage_table=coverage_table, area_table=area_table, probe_table=probe_table,
        corpus_table=corpus_table, problem_table=problem_table, time_table=time_table, hostile_table=hostile_table,
        footprint_table=footprint_table, probes=len(PROBES), formats=len(COVERAGE), files=files, hostile_files=hostile_files,
        us_probes=probe_pass["opendockit"], second=NAMES[order[1]], second_probes=probe_pass[order[1]],
        us_coverage=coverage_pass["opendockit"], us_vision=vision_total,
        best_coverage=NAMES[max(tools, key=lambda tool: coverage_pass[tool])], best_coverage_count=max(coverage_pass.values()),
        fastest=NAMES[speed_order[0]], fastest_seconds=round(seconds[speed_order[0]]), us_seconds=round(seconds["opendockit"]),
        us_rank=speed_order.index("opendockit") + 1, docling_seconds=round(seconds["docling"]), unstructured_seconds=round(seconds["unstructured"]),
        markitdown_seconds=round(seconds["markitdown"]), hwp_ok=ordinary["opendockit"]["formats"]["HWP"]["ok"] + ordinary["opendockit"]["formats"]["HWPX"]["ok"],
        hwp_files=ordinary["opendockit"]["formats"]["HWP"]["files"] + ordinary["opendockit"]["formats"]["HWPX"]["files"],
        us_errors=int(total("opendockit", "error")), formats_note=formats_note,
        legacy_ok=legacy_ok, legacy_files=legacy_files, legacy_seconds=round(legacy_seconds),
        rest_seconds=round(seconds["opendockit"] - legacy_seconds), us_hostile_slowest=f"{us_hostile_slowest:.0f}",
        us_hostile_memory=f"{us_hostile_memory / 1024:.1f}",
        hostile_verdict=("no tool crashed or hung" if not bad_hostile else f"{', '.join(bad_hostile)} crashed or timed out on at least one file"),
        over_cap=" and ".join(over_cap) if over_cap else "no tool", opendockit_build=(
            "built from the commit above" if environment.get("opendockit") == "checkout"
            else "installed from PyPI"),
    )
    (ROOT / "README.md").write_text(text.replace("@@FAILED@@", failed), encoding="utf-8")
    print("README.md written;", {tool: (coverage_pass[tool], probe_pass[tool], round(seconds[tool])) for tool in tools}, "vision", vision_total)


TEMPLATE = """# Benchmark: OpenDocKit and four other document converters

OpenDocKit, [MarkItDown](https://github.com/microsoft/markitdown), [Kreuzberg](https://kreuzberg.dev),
[Unstructured](https://github.com/Unstructured-IO/unstructured) and [Docling](https://github.com/docling-project/docling)
were given the same files on the same machine. This page reports what came out — including the places where
OpenDocKit is not the best choice.

> **Read this first.** The benchmark was written by OpenDocKit's maintainers, so treat it the way you would treat any
> vendor's benchmark: the documents and checks are ours, even though the scoring is done by a program and every
> output is saved. Everything needed to rerun it, on a free GitHub runner, is in this folder. If a check is unfair to a
> tool, or a tool was configured badly, please open an issue — we will fix the benchmark.

![Structure checks passed](charts/structure-checks.svg)

## Summary

- **Structure.** On {probes} yes/no checks of the kind "is the table still a table", OpenDocKit passes {us_probes}; the next
  tool, {second}, passes {second_probes}. This is where OpenDocKit is strongest: merged cells, several tables on one sheet,
  Excel's display formats, two-column PDFs, running headers, equations, meeting invitations.
- **Formats.** OpenDocKit reads {us_coverage} of the {formats} formats tested offline ({us_vision} with a vision model for
  images and scans). {formats_note}
- **Korean documents.** OpenDocKit is the only one of the five that reads HWP and HWPX at all, and it reads
  CP949-encoded text and CSV, which several of the others do not.
- **Speed.** {fastest} is in a class of its own ({fastest_seconds} s for {files} files). OpenDocKit is #{us_rank} of 5 at
  {us_seconds} s, of which {legacy_seconds} s is LibreOffice converting the {legacy_files} legacy `.doc` and `.ppt` files;
  MarkItDown takes {markitdown_seconds} s, Unstructured {unstructured_seconds} s and Docling {docling_seconds} s on a
  CPU-only machine.
- **Hostile input.** On {hostile_files} fuzzer findings and attack files from other projects, {hostile_verdict}.
  One file made {over_cap} pass 6 GB of memory; OpenDocKit's largest was about {us_hostile_memory} GB.

## What was compared

{versions}

Each tool was installed in its own virtual environment with the extras its documentation lists for full document
support (see [`setup_tools.sh`](setup_tools.sh)), and called through its default entry point with default options
(see [`adapters.py`](adapters.py)). No tool was given an API key or a remote model. System packages that Unstructured
and Kreuzberg need for OCR and legacy formats (Tesseract with English and Korean data, LibreOffice, pandoc, Poppler,
libmagic) were installed for them.

Machine: GitHub-hosted runner, {cpu}, {cores} cores, {memory} GB RAM, no GPU, {platform}. Run from commit `{commit}`;
OpenDocKit was {opendockit_build}.

## 1. Which formats does each tool read?

One representative file per format. ✅ means the output contains every piece of text we looked for; 🟡 some of it;
❌ none, an error, or an empty result. The documents are in [`cases/`](cases/) and the expected strings in
[`probes.py`](probes.py).

{coverage_table}

Notes:

- The "… + vision model" column is OpenDocKit with `OpenAIImageDescriber` (`gpt-5.6-terra`), run separately
  ([`vision_cases.py`](vision_cases.py)). It needs an API key and costs money; the other four columns to its right do
  their OCR locally and for free. Compare like with like: offline, OpenDocKit does not read images or scans.
- Not tested, and supported by some of the other tools but not by OpenDocKit: audio and video, YouTube and web URLs,
  ZIP archives, Outlook `.msg`, Apple Pages/Numbers/Keynote, LaTeX, XBRL.
- A ✅ here only says the text came through. How well the structure survived is the next section.

## 2. How much of the structure survives?

{probes} yes/no questions about documents whose content we know exactly, because we generated them
([`make_cases.py`](make_cases.py)). Each question is a few lines of code in [`probes.py`](probes.py); matching ignores
case, whitespace, Markdown escapes and emphasis markers, and accepts both Markdown and HTML tables.

{area_table}

@@FAILED@@

These are the same {probes} checks, unchanged, that were published with the report for OpenDocKit 0.1.0, which passed 43
of them: it had no reader for the OpenDocument, RTF, EPUB and legacy Office files and flattened links and emphasis to
plain text. 0.2.0 was written afterwards with those checks known, so its score says the gaps are closed, not that
OpenDocKit would do as well on checks somebody else wrote.

<details>
<summary>Every check, every tool</summary>

{probe_table}

</details>

Some checks encode an opinion about what is right for LLM input rather than a fact about the document: dropping
navigation and hidden elements, leaving hidden sheets out, removing running headers and page numbers, and showing a
cell the way Excel displays it (`46.4%`) instead of its stored value (`0.464459`). A tool that deliberately does the
opposite fails those checks by design, not by mistake.

## 3. Real files from other projects

{files} documents taken from the test suites of Apache POI, pdfplumber, hwplib and hwpxlib
([`corpus.py`](corpus.py) picks up to 100 per format at even intervals). Nobody here wrote or chose these files for a
particular tool. We do not know their correct output, so this section only counts what happened.

{corpus_table}

{problem_table}

- OpenDocKit reads legacy `.doc` and `.ppt` by handing them to LibreOffice, which this machine has because Unstructured
  and Kreuzberg need it too: {legacy_ok} of {legacy_files} returned text. Without LibreOffice it refuses those two formats.
- "No text returned" is often correct: many test files are blank pages, pictures or shapes. Of the {hwp_files} Hangul
  files OpenDocKit returned text for {hwp_ok}; we checked the rest against the preview text stored inside each file, and
  they contain none.
- Kreuzberg accepts `.hwp` files but returned no text for any of them.

{time_table}

Three workers per tool ran in parallel on the 4-core machine; times are the sum over files of the time inside the
conversion call. Docling and Unstructured run layout and OCR models, which is why they are slow on a machine without a
GPU and why they can read scans that OpenDocKit, offline, cannot.

## 4. Hostile input

The {hostile_files} files those same projects keep as fuzzer findings and proof-of-concept attacks (ClusterFuzz test
cases, XML bombs, deeply nested tables). Each file ran in a fresh process with a 60-second limit and a 6 GB memory cap.

{hostile_table}

The one clear difference is `poc-shared-strings.xlsx`, an Excel file whose shared-string table expands enormously: it
pushed {over_cap} past the 6 GB cap. OpenDocKit bounds decompressed size; its slowest file took {us_hostile_slowest} s and its largest used
{us_hostile_memory} GB.
This is a small sample; it shows the absence of obvious problems, not robustness in general.

## 5. Footprint

{footprint_table}

Sizes are of the environments as installed above, with every extra the comparison uses; a tool installed without its
extras is smaller.

## Which one should you use?

- **Korean office documents (HWP, HWPX, CP949), or Office files where tables and layout must survive** → OpenDocKit.
- **Scanned PDFs and images, offline** → Docling or Unstructured. They run OCR and layout models locally; OpenDocKit
  needs a vision-model API for those.
- **Sheer throughput** → Kreuzberg.
- **Audio, video, web URLs, archives, Outlook `.msg`** → MarkItDown or Unstructured; OpenDocKit does not read them.

## Run it yourself

On GitHub: fork the repository, open **Actions → Benchmark → Run workflow**. It takes about 40 minutes on a free
runner and uploads the raw results as an artifact.

Locally, on an x86-64 Linux machine (the ML-based tools crash under Docker on Apple silicon):

```bash
docker build -t opendockit-bench benchmarks
sh benchmarks/fetch_corpus.sh /tmp/corpus
docker run --rm -v "$PWD/benchmarks":/bench -v /tmp/corpus:/corpus:ro opendockit-bench sh run_all.sh
python benchmarks/make_report.py && python benchmarks/charts.py
```

| File | What it does |
| --- | --- |
| [`setup_tools.sh`](setup_tools.sh) | Installs the five tools, pinned, each in its own environment |
| [`adapters.py`](adapters.py) | The one function per tool that the benchmark calls |
| [`make_cases.py`](make_cases.py), [`make_legacy_cases.sh`](make_legacy_cases.sh) | Build the documents in [`cases/`](cases/) |
| [`probes.py`](probes.py) | The expected text per format and the structure checks |
| [`corpus.py`](corpus.py), [`fetch_corpus.sh`](fetch_corpus.sh) | Select and download the public test files, at fixed revisions |
| [`harness.py`](harness.py), [`worker.py`](worker.py) | Run each tool in worker processes with time and memory limits |
| [`report.py`](report.py), [`make_report.py`](make_report.py), [`charts.py`](charts.py) | Score the saved outputs, write this page and the chart |
| [`results/`](results/) | Raw results: per-file outcomes, timings, and each tool's output for every case |

## Limits of this benchmark

- **Author bias.** We chose the documents and wrote the checks, and a maintainer of another tool would have picked
  different ones. The checks include six that OpenDocKit 0.1.0 failed; 0.2.0 was then developed with all of them in
  view, which no other tool here was.
- **Small and synthetic.** The scored documents are {formats} files we generated. They are realistic but not real,
  and several tools were not built for the Korean formats that make up part of the set.
- **Defaults only.** Each tool ran with default options. Docling and Unstructured in particular have settings
  (OCR engines, table modes, a GPU) that change both quality and speed.
- **One run, one machine.** Timings come from a single run on a shared 4-core runner; treat differences under 2× as noise.
- **No accuracy score for real files.** For the public corpus we count outcomes, not correctness.
- **A snapshot.** The other four tools are pinned to what was current on 2026-11-12. All five projects move quickly.
"""


if __name__ == "__main__":
    main()
