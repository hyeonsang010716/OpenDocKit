"""Download Korean press releases published as both HWPX and PDF, for the pairs scorecard (`evals/README.md`).

    uv run python evals/fetch_korean_pairs.py <target-dir>               # the releases listed in korean_pairs.tsv
    uv run python evals/fetch_korean_pairs.py <target-dir> --find 60 --from 156790125   # look for new ones and list them
    uv run python evals/fetch_korean_pairs.py <target-dir> --list korean_pairs_holdout.tsv   # another list (see below)
    uv run python evals/fetch_korean_pairs.py <target-dir> --list <file> --find 60 --from 156780000 --stride 3000

Korea's policy briefing site (korea.kr) publishes each ministry's press release with the files the ministry attached,
usually the same document as HWP, HWPX and PDF. A release is taken when it has exactly one HWPX and one PDF of the same
name, so the pair is one document in two formats. The files go to `<target-dir>/hwpx/<id>.hwpx` and
`<target-dir>/pdf/<id>.pdf`; they are never committed — only the list of what was taken is (`korean_pairs.tsv`).

`--stride` steps that many ids down after each release taken, so a list reaches over months or years and not over the
two days sixty releases in a row come from: `korean_pairs_holdout.tsv` was found so, from September 2026 back to 2023,
and is the list no rule was written on (`evals/README.md`).
"""

import argparse
from html import unescape
from pathlib import Path
import re
import sys
import time
import urllib.request

from opendockit.cli import _write_output_as_utf8_lines


SITE = "https://www.korea.kr"
LISTED = Path(__file__).with_name("korean_pairs.tsv")
_ATTACHMENT = re.compile(r'<a href="/common/download\.do\?fileId=(\d+)&amp;tblKey=GMN">\s*<img[^>]*>([^<]+?)\s*</a>')
_PAUSE = 1.0  # seconds between requests
_MAX_BYTES = 30 * 1024 * 1024


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (document-conversion test corpus)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(_MAX_BYTES + 1)


def pair_of(news_id: int) -> tuple[int, int] | None:
    """The file ids (HWPX, PDF) of a release that attaches exactly one of each under one name."""
    try:
        page = _get(f"{SITE}/briefing/pressReleaseView.do?newsId={news_id}").decode("utf-8", "replace")
    except OSError:
        return None
    files = {unescape(name).strip(): int(file_id) for file_id, name in _ATTACHMENT.findall(page)}
    hwpx = [name for name in files if name.lower().endswith(".hwpx")]
    pdf = [name for name in files if name.lower().endswith(".pdf")]
    if len(hwpx) != 1 or len(pdf) != 1 or Path(hwpx[0]).stem != Path(pdf[0]).stem:
        return None
    return files[hwpx[0]], files[pdf[0]]


def _download(target: Path, news_id: int, hwpx: int, pdf: int) -> bool:
    for folder, suffix, file_id, magic in (("hwpx", ".hwpx", hwpx, b"PK"), ("pdf", ".pdf", pdf, b"%PDF")):
        path = target / folder / f"{news_id}{suffix}"
        if path.is_file():
            continue
        data = _get(f"{SITE}/common/download.do?fileId={file_id}&tblKey=GMN")
        time.sleep(_PAUSE)
        if len(data) > _MAX_BYTES or not data.startswith(magic):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("--find", type=int, default=0, help="look for this many new pairs and write them to korean_pairs.tsv")
    parser.add_argument("--from", dest="start", type=int, help="the release id to count down from with --find")
    parser.add_argument("--stride", type=int, default=0, help="with --find, the ids to step down after each release taken")
    parser.add_argument("--list", dest="listed", default=LISTED.name, help="the list in evals/ to read, or to write with --find")
    args = parser.parse_args()
    listed = LISTED.with_name(args.listed)
    if args.find:
        if args.start is None:
            parser.error("--find needs --from")
        found: list[tuple[int, int, int]] = []
        news_id = args.start
        tried = 0
        while len(found) < args.find and tried < args.find * 20:
            pair = pair_of(news_id)
            time.sleep(_PAUSE)
            tried += 1
            if pair is not None and _download(args.target, news_id, *pair):
                found.append((news_id, *pair))
                print(f"{len(found):3} {news_id}", flush=True)
                news_id -= args.stride
            news_id -= 1
        listed.write_text("# release id, HWPX file id, PDF file id (korea.kr)\n" + "".join(f"{a}\t{b}\t{c}\n" for a, b, c in found), encoding="utf-8")
        return 0
    missing = 0
    for line in listed.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        news_id, hwpx, pdf = (int(part) for part in line.split("\t"))
        if not _download(args.target, news_id, hwpx, pdf):
            missing += 1
            print(f"not available any more: {news_id}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    _write_output_as_utf8_lines()   # UTF-8 whatever the code page, as the command line (Pinned by: test_evals_output.py)
    sys.exit(main())
