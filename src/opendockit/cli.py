"""Command-line entry point for trying a conversion.

    opendockit <file> [--count | --json] [--describe [--model NAME]]
    opendockit --version

`--describe` has an OpenAI vision model describe the images (this costs money). The key is read from the `OPENAI_API_KEY`
environment variable, and `--describe` needs the optional extra: `pip install "opendockit[openai]"`.
"""

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import asdict
import errno
import io
import json
import os
from pathlib import Path
import sys
from typing import TextIO

from opendockit import (
    ConversionError,
    ConversionResult,
    OpenAIImageDescriber,
    __version__,
    convert_file,
    describe_images,
)
from opendockit.describe import DEFAULT_MODEL


if os.name == "nt":
    from opendockit import _windows
else:
    _windows = None


def main() -> int:
    if sys.stderr is None:
        # File descriptor 2 is closed (`2>&-`): `print(file=None)` and argparse's usage errors wrote to standard output, so
        # 'File not found', the warnings and `usage: …` went into the output. Every message, a traceback too, now goes nowhere
        # (Pinned by: test_cli.py::TestArguments::test_messages_go_nowhere_when_standard_error_is_closed).
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # open for the life of the process
    _write_output_as_utf8_lines()
    parser = argparse.ArgumentParser(description="Convert a document to Markdown ready for LLM input.", add_help=False)
    parser.add_argument("-h", "--help", action=_Help, nargs=0, help="show this help message and exit")
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
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="write the output to FILE instead of standard output, as UTF-8 with LF line ends whatever the "
                             "shell (Windows PowerShell 5.1 re-encodes what a program prints)")
    # As `opendockit-mcp` and `opendockit-open-webui` have it; the bug report form asks for the version
    # (Pinned by: test_cli.py::TestArguments::test_version_is_printed_without_a_file).
    parser.add_argument("--version", action=_Version, nargs=0, help="show program's version number and exit")
    try:
        args = parser.parse_args()
    finally:
        _flush_errors()   # argparse's usage error: it ignores a failed write, and the message was flushed only at exit

    path = Path(args.path)
    target = Path(args.output) if args.output is not None else None
    if target is not None and _same_file(path, target):
        # Checked before converting: the output would replace the document it was made from
        # (Pinned by: test_cli.py::TestOutputFile::test_the_document_itself_is_never_the_output).
        try:
            parser.error("argument -o/--output: names the document being converted, which it would replace")
        finally:
            _flush_errors()
    if target is not None and args.describe:
        # --describe calls a vision model that is paid for by the call, and the file is written only after it: a FILE that
        # cannot be written for certain is said before converting, as the write would say it, and nothing is touched
        # (Pinned by: test_cli.py::TestOutputFile::test_with_describe_a_file_that_cannot_be_written_is_said_before_converting).
        reason = _certainly_unwritable(target)
        if reason is not None:
            _error(f"Could not write the output file ({reason}): {target}")
            return 1
    describer = OpenAIImageDescriber(model_name=args.model) if args.describe else None
    try:
        # convert_file refuses a regular file over the limit from its size before reading it, and reads anything else (a
        # pipe, /dev/stdin) only up to the limit, so a file over the limit is never read whole.
        result = convert_file(path)
        if describer is not None and result.images:
            result = asyncio.run(_describe(result, describer))
    except ConversionError as exc:
        _error(f"[{exc.code}] {exc}")
        return 1
    except ImportError as exc:
        # A reader library that cannot be imported (`extractors.load`): its fixed sentence, not a traceback (Pinned by:
        # test_cli.py::TestFileErrors::test_reader_library_that_cannot_be_imported_prints_a_message)
        _error(str(exc))
        return 1
    except _DescriberUnavailable as exc:
        _error(f"Cannot use --describe: {exc}")
        return 1
    except FileNotFoundError:
        _error(f"File not found: {path}")
        return 1
    except OSError:
        if _windows is not None and _windows.held_by_another_program(str(path)):
            # The likeliest reason on Windows, which the generic sentence left out: another program holds the file open
            # without sharing it (Pinned by: test_cli.py::TestFileErrors::test_a_file_another_program_holds_open_says_so).
            _error(f"Could not read the file (another program has it open; close it there and try again): {path}")
        else:
            _error(f"Could not read the file (it may be a directory, or permission may be missing): {path}")
        return 1

    if target is not None:
        return _write_file(target, lambda stream: _print_result(result, args, stream))
    return _write(lambda: _print_result(result, args))


def _openable(path: Path) -> str:
    """`path` as the file system is asked for it: its long form on Windows when it is too long as it is."""
    return _windows.openable(str(path)) if _windows is not None else str(path)


def _same_file(document: Path, target: Path) -> bool:
    """Whether `target` is the document itself, by what the files are rather than how their names are spelled (a second
    name, another case, a link). False when either cannot be looked at: the conversion or the write reports that."""
    try:
        return os.path.samefile(_openable(document), _openable(target))
    except (OSError, ValueError):
        return False


# Why the output file could not be written, as `_write_file` and `_certainly_unwritable` both say it.
_FOLDER_MISSING = "its folder does not exist"
_NOT_WRITABLE = "it may be a directory, or permission may be missing"


def _certainly_unwritable(target: Path) -> str | None:
    """Why the write of `target` will fail, when that is certain without opening it: the file is a folder, or its folder
    does not exist; in the words the write would use. None otherwise, a permission or a file held open among them: the
    write finds those, and only it can say them for sure. Nothing is created or changed."""
    try:
        if os.path.isdir(_openable(target)):
            return _NOT_WRITABLE
        os.stat(_openable(target.parent))
    except FileNotFoundError:
        return _FOLDER_MISSING
    except (OSError, ValueError):
        pass
    return None


def _write_file(target: Path, print_output: Callable[[TextIO], None]) -> int:
    """Write the output to `target`: what standard output would have got, byte for byte, as UTF-8 with LF line ends.

    A redirect is the shell's to make, and Windows PowerShell 5.1 does not keep a program's bytes: it decodes them with the
    console's code page and writes them again, so under cp949, the Korean default, the Korean text of `> report.md` came
    out garbled (docs/limitations.md). The file is written by this program instead. It is opened only once the output is
    ready, so a conversion that fails leaves a file of that name as it was, where `>` empties it first; and it is written
    in place, as `>` writes it, so a link still leads to its target and the file keeps its permissions
    (Pinned by: test_cli.py::TestOutputFile). The warnings go to standard error as without the option.
    """
    buffer = io.StringIO()
    print_output(buffer)
    try:
        with open(_openable(target), "w", encoding="utf-8", errors="replace", newline="\n") as stream:
            stream.write(buffer.getvalue())
    except FileNotFoundError:
        _error(f"Could not write the output file ({_FOLDER_MISSING}): {target}")
        return 1
    except OSError:
        if _windows is not None and _windows.held_by_another_program(str(target), for_writing=True):
            _error(f"Could not write the output file (another program has it open; close it there and try again): {target}")
        else:
            _error(f"Could not write the output file ({_NOT_WRITABLE}): {target}")
        return 1
    return 0


def _write(print_output: Callable[[], None]) -> int:
    """Print the output and flush it; 0, or 1 when it could not reach its reader."""
    if sys.stdout is None:
        # Python sets sys.stdout to None when file descriptor 1 is closed (`>&-`, pythonw, a detached service): the flush
        # below failed with an AttributeError traceback. Nothing can be written, so it ends with exit code 1, as for a
        # closed pipe (Pinned by: test_cli.py::TestArguments::test_closed_standard_output_ends_quietly).
        return 1
    try:
        print_output()
        # Flushed here: output smaller than the buffer reached the closed pipe only at exit, outside this block, and ended
        # with an 'Exception ignored' message and exit code 120
        # (Pinned by: test_cli.py::TestArguments::test_closed_pipe_ends_quietly_after_a_short_output).
        sys.stdout.flush()
    except OSError as error:
        # The reader closed first, as with `| head`. Finish without dumping a traceback. Point standard output at /dev/null so
        # the error does not fire again at exit while flushing the remaining output buffer
        # (the SIGPIPE advice in the Python docs, Pinned by: test_cli.py::TestArguments::test_closed_pipe_ends_quietly).
        # Windows reports a closed pipe as EINVAL, not as a BrokenPipeError: a short output there ended in an OSError
        # traceback (Pinned by: test_cli.py::TestArguments::test_closed_pipe_ends_quietly_after_a_short_output).
        if not _reader_gone(error):
            raise
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    return 0


def _reader_gone(error: OSError) -> bool:
    """Whether a write failed because the reader of the pipe has gone. Windows reports a closed pipe as EINVAL, not as a
    BrokenPipeError."""
    return isinstance(error, BrokenPipeError) or (sys.platform == "win32" and error.errno == errno.EINVAL)


def _error(message: str) -> None:
    """A message or warning on standard error (a null device when it is closed, `main`, or when its reader has gone,
    `_flush_errors`)."""
    try:
        print(message, file=sys.stderr)
    except OSError as error:
        if not _reader_gone(error):
            raise
    _flush_errors()


def _flush_errors() -> None:
    """Flush standard error; when its reader has gone, point it at the null device, as `_write` does with standard output.

    With standard error sent to a pipe that was closed (`2>&1 | true`, a log reader that died) a warning or a message raised
    BrokenPipeError: a message for a missing file ended in a traceback, and a warning, or argparse's usage error (which
    ignores a failed write), stayed in the buffer and failed again at exit with 'Exception ignored … BrokenPipeError' and
    exit code 120. The messages now go nowhere and the exit code is the one the run would have had: the reader of the
    messages has gone, not the reader of the output
    (Pinned by: test_cli.py::TestArguments::test_messages_to_a_closed_pipe_end_quietly).
    """
    try:
        sys.stderr.flush()
    except OSError as error:
        if not _reader_gone(error):
            raise
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())
        except (OSError, ValueError):   # a stream without a file descriptor of its own
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # open for the life of the process


class _Help(argparse.Action):
    """`--help` printed through `_write`, as `--version` is: argparse's own help action exits before anything is flushed, so
    `opendockit --help | true` ended with 'Exception ignored … BrokenPipeError' and exit code 120
    (Pinned by: test_cli.py::TestArguments::test_version_and_help_to_a_closed_pipe_end_quietly)."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(_write(parser.print_help))


class _Version(argparse.Action):
    """`--version` printed through `_write`: argparse's own version action exits before anything is flushed, so
    `opendockit --version | true` ended with 'Exception ignored … BrokenPipeError' and exit code 120
    (Pinned by: test_cli.py::TestArguments::test_version_and_help_to_a_closed_pipe_end_quietly)."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(_write(lambda: print(f"opendockit {__version__}")))


def _write_output_as_utf8_lines() -> None:
    """Write standard output and error as UTF-8, with LF line ends on every platform.

    Under `PYTHONIOENCODING=ascii` or a Windows redirect (cp949), Korean text ended in a UnicodeEncodeError traceback
    (Pinned by: test_cli.py::TestArguments::test_output_is_written_as_utf8_even_when_stdout_is_not). Python writes a text
    stream with the line end of the platform, so on Windows every line of a redirected or piped output ended in CR LF: the
    same document gave other bytes than on Linux and macOS, and a Markdown file for a model carried a CR on every line. The
    text of a conversion has LF line ends, and the output now keeps them; error goes the same way, so that `2>&1` does not
    mix the two (Pinned by: test_cli.py::TestArguments::test_output_has_lf_line_ends_on_every_platform). A console shows
    the two alike. A stream that cannot be reconfigured is left as it is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (ValueError, OSError):  # a closed stream, or one that cannot be reconfigured
            pass


def _print_result(result: ConversionResult, args: argparse.Namespace, out: TextIO | None = None) -> None:
    """Print the output to `out`, standard output when it is None, and the warnings to standard error."""
    if args.count:
        print(f"{result.char_count}{' exceeded' if result.exceeded else ''}", file=out)
        if result.images and not args.describe:
            _error(f"[warning] The text in {len(result.images)} picture(s) was not counted. Add --describe to include it.")
        for warning in result.warnings:
            _error(f"[warning] {warning}")
    elif args.json:
        payload = asdict(result)
        for key in ("base_warnings", "options", "fallbacks", "counted", "marker_key", "time_limited", "reached_sections"):
            payload.pop(key)
        for section in payload["sections"]:
            section.pop("template")
        for image in payload["images"]:
            # Picture bytes are written as their size only. So are the tiles of a very tall picture (`tiles`) — they came out as
            # a repr and the output went over 1MB
            # (Pinned by: test_cli.py::test_json_output_summarizes_picture_tiles_like_picture_data).
            image["data"] = f"<{len(image['data'])} bytes>"
            image["tiles"] = [f"<{len(tile)} bytes>" for tile in image["tiles"]]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str), file=out)
    else:
        print(result.text, file=out)
        for warning in result.warnings:
            _error(f"[warning] {warning}")


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
