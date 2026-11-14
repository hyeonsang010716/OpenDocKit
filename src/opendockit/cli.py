"""Command-line entry point for trying a conversion.

    opendockit <file> [--count | --json] [--describe [--model NAME]]

`--describe` has an OpenAI vision model describe the images (this costs money). The key is read from the `OPENAI_API_KEY`
environment variable, and `--describe` needs the optional extra: `pip install "opendockit[openai]"`.
"""

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from opendockit import (
    ConversionError,
    ConversionResult,
    OpenAIImageDescriber,
    convert_file,
    describe_images,
)
from opendockit.describe import DEFAULT_MODEL


def main() -> int:
    _write_output_as_utf8()
    parser = argparse.ArgumentParser(description="Convert a document to Markdown ready for LLM input.")
    parser.add_argument("path")
    # Given both, only --count was honored and --json was silently ignored
    # (Pinned by: test_cli.py::TestArguments::test_count_and_json_cannot_be_used_together).
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print JSON that includes the sections, metadata, warnings and image list")
    output.add_argument("--count", action="store_true",
                        help="print only the character count instead of the text ('exceeded' when over the limit). "
                             "Add --describe to also count the text inside pictures")
    parser.add_argument("--describe", action="store_true",
                        help="describe the images with an OpenAI vision model and fill the descriptions into the text")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model to use for --describe (default {DEFAULT_MODEL})")
    args = parser.parse_args()

    path = Path(args.path)
    describer = OpenAIImageDescriber(model_name=args.model) if args.describe else None
    try:
        # convert_file refuses a regular file over the limit from its size before reading it, and reads anything else (a
        # pipe, /dev/stdin) only up to the limit, so a file over the limit is never read whole.
        result = convert_file(path)
        if describer is not None and result.images:
            result = asyncio.run(_describe(result, describer))
    except ConversionError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 1
    except _DescriberUnavailable as exc:
        print(f"Cannot use --describe: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    except OSError:
        print(f"Could not read the file (it may be a directory, or permission may be missing): {path}", file=sys.stderr)
        return 1

    try:
        _print_result(result, args)
    except BrokenPipeError:
        # The reader closed first, as with `| head`. Finish without dumping a traceback. Point standard output at /dev/null so
        # the error does not fire again at exit while flushing the remaining output buffer
        # (the SIGPIPE advice in the Python docs, Pinned by: test_cli.py::TestArguments::test_closed_pipe_ends_quietly).
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    return 0


def _write_output_as_utf8() -> None:
    """Write standard output and error as UTF-8. Under `PYTHONIOENCODING=ascii` or a Windows redirect (cp949), Korean text ended in
    a UnicodeEncodeError traceback (Pinned by: test_cli.py::TestArguments::test_output_is_written_as_utf8_even_when_stdout_is_not).
    A stream that cannot be reconfigured is left as it is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # a closed stream, or one that cannot be reconfigured
            pass


def _print_result(result: ConversionResult, args: argparse.Namespace) -> None:
    if args.count:
        print(f"{result.char_count}{' exceeded' if result.exceeded else ''}")
        if result.images and not args.describe:
            print(f"[warning] The text in {len(result.images)} picture(s) was not counted. Add --describe to include it.", file=sys.stderr)
        for warning in result.warnings:
            print(f"[warning] {warning}", file=sys.stderr)
    elif args.json:
        payload = asdict(result)
        for key in ("base_warnings", "options", "fallbacks", "counted", "marker_key"):
            payload.pop(key)
        for section in payload["sections"]:
            section.pop("template")
        for image in payload["images"]:
            # Picture bytes are written as their size only. So are the tiles of a very tall picture (`tiles`) — they came out as
            # a repr and the output went over 1MB
            # (Pinned by: test_cli.py::test_json_output_summarizes_picture_tiles_like_picture_data).
            image["data"] = f"<{len(image['data'])} bytes>"
            image["tiles"] = [f"<{len(tile)} bytes>" for tile in image["tiles"]]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(result.text)
        for warning in result.warnings:
            print(f"[warning] {warning}", file=sys.stderr)


class _DescriberUnavailable(Exception):
    """The vision model could not be set up (missing extra, missing API key)."""


async def _describe(result: ConversionResult, describer: OpenAIImageDescriber) -> ConversionResult:
    """Close the HTTP client the describer created in this loop before the loop ends.

    The model is built first: without the `openai` extra or an API key every image would otherwise just come back as
    "could not be described" with exit code 0 (Pinned by: test_cli.py::TestDescribeSetup).
    """
    try:
        try:
            describer.check()
        except Exception as exc:
            raise _DescriberUnavailable(str(exc)) from exc
        return await describe_images(result, describer)
    finally:
        await describer.aclose()


if __name__ == "__main__":
    sys.exit(main())
