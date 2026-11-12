"""Convert every file in `examples/samples/` and write the Markdown to `examples/output/`.

    uv run python examples/run_examples.py              # offline: pictures stay as [Image] placeholders
    uv run python examples/run_examples.py --describe   # also describe pictures with a vision model (needs OPENAI_API_KEY)

With `--describe`, each sample that contains pictures gets a second output, `<name>.described.md`.
The outputs are committed so you can read them without running anything; rerun this script after changing the converter
to see exactly what moved.
"""

import argparse
import asyncio
from pathlib import Path

from opendockit import ConversionResult, OpenAIImageDescriber, convert_file, describe_images


HERE = Path(__file__).parent
SAMPLES, OUTPUT = HERE / "samples", HERE / "output"


def write(name: str, result: ConversionResult) -> None:
    text = result.text + "\n"
    if result.warnings:
        text += "\n<!-- warnings:\n" + "\n".join(f"- {warning}" for warning in result.warnings) + "\n-->\n"
    (OUTPUT / name).write_text(text, encoding="utf-8")


async def describe_all(results: dict[str, ConversionResult]) -> None:
    describer = OpenAIImageDescriber()
    describer.check()
    try:
        for name, result in results.items():
            if result.images:
                write(f"{name}.described.md", await describe_images(result, describer))
                print(f"  described {name} ({len(result.images)} image(s))")
    finally:
        await describer.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--describe", action="store_true", help="describe pictures with a vision model (costs money)")
    args = parser.parse_args()

    OUTPUT.mkdir(exist_ok=True)
    results = {}
    for path in sorted(SAMPLES.iterdir()):
        if path.suffix == ".md" and path.name.isupper() or path.name.startswith("THIRD_PARTY"):
            continue
        result = results[path.name] = convert_file(path)
        write(f"{path.name}.md", result)
        print(f"{path.name:28} {result.format.value:10} {result.char_count:>6} chars  {len(result.images)} image(s)  "
              f"{len(result.warnings)} warning(s)")
    if args.describe:
        asyncio.run(describe_all(results))


if __name__ == "__main__":
    main()
