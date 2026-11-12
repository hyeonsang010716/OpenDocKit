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
         "docling": "Docling"}
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
    width, left, right, top, step, thickness = 760, 150, 110, 64, 38, 22
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
    (ROOT / "charts").mkdir(exist_ok=True)

    passed = {tool: sum(bool(row[tool]) for row in summary["probes"].values()) for tool in tools}
    rows = sorted(((tool, passed[tool], f"{passed[tool]} of {len(PROBES)}") for tool in tools), key=lambda row: -row[1])
    bar_chart("Structure checks passed", f"{len(PROBES)} yes/no checks on documents with known content. Tool versions and method: benchmarks/README.md",
              rows, len(PROBES), ROOT / "charts" / "structure-checks.svg")


if __name__ == "__main__":
    main()
