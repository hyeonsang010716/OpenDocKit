"""Select the public test documents used by the corpus and hostile-input suites.

    python corpus.py <corpus-dir> <out-dir>

`<corpus-dir>` is what `fetch_corpus.sh` downloads: the test data of Apache POI, pdfplumber, hwplib and hwpxlib — files
written by other projects to test their own parsers, so nobody here chose them to suit a particular tool.

- ordinary.json: up to PER_FORMAT files of each format, taken at even intervals from the sorted list.
- hostile.json: every file those projects keep as a fuzzer finding or proof-of-concept attack (by name).
"""

import json
from pathlib import Path
import sys


FORMATS = {".pdf": "PDF", ".docx": "DOCX", ".pptx": "PPTX", ".xlsx": "XLSX", ".xls": "XLS", ".doc": "DOC", ".ppt": "PPT",
           ".hwp": "HWP", ".hwpx": "HWPX"}
HOSTILE_MARKS = ("clusterfuzz", "crash-", "poc-", "fuzz", "oom-", "timeout-", "xmlbomb", "deep-")
PER_FORMAT = 100
MAX_BYTES = 20 * 1024 * 1024


def main() -> None:
    corpus, out = Path(sys.argv[1]), Path(sys.argv[2])
    by_format: dict[str, list[Path]] = {}
    hostile: list[Path] = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() not in FORMATS or path.stat().st_size > MAX_BYTES:
            continue
        # pdfplumber keeps its fuzzer findings as numbered PDFs; POI marks them in the file name
        if any(mark in path.name.lower() for mark in HOSTILE_MARKS) or (path.suffix == ".pdf" and path.stem.isdigit()):
            hostile.append(path)
        else:
            by_format.setdefault(FORMATS[path.suffix.lower()], []).append(path)

    def entry(path: Path, index: int) -> dict:
        return {"id": f"{index:04d}{path.suffix.lower()}", "path": str(path), "format": FORMATS[path.suffix.lower()],
                "source": str(path.relative_to(corpus))}

    ordinary = []
    for name, paths in sorted(by_format.items()):
        step = max(len(paths) / PER_FORMAT, 1)
        chosen = [paths[int(i * step)] for i in range(min(PER_FORMAT, len(paths)))]
        ordinary += chosen
        print(f"{name:5} {len(chosen):4} of {len(paths)}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ordinary.json").write_text(json.dumps([entry(path, index) for index, path in enumerate(ordinary)], indent=1, ensure_ascii=False))
    (out / "hostile.json").write_text(json.dumps([entry(path, index) for index, path in enumerate(hostile)], indent=1, ensure_ascii=False))
    print(f"ordinary {len(ordinary)}  hostile {len(hostile)}")


if __name__ == "__main__":
    main()
