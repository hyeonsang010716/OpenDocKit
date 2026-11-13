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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re

from opendockit import __version__
from opendockit.converter import convert_file, section_heading
from opendockit.exceptions import ConversionError
from opendockit.isolated import IsolatedConverter
from opendockit.models import ConversionResult, ConvertOptions, Section, SectionKind


try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('The OpenDocKit MCP server needs the MCP SDK (2.0 or newer): pip install "opendockit[mcp]"') from exc


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
_UNIT = {SectionKind.PAGE: "Page", SectionKind.SLIDE: "Slide", SectionKind.SHEET: "Sheet", SectionKind.DOCUMENT: "Document"}


def create_server(roots: list[Path] | None = None, default_max_chars: int = DEFAULT_MAX_CHARS) -> MCPServer:
    """The server with its two tools. `roots`, when given, are the only folders files may be read from."""
    allowed = [root.expanduser().resolve() for root in roots or []]
    converter: IsolatedConverter | None = None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if converter is not None:
                await converter.aclose()

    server = MCPServer("opendockit", instructions=_INSTRUCTIONS, version=__version__, lifespan=lifespan)

    async def converted(path: str, limit: int | None) -> ConversionResult:
        nonlocal converter
        file = _checked_path(path, allowed)
        options = ConvertOptions(max_chars=limit)
        try:
            if os.name != "posix":
                # IsolatedConverter is verified on Linux and macOS only (docs/limitations.md): elsewhere the file is converted
                # in a thread of this process, without the worker's memory and time limits.
                return await asyncio.to_thread(convert_file, file, options)
            if file.stat().st_size > options.max_file_bytes:
                raise ToolError(f"The file is too large (maximum {options.max_file_bytes // (1024 * 1024)}MB).")
            if converter is None:
                # One worker process per file, ended after 2 minutes or 2 GB: a document that hangs or balloons must not take
                # the server, and the agent's session, with it.
                converter = IsolatedConverter(workers=1)
            return await converter.convert(await asyncio.to_thread(file.read_bytes), file.name, options)
        except ConversionError as error:
            raise ToolError(str(error)) from error

    @server.tool(structured_output=False)
    async def convert_document(path: str, sections: str | None = None, max_chars: int = default_max_chars) -> str:
        """Convert a local document to Markdown.

        path: the file to read (absolute, or relative to where the server was started).
        sections: which pages, slides or sheets to return, counted from 1: "3" or "2-5". Leave out for the whole document.
        max_chars: the most characters to return (default 100,000). When the text is longer it is cut there and a note says so.
        """
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
        warnings = [warning for warning in result.warnings if not warning.startswith("The conversion was stopped")]
        tail = [f"[{note}]"] if note else []
        if warnings:
            tail.append("Warnings:\n" + "\n".join(f"- {warning}" for warning in warnings))
        if not text.strip() and not tail:
            tail.append("[No text was extracted.]")
        return "\n\n".join(part for part in (text, *tail) if part)

    @server.tool(structured_output=False)
    async def document_outline(path: str) -> str:
        """Describe a local document without returning its text: format, title, size, and its pages, slides or sheets with
        their titles and lengths. Use it to decide which `sections` to ask convert_document for.

        path: the file to read (absolute, or relative to where the server was started).
        """
        result = await converted(path, None)
        lines = [f"Format: {result.format.value}", f"Characters: {len(result.text):,}"]
        lines += [f"{key.replace('_', ' ').capitalize()}: {value}" for key, value in result.metadata.items()]
        units = [section for section in result.sections if section.kind is not SectionKind.DOCUMENT]
        if units:
            lines.append("")
            lines += [_outline_line(section) for section in units]
        else:
            lines.append("The document is not divided into pages, slides or sheets; convert_document returns it whole.")
        if result.warnings:
            lines += ["", "Warnings:", *(f"- {warning}" for warning in result.warnings)]
        return "\n".join(lines)

    return server


def _checked_path(path: str, allowed: list[Path]) -> Path:
    """The real file behind `path`. Symbolic links are followed before the folders are checked, so a link inside an allowed
    folder cannot lead out of it
    (Pinned by: test_mcp_server.py::TestWhichFilesMayBeRead::test_symbolic_link_inside_a_root_cannot_lead_out_of_it)."""
    if not path or "\x00" in path:
        raise ToolError("path must name a file.")
    try:
        file = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise ToolError("path must name a file.") from error
    if allowed and not any(file.is_relative_to(root) for root in allowed):
        raise ToolError("The file is outside the folders this server may read.")
    if not file.is_file():
        raise ToolError("No file at that path.")
    return file


def _parse_range(sections: str | None) -> tuple[int, int] | None:
    if sections is None or not sections.strip():
        return None
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
    units = [section for section in result.sections if section.kind is not SectionKind.DOCUMENT]
    if not units:
        text = result.text
        note = "The document is not divided into pages, slides or sheets; this is all of it."
    else:
        first, last = chosen
        picked = [section for section in units if first <= section.index <= last]
        if not picked:
            return "", f"The document has {len(units)} {_UNIT[units[0].kind].lower()}(s); none is in the range asked for."
        text = "\n\n".join(_section_text(section) for section in picked)
        note = ""
    if len(text) > max_chars:
        return text[:max_chars].rstrip(), "The selection is longer than max_chars and was cut. Ask for fewer sections or raise max_chars."
    return text, note


def _section_text(section: Section) -> str:
    heading = section_heading(section) or f"<!-- Page {section.index} -->"
    return f"{heading}\n\n{section.text}" if section.text else heading


def _outline_line(section: Section) -> str:
    title = f' "{section.title}"' if section.title else ""
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
    if args.max_chars < 1:
        parser.error("--max-chars must be at least 1")
    from_environment = [part for part in os.environ.get(ROOTS_VARIABLE, "").split(os.pathsep) if part.strip()]
    roots = [Path(root) for root in (*args.root, *from_environment)]
    missing = [str(root) for root in roots if not root.expanduser().is_dir()]
    if missing:
        parser.error(f"not a folder: {', '.join(missing)}")
    create_server(roots, args.max_chars).run("stdio")


if __name__ == "__main__":
    main()
