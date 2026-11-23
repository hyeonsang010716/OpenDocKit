#!/bin/sh
# Run the suites. Needs the tools from setup_tools.sh and, for the corpus suites, the files from fetch_corpus.sh;
# the PDF benchmark suite needs the documents evals/fetch_corpus.sh downloads.
#   sh run_all.sh [all|cases|corpus|pdfbench]   (CORPUS=/path/to/corpus, default /corpus; PDFBENCH=/path/to/opendataloader-bench)
set -e
cd "$(dirname "$0")"
venvs="${BENCH_VENVS:-/venvs}"
python="$venvs/opendockit/bin/python"
scorer="$venvs/scorer/bin/python"
corpus="${CORPUS:-/corpus}"
pdfbench="${PDFBENCH:-/corpus/opendataloader-bench}"
suite="${1:-all}"
mkdir -p results
[ -f "$venvs/install.json" ] && cp "$venvs/install.json" results/install.json
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
    # Words each document states in its own XML or records, looked for in what each tool wrote (evals/recall.py)
    PYTHONPATH=.. $scorer recall_corpus.py results
    $python harness.py hostile results/hostile.json results --workers 3 --timeout 60 --fresh
fi
if [ "$suite" = all ] || [ "$suite" = pdfbench ]; then
    # The 200 PDFs of opendataloader-bench, a benchmark none of the six projects here wrote, scored against its references
    $python -c "
import json, pathlib, sys
files = sorted(pathlib.Path(sys.argv[1], 'pdfs').glob('*.pdf'))
pathlib.Path('results/pdfbench.json').write_text(json.dumps([{'id': p.stem, 'path': str(p.resolve()), 'format': 'PDF'} for p in files], indent=1))" "$pdfbench"
    $python harness.py pdfbench results/pdfbench.json results --workers 3 --timeout 300
    PYTHONPATH=.. $scorer score_pdfbench.py results "$pdfbench/ground-truth/markdown"
fi
# What the tools downloaded into the home folder while they were set up and run (models, mostly), by folder
$python -c "
import json, pathlib, subprocess
home = pathlib.Path.home()
folders = [p for p in [*(home / '.cache').glob('*'), home / 'nltk_data', home / '.EasyOCR', home / '.paddleocr', home / '.kordoc'] if p.is_dir()]
sizes = {str(p.relative_to(home)): int(subprocess.run(['du', '-sm', str(p)], capture_output=True, text=True).stdout.split()[0]) for p in folders}
pathlib.Path('results/caches.json').write_text(json.dumps(sizes, indent=1)); print('caches', sizes)"
$python report.py results
