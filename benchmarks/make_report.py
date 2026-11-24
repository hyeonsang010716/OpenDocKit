"""Write benchmarks/README.md from results/summary.json, so every number in the report comes from the saved results.

    python make_report.py

The prose lives here; the figures are filled in. If a result changes, rerun this and read the diff.
"""

import json
from pathlib import Path

from probes import COVERAGE, PROBES


ROOT = Path(__file__).parent
TOOLS = ["opendockit", "markitdown", "kreuzberg", "unstructured", "docling", "kordoc"]
NAMES = {"opendockit": "OpenDocKit", "markitdown": "MarkItDown", "kreuzberg": "Kreuzberg", "unstructured": "Unstructured",
         "docling": "Docling", "kordoc": "kordoc"}
LINKS = {"opendockit": "https://github.com/hyeonsang010716/OpenDocKit", "markitdown": "https://github.com/microsoft/markitdown",
         "kreuzberg": "https://kreuzberg.dev", "unstructured": "https://github.com/Unstructured-IO/unstructured",
         "docling": "https://github.com/docling-project/docling", "kordoc": "https://github.com/chrisryugj/kordoc"}
INSTALL = {"opendockit": "`opendockit[heic]`", "markitdown": "`markitdown[all]`", "kreuzberg": "`kreuzberg[all]`",
           "unstructured": "`unstructured[all-docs]`", "docling": "`docling` + `docling-slim[format-opendocument,format-office,format-email]`",
           "kordoc": "`npm install kordoc`"}
LICENSES = {"opendockit": "MIT", "markitdown": "MIT", "kreuzberg": "MIT", "unstructured": "Apache-2.0", "docling": "MIT", "kordoc": "MIT"}
MODELS = {"opendockit": "no", "markitdown": "no", "kreuzberg": "OCR (Tesseract)", "unstructured": "layout and OCR", "docling": "layout, tables and OCR",
          "kordoc": "OCR (ONNX)"}
SYSTEM = {"opendockit": "LibreOffice, only for `.doc` / `.ppt`", "markitdown": "none", "kreuzberg": "Tesseract",
          "unstructured": "libmagic, Poppler, Tesseract, LibreOffice, pandoc", "docling": "LibreOffice for legacy Office files", "kordoc": "none"}
RUNTIME = {"opendockit": "Python", "markitdown": "Python", "kreuzberg": "Rust core, Python package", "unstructured": "Python", "docling": "Python",
           "kordoc": "Node.js"}
# Docling runs its layout model on every page, and kordoc's default switches OCR on where a page needs it (its own
# figures: 0.965 by default, 0.939 with OCR off). Unstructured's default takes its fast, model-free path for a PDF with
# a text layer, and Kreuzberg calls Tesseract only for a page without text.
PDF_MODELS = {"docling", "kordoc"}
WORDS = {4: "four", 5: "five", 6: "six", 7: "seven"}
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
         LICENSES[tool]] for tool in tools], "left")

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

    install, caches = summary["install"], summary.get("caches", {})
    footprint_table = table(["", *head], [
        ["Installed size, MB"] + [bold_best({t: install[t]["megabytes"] for t in tools}, {t: f"{install[t]['megabytes']:,}" for t in tools}, lowest=True)[tool]
                                  for tool in tools],
        ["Packages installed"] + [str(install[tool]["packages"]) for tool in tools],
        ["Runs models on your machine"] + [MODELS[tool] for tool in tools],
        ["System packages it uses"] + [SYSTEM[tool] for tool in tools],
        ["Runtime"] + [RUNTIME[tool] for tool in tools],
    ])
    cache_names = {".cache/docling": "Docling", ".cache/huggingface": "Hugging Face", ".cache/kordoc": "kordoc", ".cache/kreuzberg": "Kreuzberg"}
    cache_note = ", ".join(f"{cache_names[key]} {caches[key]:,} MB" for key in cache_names if caches.get(key, 0) > 1)

    # ---- the PDF benchmark ---------------------------------------------------------------------------------------------
    pdf = summary["pdfbench"]
    pdf_order = sorted(tools, key=lambda tool: -pdf[tool]["means"]["overall"]["mean"])
    pdf_rows = []
    for label, key in (("**Overall**", "overall"), ("Reading order (NID)", "nid"), ("Tables (TEDS)", "teds"), ("Headings (MHS)", "mhs"),
                       ("Words kept", "words")):
        values = {tool: pdf[tool]["means"][key]["mean"] for tool in tools}
        shown = bold_best(values, {tool: f"{values[tool]:.3f}" for tool in tools})
        pdf_rows.append([label] + [shown[tool] for tool in pdf_order])
    values = {tool: pdf[tool]["total_seconds"] for tool in tools}
    shown = bold_best(values, {tool: f"{values[tool]:,.0f}" for tool in tools}, lowest=True)
    pdf_rows.append(["Time for the 200 files, seconds"] + [shown[tool] for tool in pdf_order])
    values = {tool: pdf[tool]["peak_rss_mb"] for tool in tools}
    shown = bold_best(values, {tool: f"{values[tool]:,}" for tool in tools}, lowest=True)
    pdf_rows.append(["Peak memory, MB"] + [shown[tool] for tool in pdf_order])
    pdf_rows.append(["Ran a model on these files"] + ["yes" if tool in PDF_MODELS else "no" for tool in pdf_order])
    pdf_table = table(["200 PDFs", *[NAMES[tool] for tool in pdf_order]], pdf_rows)
    pdf_documents = pdf["opendockit"]["means"]
    pdf_failed = [NAMES[tool] for tool in tools if set(pdf[tool]["outcomes"]) - {"ok"}]

    # ---- recall ----------------------------------------------------------------------------------------------------------
    recall = summary["recall"]
    recall_formats = ["DOCX", "PPTX", "XLSX", "HWPX", "HWP"]
    recall_rows = []
    for name in recall_formats:
        row = [f"{name} ({recall['opendockit'][name]['documents']} files)"]
        for tool in tools:
            bucket = recall[tool][name]
            row.append(f"{bucket['missing']:,} of {bucket['words']:,}" if bucket["read"] else "reads none")
        recall_rows.append(row)
    recall_table = table(["Words missing", *head], recall_rows)
    where_rows = []
    for name in recall_formats:
        for tool in tools:
            bucket = recall[tool][name]
            if bucket["missing"]:
                where_rows.append([name, NAMES[tool], str(bucket["read"]), ", ".join(f"`{key}` {count:,}" for key, count in bucket["where"].items())])
    where_table = table(["Format", "Tool", "Files it returned text for", "Where the missing words sit in the source"], where_rows, "left")

    order = sorted(tools, key=lambda tool: -probe_pass[tool])
    speed_order = sorted(tools, key=lambda tool: seconds[tool])
    widest = max(coverage_pass.values())
    widest_tools = [NAMES[tool] for tool in tools if coverage_pass[tool] == widest]
    offline_gaps = [case.label for case in COVERAGE if summary["coverage"][case.file]["opendockit"]["status"] != "pass"]
    if coverage_pass["opendockit"] == widest:
        formats_note = (f"No tool reads more ({', '.join(widest_tools)})." if len(widest_tools) > 1 else f"That is the most of the {WORDS[len(tools)]}.")
    else:
        formats_note = f"**It is not the widest:** {' and '.join(widest_tools)} read{'s' if len(widest_tools) == 1 else ''} {widest}."
    formats_note += (" Without a model it does not read " + ", ".join(offline_gaps) + ": it has no OCR of its own." if offline_gaps else "")
    hangul = [NAMES[tool] for tool in tools if all(summary["coverage"][file][tool]["status"] == "pass" for file in ("table.hwp", "score-table.hwpx"))]
    failed_questions = list(dict.fromkeys(probe.question for probe in PROBES if not summary["probes"][probe.id]["opendockit"]))
    if failed_questions:
        failed = "OpenDocKit fails these:\n\n" + "\n".join(f"- {question}" for question in failed_questions)
    else:
        failed = f"OpenDocKit passes all {len(PROBES)}. Read that with the note on author bias at the end of this page in mind."
    legacy = {name: ordinary["opendockit"]["formats"][name] for name in ("DOC", "PPT")}
    legacy_ok, legacy_files = sum(b["ok"] for b in legacy.values()), sum(b["files"] for b in legacy.values())
    legacy_seconds = sum(b["total_seconds"] for b in legacy.values())
    hostile_us = summary["hostile"]["opendockit"]["formats"]
    over_cap = [NAMES[tool] for tool in tools if total(tool, "memory", "hostile")]
    timed_out = [NAMES[tool] for tool in tools if total(tool, "timeout", "hostile")]
    crashed = [NAMES[tool] for tool in tools if total(tool, "crash", "hostile")]
    clean = [NAMES[tool] for tool in tools if not (total(tool, "memory", "hostile") or total(tool, "timeout", "hostile") or total(tool, "crash", "hostile"))]
    hostile_parts = []
    if over_cap:
        hostile_parts.append(f"{join(over_cap)} went past the 6 GB cap on one file")
    if timed_out:
        hostile_parts.append(f"{join(timed_out)} ran into the 60-second limit on one")
    if crashed:
        hostile_parts.append(f"{join(crashed)} crashed")
    us_pdf, docling_pdf, kordoc_pdf = pdf["opendockit"], pdf["docling"], pdf["kordoc"]
    text = TEMPLATE.format(
        cpu=environment["cpu"], cores=environment["cores"], memory=round(environment["memory_mb"] / 1024), platform=environment["platform"],
        commit=environment["commit"][:7], versions=versions, coverage_table=coverage_table, area_table=area_table, probe_table=probe_table,
        corpus_table=corpus_table, problem_table=problem_table, time_table=time_table, hostile_table=hostile_table,
        footprint_table=footprint_table, pdf_table=pdf_table, recall_table=recall_table, where_table=where_table,
        probes=len(PROBES), formats=len(COVERAGE), files=files, hostile_files=hostile_files,
        us_probes=probe_pass["opendockit"], second=NAMES[order[1]], second_probes=probe_pass[order[1]],
        us_coverage=coverage_pass["opendockit"], us_vision=vision_total, vision_version=vision["version"] if vision else "",
        fastest=NAMES[speed_order[0]], fastest_seconds=round(seconds[speed_order[0]]), us_seconds=round(seconds["opendockit"]),
        us_rank=speed_order.index("opendockit") + 1, tool_count=len(tools), others=WORDS[len(tools) - 1], all_tools=WORDS[len(tools)],
        docling_seconds=f"{seconds['docling']:,.0f}", unstructured_seconds=round(seconds["unstructured"]),
        markitdown_seconds=round(seconds["markitdown"]), kordoc_seconds=round(seconds["kordoc"]),
        hwp_ok=ordinary["opendockit"]["formats"]["HWP"]["ok"] + ordinary["opendockit"]["formats"]["HWPX"]["ok"],
        hwp_files=ordinary["opendockit"]["formats"]["HWP"]["files"] + ordinary["opendockit"]["formats"]["HWPX"]["files"],
        formats_note=formats_note, hangul_tools=join(hangul), legacy_ok=legacy_ok, legacy_files=legacy_files, legacy_seconds=round(legacy_seconds),
        us_hostile_slowest=f"{max(b['max_seconds'] for b in hostile_us.values()):.1f}",
        us_hostile_memory=max(b["peak_rss_mb"] for b in hostile_us.values()),
        hostile_verdict="; ".join(hostile_parts) if hostile_parts else "no tool crashed, hung or ran out of memory",
        hostile_clean=join(clean), over_cap=join(over_cap) if over_cap else "no tool",
        pdf_first=NAMES[pdf_order[0]], pdf_first_score=f"{pdf[pdf_order[0]]['means']['overall']['mean']:.3f}",
        pdf_us=f"{us_pdf['means']['overall']['mean']:.3f}", pdf_us_rank=pdf_order.index("opendockit") + 1,
        pdf_docling=f"{docling_pdf['means']['overall']['mean']:.3f}", pdf_us_seconds=round(us_pdf["total_seconds"]),
        pdf_docling_seconds=f"{docling_pdf['total_seconds']:,.0f}", pdf_kordoc_seconds=round(kordoc_pdf["total_seconds"]),
        pdf_docling_times=round(docling_pdf["total_seconds"] / us_pdf["total_seconds"]),
        pdf_kordoc_times=round(kordoc_pdf["total_seconds"] / us_pdf["total_seconds"]),
        pdf_us_memory=us_pdf["peak_rss_mb"], pdf_tables=pdf_documents["teds"]["documents"], pdf_headings=pdf_documents["mhs"]["documents"],
        pdf_us_teds=f"{us_pdf['means']['teds']['mean']:.3f}", pdf_docling_teds=f"{docling_pdf['means']['teds']['mean']:.3f}",
        pdf_kordoc_teds=f"{kordoc_pdf['means']['teds']['mean']:.3f}",
        pdf_failures=("Every tool returned text for all 200." if not pdf_failed else f"{join(pdf_failed)} did not return text for every file."),
        us_install=install["opendockit"]["megabytes"], us_packages=install["opendockit"]["packages"], cache_note=cache_note,
        opendockit_build=("built from the commit above (" + summary["tools"]["opendockit"] + " plus what has been merged since)"
                          if environment.get("opendockit") == "checkout" else "installed from PyPI"),
    )
    (ROOT / "README.md").write_text(text.replace("@@FAILED@@", failed), encoding="utf-8")
    print("README.md written;", {tool: (coverage_pass[tool], probe_pass[tool], round(seconds[tool])) for tool in tools}, "vision", vision_total)


def join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "none"


TEMPLATE = """# Benchmark: OpenDocKit and {others} other document converters

OpenDocKit, [MarkItDown](https://github.com/microsoft/markitdown), [Kreuzberg](https://kreuzberg.dev),
[Unstructured](https://github.com/Unstructured-IO/unstructured), [Docling](https://github.com/docling-project/docling)
and [kordoc](https://github.com/chrisryugj/kordoc) were given the same files on the same machine. This page reports
what came out, including the places where OpenDocKit is not the best choice.

> **Read this first.** OpenDocKit's maintainers wrote this benchmark, so treat it as you would any vendor's. Sections 1
> and 2 use documents and checks we made. Section 3 uses a PDF benchmark we did not make, and sections 4 and 5 use
> other projects' test files. A program does the scoring and the results are saved in [`results/`](results/).
> Everything needed to rerun it on a free GitHub runner is in this folder. If a check is unfair to a tool, or a tool
> was set up badly, please open an issue and we will fix the benchmark.

## Summary

- **Formats.** OpenDocKit reads {us_coverage} of the {formats} formats tested ({us_vision} with a vision model for images
  and scans). {formats_note}
- **Structure.** Of {probes} yes/no checks such as "is the merged cell still attached to its row", OpenDocKit passes
  {us_probes} and the next tool, {second}, passes {second_probes}. The checks are ours; section 2 says how to read that.
- **PDF, on a benchmark we did not write.** On the 200 PDFs of
  [opendataloader-bench](https://github.com/opendataloader-project/opendataloader-bench), {pdf_first} scores highest
  ({pdf_first_score}), Docling {pdf_docling} and OpenDocKit {pdf_us}. kordoc and Docling run models: kordoc needed
  {pdf_kordoc_seconds} s and Docling {pdf_docling_seconds} s for the 200 files, OpenDocKit {pdf_us_seconds} s. Where
  OpenDocKit loses is tables ({pdf_us_teds} against {pdf_kordoc_teds} and {pdf_docling_teds}).
- **Korean documents.** {hangul_tools} read HWP and HWPX; the other four do not.
- **Speed.** {fastest} is in a class of its own ({fastest_seconds} s for {files} real files). OpenDocKit takes
  {us_seconds} s, of which {legacy_seconds} s is LibreOffice converting {legacy_files} legacy `.doc` and `.ppt` files;
  MarkItDown {markitdown_seconds} s, kordoc {kordoc_seconds} s, Unstructured {unstructured_seconds} s and Docling
  {docling_seconds} s on a machine without a GPU.
- **Hostile input.** On {hostile_files} fuzzer findings and attack files, {hostile_verdict}. {hostile_clean} got
  through all of them; OpenDocKit's largest process was {us_hostile_memory} MB.
- **Footprint.** OpenDocKit installs {us_install} MB in {us_packages} packages and downloads no model.

![Structure checks passed](charts/structure-checks.svg)

![Score on the 200 PDFs of opendataloader-bench](charts/pdf-benchmark.svg)

![Seconds to convert the 200 PDFs](charts/pdf-time.svg)

![Formats read without a model or a service](charts/formats.svg)

![Installed size](charts/install-size.svg)

## What was compared

{versions}

Each Python tool was installed in its own virtual environment with the extras its documentation lists for full
document support, and kordoc with `npm install` (see [`setup_tools.sh`](setup_tools.sh)). Each was called through its
default entry point with default options (see [`adapters.py`](adapters.py) and
[`worker_kordoc.mjs`](worker_kordoc.mjs)). No tool was given an API key or a remote model. The system packages that
Unstructured and Kreuzberg use for OCR and legacy formats (Tesseract with English and Korean data, LibreOffice,
pandoc, Poppler, libmagic) were installed for everyone.

Machine: GitHub-hosted runner, {cpu}, {cores} cores, {memory} GB RAM, no GPU, {platform}. Run from commit `{commit}`;
OpenDocKit was {opendockit_build}.

## 1. Which formats does each tool read?

One representative file per format. ✅ means the output contains every piece of text we looked for; 🟡 some of it;
❌ none, an error, or an empty result. The documents are in [`cases/`](cases/) and the expected strings in
[`probes.py`](probes.py).

{coverage_table}

Notes:

- The "… + vision model" column is OpenDocKit with `OpenAIImageDescriber`, run separately
  ([`vision_cases.py`](vision_cases.py)) and not repeated for this report: it was last run with OpenDocKit {vision_version}. It needs
  an API key and costs money, while the tools that read images here do their OCR on the machine for free. Compare
  like with like: without a model, OpenDocKit does not read images or scans.
- kordoc is a converter for Korean office documents and says so. It reads HWP, HWPX, PDF, the Office formats and
  images, and was not built for the rest of this list.
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
of them. Every release since was written with the checks known, so the score says those gaps are closed. It does not
say OpenDocKit would do as well on checks somebody else wrote; section 3 is the closest this page comes to that.

<details>
<summary>Every check, every tool</summary>

{probe_table}

</details>

Some checks encode an opinion about what is right for LLM input rather than a fact about the document: dropping
navigation and hidden elements, leaving hidden sheets out, removing running headers and page numbers, and showing a
cell the way Excel displays it (`46.4%`) instead of its stored value (`0.464459`). A tool that deliberately does the
opposite fails those checks by design, not by mistake.

## 3. A PDF benchmark we did not write

[opendataloader-bench](https://github.com/opendataloader-project/opendataloader-bench) is 200 PDFs, mostly English
reports and papers, each with a hand-checked Markdown reference. It scores reading order (NID), tables (TEDS,
{pdf_tables} documents have one) and headings (MHS, {pdf_headings} documents), and averages the three. We ran all {all_tools}
tools on those PDFs in this run and scored every output with the same program
([`score_pdfbench.py`](score_pdfbench.py), which calls OpenDocKit's scorer in [`evals/`](../evals/README.md); that
scorer was checked against the per-document scores the benchmark itself publishes).

{pdf_table}

- {pdf_failures}
- **kordoc is the best of the six here, and by a clear margin.** Docling is second and OpenDocKit third. kordoc and
  Docling read a page with models as well as with rules; OpenDocKit has only rules. That is most visible in the table
  row.
- **The price is time and memory.** kordoc took {pdf_kordoc_times} times as long as OpenDocKit and Docling
  {pdf_docling_times} times, on a machine without a GPU; OpenDocKit stayed under {pdf_us_memory} MB.
- **Two of these scores are not blind tests.** OpenDocKit keeps these 200 documents as a regression gate (no change
  may lower a mean, [`evals/README.md`](../evals/README.md)), and kordoc's
  [changelog](https://github.com/chrisryugj/kordoc/blob/main/docs/benchmarks.md) tracks them document by document, so
  both are likely to score higher here than on PDFs neither has seen. The benchmark is also English: it says little about Korean PDFs, which is what kordoc is built for.
- The benchmark's own leaderboard lists engines we did not run (OpenDataLoader, Marker, MinerU and others). Their
  published figures are in its README; the scores here are from this run only, with each tool's current version.
- OpenDocKit ran with page markers and image placeholders switched off, as in its own scorecard: they are marks it
  adds, not text of the document ([`adapters.py`](adapters.py)).

## 4. Real files from other projects

{files} documents taken from the test suites of Apache POI, pdfplumber, hwplib and hwpxlib
([`corpus.py`](corpus.py) picks up to 100 per format at even intervals). Nobody here wrote or chose these files for a
particular tool. We do not know their correct output, so this section counts what happened, and then counts words.

{corpus_table}

{problem_table}

- "Returned text" means the result was not empty, nothing more. Many of these test files are blank pages, pictures or
  shapes, so "no text" is often the right answer: of the {hwp_files} Hangul files OpenDocKit returned text for
  {hwp_ok}, and the rest state no text in their own records. kordoc returns a short placeholder for some of the same
  files, which this table counts as text. The word counts below are the fairer comparison.
- OpenDocKit reads legacy `.doc` and `.ppt` by handing them to LibreOffice, which this machine has: {legacy_ok} of
  {legacy_files} returned text. Without LibreOffice it refuses those two formats.
- Kreuzberg accepts `.hwp` files but returned no text for any of them.

### How many words of the source are in the output?

A DOCX, PPTX, XLSX or HWPX states its text in its own XML, and an HWP 5 file in its paragraph records. For every file
of the corpus that a tool returned text for, we looked up each word of the source in that tool's output
([`recall_corpus.py`](recall_corpus.py)). Nobody has to know the right Markdown for this.

{recall_table}

A word left out on purpose counts exactly like a word that was lost, so the number needs the place beside it:

<details>
<summary>Where the missing words sit</summary>

{where_table}

</details>

- OpenDocKit's missing words are almost all left out by design: placeholder prompts ("click here to enter text",
  `sdtContent` in DOCX, and one template each in HWP and HWPX), page headers and footers in DOCX
  (`include_headers_footers` brings them back), a hidden sheet in XLSX (`include_hidden_sheets`), and the date and
  footer placeholders of notes pages in PPTX. One DOCX is a measuring artefact: its `ﬁ` ligatures are written as two
  letters, and the count takes `identified` for a different word.
- We have not gone through the other tools' missing words one by one. The places say most of it: speaker notes in
  PPTX, footnotes, headers and footers in DOCX, and text inside table cells and boxes (`subList`) in HWPX.

### Time

{time_table}

Three workers per tool ran in parallel on the 4-core machine; times are the sum over files of the time inside the
conversion call. Docling, Unstructured and kordoc run layout or OCR models, which is why they are slow on a machine
without a GPU and why they can read scans that OpenDocKit, without a model, cannot.

## 5. Hostile input

The {hostile_files} files those same projects keep as fuzzer findings and proof-of-concept attacks (ClusterFuzz test
cases, XML bombs, deeply nested tables). Each file ran in a fresh process with a 60-second limit and a 6 GB memory cap.

{hostile_table}

The clear difference is `poc-shared-strings.xlsx`, an Excel file whose shared-string table expands enormously: it
pushed {over_cap} past the 6 GB cap. OpenDocKit bounds what it unpacks; its slowest file took {us_hostile_slowest} s
and its largest process was {us_hostile_memory} MB. This is a small sample. It shows the absence of obvious problems,
not robustness in general.

## 6. Footprint

{footprint_table}

Sizes were measured on the runner after installing each tool as above, with every extra the comparison uses; a tool
installed without its extras is smaller. A model that is downloaded on first use is not in those sizes; after this
run the caches held {cache_note}.

## Which one should you use?

- **Many formats through one function, on a server that takes uploads from strangers, with no GPU** → OpenDocKit.
- **Korean office documents (HWP, HWPX)** → OpenDocKit or kordoc. kordoc if you work in Node.js, need OCR on the
  machine, or want to write HWPX as well as read it; OpenDocKit if you work in Python or the same pipeline also takes
  the other formats.
- **PDFs with hard tables, scanned PDFs and images, without calling a service** → kordoc or Docling. They run models
  on the machine; OpenDocKit needs a vision-model API for scans and reads tables by rules.
- **Sheer throughput** → Kreuzberg.
- **Audio, video, web URLs, archives, Outlook `.msg`** → MarkItDown or Unstructured; OpenDocKit does not read them.

## Run it yourself

On GitHub: fork the repository, open **Actions → Benchmark → Run workflow**. It takes about an hour on a free runner
and uploads the raw results as an artifact.

Locally, on an x86-64 Linux machine (the ML-based tools crash under Docker on Apple silicon):

```bash
docker build -t opendockit-bench benchmarks
sh benchmarks/fetch_corpus.sh /tmp/corpus
docker run --rm -v "$PWD/benchmarks":/bench -v /tmp/corpus:/corpus:ro opendockit-bench sh run_all.sh
python benchmarks/make_report.py && python benchmarks/charts.py
```

The container holds the five Python tools. kordoc needs Node.js 20 or newer and the PDF benchmark needs its documents
(`sh evals/fetch_corpus.sh <dir>`, then `PDFBENCH=<dir>/opendataloader-bench`); the workflow sets up both.

| File | What it does |
| --- | --- |
| [`setup_tools.sh`](setup_tools.sh) | Installs the {all_tools} tools, pinned, each in its own environment, and measures their size |
| [`adapters.py`](adapters.py), [`worker_kordoc.mjs`](worker_kordoc.mjs) | The one function per tool that the benchmark calls |
| [`make_cases.py`](make_cases.py), [`make_legacy_cases.sh`](make_legacy_cases.sh) | Build the documents in [`cases/`](cases/) |
| [`probes.py`](probes.py) | The expected text per format and the structure checks |
| [`corpus.py`](corpus.py), [`fetch_corpus.sh`](fetch_corpus.sh) | Select and download the public test files, at fixed revisions |
| [`harness.py`](harness.py), [`worker.py`](worker.py) | Run each tool in worker processes with time and memory limits |
| [`score_pdfbench.py`](score_pdfbench.py), [`recall_corpus.py`](recall_corpus.py) | Score the PDF benchmark and count the missing words |
| [`report.py`](report.py), [`make_report.py`](make_report.py), [`charts.py`](charts.py) | Score the saved outputs, write this page and the charts |
| [`results/`](results/) | Raw results: per-file outcomes, timings, scores, and each tool's output for every case |

## Limits of this benchmark

- **Author bias.** We chose the documents and wrote the checks of sections 1 and 2, and a maintainer of another tool
  would have picked different ones. OpenDocKit was developed with all of them in view, which no other tool here was.
- **Small and synthetic.** The documents of sections 1 and 2 are {formats} files we generated. They are realistic but
  not real, and several tools were not built for the Korean formats that make up part of the set.
- **Defaults only.** Each tool ran with default options. Docling, Unstructured and kordoc in particular have settings
  (OCR engines, table modes, a GPU) that change both quality and speed.
- **One machine, no GPU.** Timings come from one run on a shared 4-core runner; treat differences under 2× as noise.
  A second full run on another runner gave the same scores and, within one file, the same counts. With a GPU the model-based tools are faster.
- **Little Korean.** The PDF benchmark is English, and the Hangul files of section 4 are small library samples, not
  the long official documents people actually convert.
- **A snapshot.** The other tools are pinned to what was current on 2026-11-23. All {all_tools} projects move quickly.
"""


if __name__ == "__main__":
    main()
