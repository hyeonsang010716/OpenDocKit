"""Run a list of files through every tool, each in its own worker processes, with a per-file time limit.

    python harness.py <suite-name> <file-list.json> <results-dir> [--workers N] [--timeout S] [--fresh] [--tools a,b]

`file-list.json` is a list of {"id": ..., "path": ...}. Results go to <results-dir>/<suite>/<tool>.json and the converted
text of every file to <results-dir>/<suite>/<tool>/<id>.md. `--fresh` starts a new worker for every file, so the reported
peak memory belongs to that file alone (used for the hostile-input suite).
"""

import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time


TOOLS = ["opendockit", "markitdown", "kreuzberg", "unstructured", "docling"]
VENVS = Path(os.environ.get("BENCH_VENVS", "/venvs"))
# A worker that grows past this is stopped and the file is recorded as "memory": it keeps one hostile file from taking the
# whole machine down, and no ordinary document comes near it.
MEMORY_LIMIT_MB = 6000


class Worker:
    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.process = subprocess.Popen([str(VENVS / tool / "bin" / "python"), "worker.py", tool], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                        cwd=str(Path(__file__).parent))
        self.hello = self._read(600) or {}

    def _read(self, timeout: float) -> dict | None:
        deadline = time.perf_counter() + timeout
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], 0.25)
            if ready:
                line = self.process.stdout.readline()
                return json.loads(line) if line else {"died": True}
            if self._rss_mb() > MEMORY_LIMIT_MB:
                return {"memory": True}
            if time.perf_counter() > deadline:
                return None

    def _rss_mb(self) -> float:
        try:
            for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0.0

    def convert(self, path: str, out: str, timeout: float) -> dict:
        began = time.perf_counter()
        try:
            self.process.stdin.write(json.dumps({"path": path, "out": out}) + "\n")
            self.process.stdin.flush()
            reply = self._read(timeout)
        except (BrokenPipeError, OSError):
            reply = {"died": True}
        if reply is None:
            self.kill()
            return {"path": path, "ok": False, "outcome": "timeout", "seconds": time.perf_counter() - began}
        if reply.get("memory"):
            self.kill()
            return {"path": path, "ok": False, "outcome": "memory", "error": f"stopped above {MEMORY_LIMIT_MB} MB",
                    "seconds": time.perf_counter() - began, "peak_rss_mb": MEMORY_LIMIT_MB}
        if reply.get("died"):
            code = self.process.wait()
            return {"path": path, "ok": False, "outcome": "crash", "error": f"worker exited with code {code}",
                    "seconds": time.perf_counter() - began}
        reply["outcome"] = ("ok" if reply.get("chars") else "empty") if reply.get("ok") else "error"
        return reply

    def alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        self.process.kill()
        self.process.wait()


def run_tool(tool: str, files: list[dict], out_dir: Path, workers: int, timeout: float, fresh: bool) -> dict:
    (out_dir / tool).mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    queue = list(reversed(files))
    lock = threading.Lock()
    hello: dict = {}

    def loop() -> None:
        worker = None
        while True:
            with lock:
                if not queue:
                    break
                item = queue.pop()
            if worker is None or not worker.alive() or fresh:
                if worker is not None:
                    worker.kill()
                worker = Worker(tool)
                hello.update(worker.hello)
            reply = worker.convert(item["path"], str(out_dir / tool / f"{item['id']}.md"), timeout)
            reply.pop("trace", None)
            with lock:
                results[item["id"]] = reply
        if worker is not None:
            worker.kill()

    began = time.perf_counter()
    threads = [threading.Thread(target=loop) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {"tool": tool, "version": hello.get("version"), "import_seconds": hello.get("import_seconds"),
            "wall_seconds": time.perf_counter() - began, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("files")
    parser.add_argument("results")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--tools", default=",".join(TOOLS))
    args = parser.parse_args()
    files = json.loads(Path(args.files).read_text())
    out_dir = Path(args.results) / args.suite
    for tool in args.tools.split(","):
        summary = run_tool(tool, files, out_dir, args.workers, args.timeout, args.fresh)
        (out_dir / f"{tool}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
        tally: dict[str, int] = {}
        for reply in summary["results"].values():
            tally[reply["outcome"]] = tally.get(reply["outcome"], 0) + 1
        print(f"{args.suite:10} {tool:13} {summary['version']:10} {summary['wall_seconds']:7.1f}s {tally}", flush=True)


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    main()
