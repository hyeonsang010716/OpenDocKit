"""Convert the image and scanned-PDF cases with OpenDocKit plus a vision model, for the "with a vision model" column.

    OPENAI_API_KEY=... uv run python benchmarks/vision_cases.py [results-dir]

This is kept apart from the main run on purpose: it calls a paid API, its wording varies from run to run, and no other
tool in the comparison is given a remote model. Results go to <results>/cases/opendockit-vision(.json).
"""

import asyncio
import json
from pathlib import Path
import sys
import time

from opendockit import OpenAIImageDescriber, aconvert
from opendockit.describe import DEFAULT_MODEL


ROOT = Path(__file__).parent
FILES = ["scanned-notice.pdf", "invoice.png", "receipt.jpg", "fax.tiff", "ticket.heic"]


async def main() -> None:
    results = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "results") / "cases"
    (results / "opendockit-vision").mkdir(parents=True, exist_ok=True)
    describer = OpenAIImageDescriber()
    describer.check()
    replies = {}
    try:
        for name in FILES:
            began = time.perf_counter()
            result = await aconvert((ROOT / "cases" / name).read_bytes(), name, describer=describer)
            (results / "opendockit-vision" / f"{name}.md").write_text(result.text, encoding="utf-8")
            replies[name] = {"path": name, "ok": True, "outcome": "ok" if result.text.strip() else "empty", "chars": len(result.text),
                             "seconds": time.perf_counter() - began, "warnings": result.warnings}
            print(f"{name:22} {replies[name]['seconds']:5.1f}s  {result.text[:70]!r}")
    finally:
        await describer.aclose()
    (results / "opendockit-vision.json").write_text(json.dumps(
        {"tool": "opendockit-vision", "version": f"0.1.0 + {DEFAULT_MODEL}", "results": replies}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
