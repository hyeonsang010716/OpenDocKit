"""One conversion worker. Reads a JSON request per line on stdin, writes a JSON reply per line on stdout.

Runs inside the virtual environment of the tool it serves. The harness kills and restarts it on a timeout.
"""

import json
import os
import resource
import sys
import time
import traceback


def main() -> None:
    tool = sys.argv[1]
    real_stdout = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # anything a library prints goes to stderr, never into the protocol
    sys.stdout = sys.stderr
    started = time.perf_counter()
    from adapters import ADAPTERS, version

    convert = ADAPTERS[tool]
    real_stdout.write(json.dumps({"ready": True, "version": version(tool), "import_seconds": time.perf_counter() - started}) + "\n")
    real_stdout.flush()
    for line in sys.stdin:
        request = json.loads(line)
        reply = {"path": request["path"]}
        began = time.perf_counter()
        try:
            text = convert(request["path"]) or ""
            reply.update(ok=True, chars=len(text.strip()))
            if request.get("out"):
                with open(request["out"], "w", encoding="utf-8") as handle:
                    handle.write(text)
        except BaseException as exc:  # noqa: BLE001 — every failure is a result, including SystemExit raised by a library
            reply.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300], trace=traceback.format_exc()[-600:])
        reply["seconds"] = time.perf_counter() - began
        reply["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux reports kilobytes
        real_stdout.write(json.dumps(reply) + "\n")
        real_stdout.flush()


if __name__ == "__main__":
    main()
