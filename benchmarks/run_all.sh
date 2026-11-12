#!/bin/sh
# Run the suites. Needs the tools from setup_tools.sh and, for the corpus suites, the files from fetch_corpus.sh.
#   sh run_all.sh [all|cases|corpus]        (CORPUS=/path/to/corpus, default /corpus)
set -e
cd "$(dirname "$0")"
python="${BENCH_VENVS:-/venvs}/opendockit/bin/python"
corpus="${CORPUS:-/corpus}"
suite="${1:-all}"
mkdir -p results
$python -c "
import json, pathlib
files = sorted(p for p in pathlib.Path('cases').iterdir() if p.is_file())
pathlib.Path('results/cases.json').write_text(json.dumps([{'id': p.name, 'path': str(p.resolve())} for p in files], indent=1))"
if [ "$suite" = all ] || [ "$suite" = cases ]; then
    $python harness.py cases results/cases.json results --workers 1 --timeout 300
fi
if [ "$suite" = all ] || [ "$suite" = corpus ]; then
    $python corpus.py "$corpus" results
    $python harness.py ordinary results/ordinary.json results --workers 3 --timeout 120
    $python harness.py hostile results/hostile.json results --workers 3 --timeout 60 --fresh
fi
$python report.py results
