"""Draw the charts in benchmarks/charts/ from results/summary.json.

    python charts.py

Plain SVG written by hand so the repository needs no plotting library. One measure per chart, bars from a single
baseline, every bar labelled with its tool and value (identity never depends on colour), and colours that switch with
the reader's light or dark theme.
"""

import json
from pathlib import Path

from probes import PROBES


ROOT = Path(__file__).parent
NAMES = {"opendockit": "OpenDocKit", "markitdown": "MarkItDown", "kreuzberg": "Kreuzberg", "unstructured": "Unstructured",
         "docling": "Docling", "kordoc": "kordoc"}
MODELS = {"docling", "kordoc"}  # ran a layout or OCR model on the PDFs of the benchmark (make_report.py says why these two)
STYLE = """
  .surface { fill: #fcfcfb; } .title { fill: #0b0b0b; font-weight: 600; } .label { fill: #0b0b0b; }
  .note, .tick { fill: #52514e; } .axis { stroke: #d8d7d2; } .bar { fill: #9a9993; } .bar.us { fill: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; } .title, .label { fill: #f0efec; } .note, .tick { fill: #c3c2b7; }
    .axis { stroke: #3a3a37; } .bar { fill: #7d7c76; } .bar.us { fill: #3987e5; }
  }
  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 14px; }
"""


def bar_chart(title: str, note: str, rows: list[tuple[str, float, str]], maximum: float, path: Path) -> None:
    """Horizontal bars, largest first. `rows` holds (tool, value, text shown at the end of the bar)."""
    width, left, right, top, step, thickness = 760, 150, 210, 64, 38, 22
    plot = width - left - right
    height = top + step * len(rows) + 44
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
             f'aria-label="{title}">', f"<style>{STYLE}</style>", f'<rect class="surface" width="{width}" height="{height}" rx="8"/>',
             f'<text class="title" x="24" y="32" font-size="16">{title}</text>',
             f'<line class="axis" x1="{left}" y1="{top - 8}" x2="{left}" y2="{top + step * len(rows) - 8}"/>']
    for index, (tool, value, text) in enumerate(rows):
        y = top + index * step
        length = max(plot * value / maximum, 2)
        radius = min(4, length)
        shape = (f"M{left},{y} h{length - radius} a{radius},{radius} 0 0 1 {radius},{radius} v{thickness - 2 * radius} "
                 f"a{radius},{radius} 0 0 1 -{radius},{radius} h-{length - radius} z")
        weight = ' font-weight="600"' if tool == "opendockit" else ""
        parts.append(f'<text class="label" x="{left - 12}" y="{y + 16}" text-anchor="end"{weight}>{NAMES[tool]}</text>')
        parts.append(f'<path class="bar{" us" if tool == "opendockit" else ""}" d="{shape}"/>')
        parts.append(f'<text class="label" x="{left + length + 8}" y="{y + 16}">{text}</text>')
    parts.append(f'<text class="note" x="24" y="{height - 16}" font-size="12">{note}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    summary = json.loads((ROOT / "results" / "summary.json").read_text())
    tools = list(summary["tools"])
    charts = ROOT / "charts"
    charts.mkdir(exist_ok=True)
    source = "Tool versions and method: benchmarks/README.md"

    def draw(name: str, title: str, note: str, values: dict[str, float], text, maximum: float | None = None, *, lowest: bool = False) -> None:
        rows = sorted(((tool, values[tool], text(tool)) for tool in tools), key=lambda row: row[1] if lowest else -row[1])
        bar_chart(title, f"{note} {source}", rows, maximum or max(values.values()), charts / name)

    passed = {tool: sum(bool(row[tool]) for row in summary["probes"].values()) for tool in tools}
    draw("structure-checks.svg", "Structure checks passed", f"{len(PROBES)} yes/no checks on documents with known content, written by us.",
         passed, lambda tool: f"{passed[tool]} of {len(PROBES)}", len(PROBES))

    read = {tool: sum(row[tool]["status"] == "pass" for row in summary["coverage"].values()) for tool in tools}
    draw("formats.svg", "Formats read without a remote model or service", f"One file for each of {len(summary['coverage'])} formats.",
         read, lambda tool: f"{read[tool]} of {len(summary['coverage'])}", len(summary["coverage"]))

    pdf = summary["pdfbench"]
    score = {tool: pdf[tool]["means"]["overall"]["mean"] for tool in tools}
    draw("pdf-benchmark.svg", "Score on the 200 PDFs of opendataloader-bench", "Reading order, tables and headings; scored in this run.",
         score, lambda tool: f"{score[tool]:.3f}" + (" · with models" if tool in MODELS else ""), 1.0)
    taken = {tool: pdf[tool]["total_seconds"] for tool in tools}
    draw("pdf-time.svg", "Seconds to convert those 200 PDFs (shorter is better)", "4 cores, no GPU.",
         taken, lambda tool: f"{taken[tool]:,.0f} s" + (" · with models" if tool in MODELS else ""), lowest=True)

    size = {tool: summary["install"][tool]["megabytes"] for tool in tools}
    draw("install-size.svg", "Installed size (smaller is better)", "Each tool with the extras for full document support.",
         size, lambda tool: f"{size[tool]:,} MB" if size[tool] < 1000 else f"{size[tool] / 1024:.1f} GB", lowest=True)


if __name__ == "__main__":
    main()
