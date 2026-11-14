"""MCP server: lets an agent read local documents as Markdown.

    pip install "opendockit[mcp]"
    opendockit-mcp                       # stdio; any file the user running it can read
    opendockit-mcp --root ~/Documents    # only files under that folder (repeatable)

Two tools:

- `convert_document(path, sections=None, max_chars=100000)`: the Markdown of a file, or of some of its pages / slides /
  sheets (`sections="3"`, `"2-5"`).
- `document_outline(path)`: format, size and the list of pages / slides / sheets, to decide what to ask for.

Conversion is offline (no vision model, nothing fetched); pictures stay `[Image]` placeholders.
"""

import argparse
import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re
import stat

from opendockit import __version__
from opendockit.converter import _size_text, convert, read_limited, section_heading
from opendockit.exceptions import ConversionError, DocumentTooLargeError
from opendockit.isolated import IsolatedConverter
from opendockit.models import ConversionResult, ConvertOptions, Section, SectionKind


_NEEDS_SDK = 'The OpenDocKit MCP server needs the MCP SDK (2.0 or newer): pip install "opendockit[mcp]"'
try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError:  # the `opendockit-mcp` command exists in every install; without the extra it says what to install
    MCPServer = None


__all__ = ["create_server", "main"]

# What one call returns at most unless the caller asks for more. An agent's context is the scarce thing: a 400-page PDF
# returned whole would fill it, and the outline tool exists so that the agent can ask for the part it needs
# (Pinned by: test_mcp_server.py::TestConvertDocument::test_long_document_is_cut_at_max_chars_with_a_note_saying_how_to_read_the_rest).
DEFAULT_MAX_CHARS = 100_000
ROOTS_VARIABLE = "OPENDOCKIT_MCP_ROOTS"
_RANGE = re.compile(r"\s*(\d{1,9})\s*(?:-\s*(\d{1,9})\s*)?\Z")
_INSTRUCTIONS = (
    "Converts local documents (PDF, DOCX, PPTX, XLSX, HWP/HWPX, ODT/ODS/ODP, RTF, EPUB, HTML, email, CSV, text) to Markdown, "
    "keeping tables, headings and lists. For a long document call document_outline first, then convert_document with "
    "`sections` to read the pages, slides or sheets you need."
)
# What the SDK serves for every server, whether or not anything was registered for it.
_UNUSED_METHODS = ("prompts/list", "prompts/get", "resources/list", "resources/read", "resources/templates/list")
# The last conversions kept for the next call (`converted`): an agent reads a long document as document_outline and then
# convert_document by sections, and each call converted the whole file again. Bounded in count and in the characters one
# entry may hold, so the cache stays at tens of megabytes at most.
_CACHE_ENTRIES = 4
_CACHE_MAX_CHARS = 4_000_000
_UNIT = {SectionKind.PAGE: "Page", SectionKind.SLIDE: "Slide", SectionKind.SHEET: "Sheet", SectionKind.DOCUMENT: "Document"}


def _serve_tools_only(server: MCPServer) -> None:
    """Stop the server from announcing prompts and resources it does not have.

    The SDK answers `prompts/list` and `resources/list` on every server and therefore announces both capabilities. A
    client that trusts the announcement gives the agent tools for them — Hermes adds four per server — which can only
    ever return empty lists and cost the agent context in every request. The SDK has no switch for this, so the handlers
    are taken out of its table. That table is private: if a later SDK keeps it elsewhere the server announces prompts
    and resources again and still works, and the pinning test fails
    (Pinned by: test_mcp_server.py::TestCommandLine::test_server_announces_tools_and_nothing_it_does_not_have).
    """
    handlers = getattr(getattr(server, "_lowlevel_server", None), "_request_handlers", None)
    if isinstance(handlers, dict):
        for method in _UNUSED_METHODS:
            handlers.pop(method, None)


def create_server(roots: list[Path] | None = None, default_max_chars: int = DEFAULT_MAX_CHARS) -> MCPServer:
    """The server with its two tools. `roots`, when given, are the only folders files may be read from; each must be an
    existing folder (`ValueError` otherwise)."""
    if MCPServer is None:
        raise ImportError(_NEEDS_SDK)
    # A root that is a file is refused here as well as on the command line: taken as a root, it allowed nothing at all and
    # every call failed with 'outside the folders', which hid the mistake
    # (Pinned by: test_mcp_server.py::TestWhichFilesMayBeRead::test_root_that_is_a_file_is_refused_by_create_server).
    resolved = [root.expanduser().resolve() for root in roots or []]
    allowed = [_identity(root) if root.is_dir() else None for root in resolved]
    if None in allowed:
        raise ValueError("Every root must be an existing folder.")
    converter: IsolatedConverter | None = None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if converter is not None:
                await converter.aclose()

    server = MCPServer("opendockit", instructions=_INSTRUCTIONS, version=__version__, lifespan=lifespan)
    _serve_tools_only(server)

    # Content hash, file name and limit → the result of converting it, most recently used last.
    cache: OrderedDict[tuple[str, str, int | None], ConversionResult] = OrderedDict()

    async def converted(path: str, limit: int | None) -> ConversionResult:
        nonlocal converter
        file = _checked_path(path, allowed)
        options = ConvertOptions(max_chars=limit)
        try:
            data, digest = await asyncio.to_thread(_read, file, options.max_file_bytes)
            # The few last results are kept by what the file holds (its SHA-256), not by its path and times: a file changed
            # in place is converted again even when its size and modification time stay the same, and reading and hashing is
            # far cheaper than converting (Pinned by: test_mcp_server.py::TestRepeatedCalls).
            key = (digest, file.name, limit)
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
            if os.name != "posix":
                # IsolatedConverter is verified on Linux and macOS only (docs/limitations.md): elsewhere the file is converted
                # in a thread of this process, without the worker's memory and time limits.
                result = await asyncio.to_thread(convert, data, file.name, options)
            else:
                if converter is None:
                    # One worker process per file, ended after 2 minutes or 2 GB: a document that hangs or balloons must not
                    # take the server, and the agent's session, with it.
                    converter = IsolatedConverter(workers=1)
                result = await converter.convert(data, file.name, options)
        except ConversionError as error:
            raise ToolError(str(error)) from error
        except OSError as error:   # no permission, a file removed meanwhile
            raise ToolError("The file could not be read.") from error
        # A result a wall-clock limit cut short (the PDF time budget on a busy machine) is not kept: the next call may get the
        # full reading. A worker that ran out of time raises and is never kept either
        # (Pinned by: test_mcp_server.py::TestRepeatedCalls::test_result_cut_short_by_the_time_budget_is_not_kept).
        if not result.time_limited and len(result.text) + sum(len(section.text) for section in result.sections) <= _CACHE_MAX_CHARS:
            # The pictures are not needed for any answer (they stay placeholders), so they are not kept.
            cache[key] = replace(result, images=[], fallbacks={})
            while len(cache) > _CACHE_ENTRIES:
                cache.popitem(last=False)
        return result

    # Both tools only read: clients use the hint to decide whether a call needs the user's confirmation.
    reads_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    @server.tool(structured_output=False, annotations=reads_only, description=(
        "Convert a local document to Markdown.\n\n"
        "path: the file to read (absolute, or relative to where the server was started).\n"
        'sections: which pages, slides or sheets to return, by their numbers in document_outline: "3" or "2-5". '
        "Leave out for the whole document.\n"
        f"max_chars: the most characters of the document to return (default {default_max_chars:,}). A longer text is cut "
        "there, and a note after it says so."))
    async def convert_document(path: str, sections: str | int | None = None, max_chars: int = default_max_chars) -> str:
        if max_chars < 1:
            raise ToolError("max_chars must be at least 1.")
        chosen = _parse_range(sections)
        # A range is cut out of the whole document, so the whole document has to be read; only the answer is limited.
        result = await converted(path, None if chosen else max_chars)
        if chosen:
            text, note = _selected(result, chosen, max_chars)
        else:
            text = result.text
            note = ("The document is longer than max_chars and was cut. Call document_outline and read it by `sections`, "
                    "or raise max_chars.") if result.exceeded else ""
        warnings = [_one_line(warning) for warning in result.warnings if not warning.startswith("The conversion was stopped")]
        tail = [f"[{note}]"] if note else []
        if warnings:
            tail.append("Warnings:\n" + "\n".join(f"- {warning}" for warning in warnings))
        if not text.strip() and not tail:
            tail.append("[No text was extracted.]")
        return "\n\n".join(part for part in (text, *tail) if part)

    @server.tool(structured_output=False, annotations=reads_only, description=(
        "Describe a local document without returning its text: format, title, size, and its pages, slides or sheets with "
        "their numbers, titles and lengths. Use it to decide which `sections` to ask convert_document for.\n\n"
        "path: the file to read (absolute, or relative to where the server was started)."))
    async def document_outline(path: str) -> str:
        result = await converted(path, None)
        lines = [f"Format: {result.format.value}", f"Characters: {len(result.text):,}"]
        # What the document says about itself is its author's text: kept on one line, so that a title cannot pose as
        # another line of this outline (Pinned by: test_mcp_server.py::TestDocumentOutline::test_title_cannot_add_lines_to_the_outline).
        lines += [f"{key.replace('_', ' ').capitalize()}: {_one_line(value)}" for key, value in result.metadata.items()]
        units = _units(result)
        if units:
            lines.append("")
            lines += [_outline_line(section) for section in units]
        elif result.text.strip():
            lines.append("The document is not divided into pages, slides or sheets; convert_document returns it whole.")
        else:
            lines.append("No text was extracted.")
        if result.warnings:
            lines += ["", "Warnings:", *(f"- {_one_line(warning)}" for warning in result.warnings)]
        return "\n".join(lines)

    return server


# How a checked file is opened (`_read`): never through a symbolic link (the final name was resolved already, so a link there
# now was put in after the check), and without waiting on a FIFO put in place of the file — the regular-file check comes after
# the open, on what was opened. Where a flag does not exist (Windows) it is 0, and binary mode is asked for there.
_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_BINARY", 0))


def _read(file: Path, limit: int) -> tuple[bytes, str]:
    """The content of the regular file `_checked_path` returned and its SHA-256, refused from its size when over `limit` and
    otherwise read only up to one byte past it, so a file that grows meanwhile is refused by `convert` instead of being read
    whole. Run in a thread: hashing 50 MB takes tens of milliseconds, which would stall the event loop.

    The file is opened once and everything is decided on that descriptor: it must be a regular file, and the checked name
    must still lead to it (same device and inode). Checked by name and then opened by name again, the file could be swapped
    in between — for a link out of the allowed folders or a FIFO that blocks the read for ever
    (Pinned by: test_mcp_server.py::TestWhichFilesMayBeRead::test_file_swapped_after_the_check_is_refused).
    """
    try:
        descriptor = os.open(file, _OPEN_FLAGS)
    except OSError as error:
        if not file.is_symlink():
            raise
        raise ToolError("No file at that path.") from error
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not _names(file, status):
            raise ToolError("No file at that path.")
        if status.st_size > limit:
            raise DocumentTooLargeError(f"The file is too large (maximum {_size_text(limit)}).")
        data = read_limited(stream, limit, size_hint=status.st_size)
    return data, hashlib.sha256(data).hexdigest()


def _names(file: Path, opened: os.stat_result) -> bool:
    """Whether the name `file` leads, without following a link, to the file that was opened."""
    try:
        status = os.stat(file, follow_symlinks=False)
    except OSError:
        return False
    return (status.st_dev, status.st_ino) == (opened.st_dev, opened.st_ino)


def _identity(folder: Path) -> tuple[int, int] | None:
    """What a folder is to the file system (device, inode), or None when it cannot be looked at."""
    try:
        status = folder.stat()
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _checked_path(path: str, allowed: list[tuple[int, int] | None]) -> Path:
    """The real file behind `path`.

    Symbolic links are followed before the folders are checked, so a link inside an allowed folder cannot lead out of it
    (Pinned by: test_mcp_server.py::TestWhichFilesMayBeRead::test_symbolic_link_inside_a_root_cannot_lead_out_of_it).
    A folder is recognised by what it is, not by how its name is spelled: on macOS `~/documents` and `~/Documents` are one
    folder, and so are a Korean name typed composed and the same name as the file system lists it, decomposed. Compared as
    text, the paths an agent got from a directory listing were refused
    (Pinned by: TestWhichFilesMayBeRead::test_root_is_recognised_whatever_the_spelling_of_its_name).
    """
    if not path or "\x00" in path:
        raise ToolError("path must name a file.")
    try:
        file = Path(path).expanduser().resolve()
        if allowed and not any(identity in allowed for identity in map(_identity, file.parents) if identity):
            raise ToolError("The file is outside the folders this server may read.")
        exists = file.is_file()
    except (OSError, RuntimeError, ValueError) as error:   # a name too long for the system, a loop of links
        raise ToolError("No file at that path.") from error
    if not exists:
        raise ToolError("No file at that path.")
    return file


def _units(result: ConversionResult) -> list[Section]:
    return [section for section in result.sections if section.kind is not SectionKind.DOCUMENT]


def _one_line(text: str, limit: int = 300) -> str:
    """Text from the document (a title, a sheet name inside a warning) on one line of bounded length."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _numbers(indexes: list[int]) -> str:
    """`1-3, 5` for [1, 2, 3, 5]."""
    runs: list[list[int]] = []
    for index in indexes:
        if runs and index == runs[-1][1] + 1:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    shown = [str(first) if first == last else f"{first}-{last}" for first, last in runs[:20]]
    return ", ".join(shown) + (", …" if len(runs) > 20 else "")


def _parse_range(sections: str | int | None) -> tuple[int, int] | None:
    if isinstance(sections, int) and not isinstance(sections, bool):   # an agent passing the page number as a number
        sections = str(sections)
    if sections is None or not isinstance(sections, str) or not sections.strip():
        if sections is None or isinstance(sections, str):
            return None
        raise ToolError('sections must be a number or a range counted from 1, such as "3" or "2-5".')
    match = _RANGE.match(sections)
    if match is None:
        raise ToolError('sections must be a number or a range counted from 1, such as "3" or "2-5".')
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if first < 1 or last < first:
        raise ToolError('sections must be a number or a range counted from 1, such as "3" or "2-5".')
    return first, last


def _selected(result: ConversionResult, chosen: tuple[int, int], max_chars: int) -> tuple[str, str]:
    """The text of the chosen pages / slides / sheets and a note when something has to be said about the choice."""
    units = _units(result)
    if not units:
        if not result.text.strip():
            return "", ""
        if len(result.text) > max_chars:
            return result.text[:max_chars].rstrip(), ("The document is not divided into pages, slides or sheets, and it is longer "
                                                      "than max_chars: it was cut. Raise max_chars to read more.")
        return result.text, "The document is not divided into pages, slides or sheets; this is all of it."
    first, last = chosen
    picked = [section for section in units if first <= section.index <= last]
    if not picked:
        # The numbers that exist, not their count: a workbook whose first sheet is hidden has a sheet 2 and no sheet 1.
        return "", (f"Nothing is in the range asked for. The {_UNIT[units[0].kind].lower()}s of this document are numbered "
                    f"{_numbers([section.index for section in units])}.")
    text = "\n\n".join(_section_text(section) for section in picked)
    if len(text) > max_chars:
        return text[:max_chars].rstrip(), "The selection is longer than max_chars and was cut. Ask for fewer sections or raise max_chars."
    return text, ""


def _section_text(section: Section) -> str:
    heading = section_heading(section) or f"<!-- Page {section.index} -->"
    return f"{heading}\n\n{section.text}" if section.text else heading


def _outline_line(section: Section) -> str:
    title = f' "{_one_line(section.title, 120)}"' if section.title else ""
    return f"{_UNIT[section.kind]} {section.index}{title}: {len(section.text):,} characters"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opendockit-mcp", description="MCP server that converts local documents to Markdown.")
    parser.add_argument("--root", action="append", default=[], metavar="DIR",
                        help=f"only read files under this folder (repeatable; also the {ROOTS_VARIABLE} environment variable, "
                             "separated like PATH). Without it, any file the user running the server can read.")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, metavar="N",
                        help="default limit on the characters one call returns (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"opendockit-mcp {__version__}")
    args = parser.parse_args(argv)
    if MCPServer is None:
        parser.exit(1, _NEEDS_SDK + "\n")
    if args.max_chars < 1:
        parser.error("--max-chars must be at least 1")
    named = [root.strip() for root in args.root]
    if ROOTS_VARIABLE in os.environ:
        named += [part.strip() for part in os.environ[ROOTS_VARIABLE].split(os.pathsep)]
    # A root that is empty is a mistake, not "no restriction": `--root "$DOCS"` with DOCS unset must not open everything,
    # and must not quietly mean the current folder (Pinned by: test_mcp_server.py::TestCommandLine::test_empty_root_is_an_error).
    if any(not root for root in named):
        parser.error(f"--root and {ROOTS_VARIABLE} must name folders; an empty value was given")
    roots = [Path(root) for root in named]
    missing = [str(root) for root in roots if not root.expanduser().is_dir()]
    if missing:
        parser.error(f"not a folder: {', '.join(missing)}")
    create_server(roots, args.max_chars).run("stdio")


if __name__ == "__main__":
    main()
