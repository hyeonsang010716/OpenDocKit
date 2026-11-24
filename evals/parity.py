"""Does one document saved as HWP and as HWPX convert to the same text? Compare the two conversions of every pair.

    uv run python -m evals.parity --documents <dir> [--baseline <file>] [--update-baseline] [--show <n>]

The two formats hold one document model in two encodings - records in one, XML in the other - and they are read here by
two readers that share their rules but not their code. A rule added to one reader and not to the other is seen by no
scorecard: each output is fine by itself. A document that exists in both formats shows it at once, and needs no
reference: the two conversions have to be the same text.

A pair is two files of one folder with one name, `<name>.hwp` and `<name>.hwpx` (the samples of hwp2hwpx are such pairs:
`evals/fetch_hangul_samples.py`). Each pair is

- `alike`: both are converted, to the same text;
- `apart`: both are converted, and the texts differ;
- `refused`: one or both are refused.

A pair that is apart is not always a defect of a reader - the two files may differ themselves - so a run is compared
with a baseline, and ends with status 1 when a pair that was alike is not alike any more. `--show` prints the first
blocks that differ of that many pairs.
"""

import argparse
from difflib import SequenceMatcher
import json
from pathlib import Path
import sys

from opendockit import ConversionError, convert_file


def _text(path: Path) -> str | None:
    try:
        return convert_file(path).text
    except ConversionError:
        return None


def first_difference(first: str, second: str) -> tuple[str, str] | None:
    """The first stretch of blocks (what a blank line parts) that differs, as (in the first text, in the second)."""
    blocks, others = first.split("\n\n"), second.split("\n\n")
    for tag, a, b, c, d in SequenceMatcher(None, blocks, others, autojunk=False).get_opcodes():
        if tag != "equal":
            return "\n\n".join(blocks[a:b]), "\n\n".join(others[c:d])
    return None


def run(documents: Path) -> tuple[dict, dict[str, tuple[str, str]]]:
    """The card of the folder - the state of each pair, named without its suffix - and, for the pairs that are apart, the
    first blocks that differ (HWP, HWPX). The card holds no text of a document: it is what a baseline keeps."""
    pairs, differences = {}, {}
    for path in sorted(documents.glob("*.hwp")):
        twin = path.with_suffix(".hwpx")
        if not twin.is_file():
            continue
        first, second = _text(path), _text(twin)
        if first is None or second is None:
            pairs[path.stem] = "refused"
        elif first == second:
            pairs[path.stem] = "alike"
        else:
            pairs[path.stem] = "apart"
            differences[path.stem] = first_difference(first, second) or ("", "")
    totals = {state: sum(value == state for value in pairs.values()) for state in ("alike", "apart", "refused")}
    return {"totals": {"pairs": len(pairs), **totals}, "pairs": pairs}, differences


def compare(card: dict, baseline: dict) -> tuple[list[str], bool]:
    """Lines saying what changed against `baseline`, and whether a pair that was alike is not any more."""
    lines, worse = [], False
    for key in ("pairs", "alike", "apart", "refused"):
        lines.append(f"{key:<10}{baseline['totals'].get(key, 0):>10}{card['totals'][key]:>10}")
    for name, state in sorted(card["pairs"].items()):
        was = baseline["pairs"].get(name)
        if was is not None and was != state:
            lines.append(f"  {name}: {was} -> {state}")
            worse = worse or was == "alike"
    return lines, worse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--documents", type=Path, required=True, help="folder holding documents as <name>.hwp and <name>.hwpx")
    parser.add_argument("--baseline", type=Path, help="states of the pairs to compare with")
    parser.add_argument("--update-baseline", action="store_true", help="write this run to --baseline instead of comparing")
    parser.add_argument("--show", type=int, default=0, help="how many pairs that are apart to print the first difference of")
    arguments = parser.parse_args(argv)
    if not arguments.documents.is_dir():
        parser.error(f"no such folder: {arguments.documents}")

    card, differences = run(arguments.documents)
    totals = card["totals"]
    print(f"{totals['pairs']} pairs: {totals['alike']} alike, {totals['apart']} apart, {totals['refused']} refused")
    for name in sorted(differences)[:arguments.show]:
        first, second = differences[name]
        print(f"--- {name}")
        print("  hwp:  " + first[:300].replace("\n", "\n        "))
        print("  hwpx: " + second[:300].replace("\n", "\n        "))
    if arguments.baseline is None:
        return 0
    if arguments.update_baseline:
        arguments.baseline.write_text(json.dumps(card, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    lines, worse = compare(card, json.loads(arguments.baseline.read_text(encoding="utf-8")))
    print(f"{'':<10}{'baseline':>10}{'now':>10}")
    print("\n".join(lines))
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
