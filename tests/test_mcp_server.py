"""The MCP server (`opendockit-mcp`): its two tools, the limits on what a call returns, and which files it may read."""

import asyncio
import os
from pathlib import Path
import sys

import pytest

from tests import builder


mcp_server = pytest.importorskip("opendockit.mcp_server", reason="needs the mcp extra")
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402 - only importable with the extra


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    documents = {"book.xlsx": builder.xlsx_bytes(), "paper.pdf": builder.pdf_bytes(pages=3), "report.docx": builder.docx_bytes()}
    for name, data in documents.items():
        (tmp_path / name).write_bytes(data)
    return {name: tmp_path / name for name in documents}


def call(tool: str, arguments: dict, **server_options) -> str:
    """One tool call against a fresh server, the way a client makes it; the server is shut down afterwards (which ends its
    worker process)."""
    async def run() -> tuple[bool, str]:
        from mcp.client.client import Client

        async with Client(mcp_server.create_server(**server_options)) as client:
            result = await client.call_tool(tool, arguments)
            return result.is_error, result.content[0].text

    failed, text = asyncio.run(run())
    if failed:
        raise ToolError(text)
    return text


class TestConvertDocument:
    def test_returns_the_markdown_of_the_file(self, files):
        text = call("convert_document", {"path": str(files["report.docx"])})

        assert text.startswith("# 1장 개요")

    def test_sections_selects_pages_by_number_or_range(self, files):
        """A three-page PDF: "2" gives page 2 only, "2-3" pages 2 and 3, each under its page marker; a range past the end says
        how many pages there are instead of returning nothing silently."""
        second = call("convert_document", {"path": str(files["paper.pdf"]), "sections": "2"})
        last_two = call("convert_document", {"path": str(files["paper.pdf"]), "sections": " 2 - 3 "})
        beyond = call("convert_document", {"path": str(files["paper.pdf"]), "sections": "9"})

        assert second.startswith("<!-- Page 2 -->") and "<!-- Page 1 -->" not in second and "<!-- Page 3 -->" not in second
        assert last_two.startswith("<!-- Page 2 -->") and "<!-- Page 3 -->" in last_two
        assert beyond == "[The document has 3 page(s); none is in the range asked for.]"

    def test_long_document_is_cut_at_max_chars_with_a_note_saying_how_to_read_the_rest(self, files):
        """An agent's context is the scarce thing. The text is cut at `max_chars` and the note tells the agent what to do next;
        the converter's own 'stopped' warning is not repeated under it."""
        text = call("convert_document", {"path": str(files["paper.pdf"]), "max_chars": 60})
        body, note = text.split("\n\n[", 1)

        assert len(body) <= 60
        assert note.startswith("The document is longer than max_chars and was cut. Call document_outline")
        assert "The conversion was stopped" not in text

    def test_warnings_of_the_converter_are_listed_after_the_text(self, files):
        text = call("convert_document", {"path": str(files["book.xlsx"])})

        assert "## Sheet: " in text
        assert text.count("Warnings:\n- ") <= 1   # the builder's workbook may or may not raise one; never more than one block

    @pytest.mark.parametrize("sections", ["0", "3-2", "two", "1,2", "-1", "1-"])
    def test_malformed_sections_are_refused_with_one_fixed_message(self, files, sections):
        with pytest.raises(ToolError, match='such as "3" or "2-5"'):
            call("convert_document", {"path": str(files["paper.pdf"]), "sections": sections})

    def test_file_that_cannot_be_converted_reports_the_converter_message(self, tmp_path):
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"PK\x03\x04 not a real package")
        with pytest.raises(ToolError, match="corrupted|Could not"):
            call("convert_document", {"path": str(broken)})


class TestDocumentOutline:
    def test_lists_format_size_and_units_without_the_text(self, files):
        outline = call("document_outline", {"path": str(files["paper.pdf"])})

        assert outline.startswith("Format: pdf\nCharacters: ")
        assert "Page count: 3" in outline
        assert [line.split(":")[0] for line in outline.split("\n") if line.startswith("Page ") and line[5].isdigit()] == ["Page 1", "Page 2", "Page 3"]

    def test_sheets_are_listed_with_their_names(self, files):
        outline = call("document_outline", {"path": str(files["book.xlsx"])})

        assert 'Sheet 1 "' in outline and "characters" in outline

    def test_document_without_units_says_so(self, files):
        outline = call("document_outline", {"path": str(files["report.docx"])})

        assert "not divided into pages, slides or sheets" in outline


class TestWhichFilesMayBeRead:
    def test_missing_file_and_folder_are_refused(self, tmp_path):
        for path in (tmp_path / "nothing.pdf", tmp_path):
            with pytest.raises(ToolError, match="No file at that path"):
                call("document_outline", {"path": str(path)})

    def test_with_roots_only_files_under_them_are_read(self, files, tmp_path_factory):
        """The server was started with `--root inbox`. A file in another folder is refused — also when reached with `..`."""
        inbox = files["report.docx"].parent
        elsewhere = tmp_path_factory.mktemp("elsewhere") / "secret.docx"
        elsewhere.write_bytes(builder.docx_bytes())

        assert call("document_outline", {"path": str(files["report.docx"])}, roots=[inbox]).startswith("Format: docx")
        for path in (str(elsewhere), str(inbox / ".." / elsewhere.parent.name / "secret.docx")):
            with pytest.raises(ToolError, match="outside the folders this server may read"):
                call("document_outline", {"path": path}, roots=[inbox])

    @pytest.mark.skipif(os.name != "posix", reason="creating a symbolic link needs no privilege only on POSIX")
    def test_symbolic_link_inside_a_root_cannot_lead_out_of_it(self, files, tmp_path_factory):
        """A link placed in the allowed folder that points at a file outside it: the real file decides."""
        inbox = files["report.docx"].parent
        outside = tmp_path_factory.mktemp("outside") / "secret.docx"
        outside.write_bytes(builder.docx_bytes())
        (inbox / "link.docx").symlink_to(outside)

        with pytest.raises(ToolError, match="outside the folders this server may read"):
            call("convert_document", {"path": str(inbox / "link.docx")}, roots=[inbox])

    def test_without_roots_any_readable_file_is_read(self, files):
        assert call("document_outline", {"path": str(files["report.docx"])}).startswith("Format: docx")


class TestCommandLine:
    def test_root_that_is_not_a_folder_is_an_error_before_the_server_starts(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            mcp_server.main(["--root", str(tmp_path / "missing")])
        assert "not a folder" in capsys.readouterr().err

    def test_tools_are_described_for_the_agent(self):
        """What the agent sees when it lists the tools: both tools, with every argument documented in the description."""
        tools = {tool.name: tool for tool in asyncio.run(mcp_server.create_server().list_tools())}

        assert set(tools) == {"convert_document", "document_outline"}
        assert set(tools["convert_document"].input_schema["properties"]) == {"path", "sections", "max_chars"}
        assert all(word in tools["convert_document"].description for word in ("path:", "sections:", "max_chars:"))

    @pytest.mark.timing
    def test_server_speaks_mcp_over_stdio(self, files):
        """The real thing: the server started as a separate process, a client talking to it over stdin / stdout. Nothing the
        conversion does (a worker process, a library's output) may write to stdout, which carries the protocol."""
        async def run() -> tuple[list[str], str]:
            from mcp.client.client import Client
            from mcp.client.stdio import StdioServerParameters, stdio_client

            server = StdioServerParameters(command=sys.executable, args=["-m", "opendockit.mcp_server", "--root", str(files["paper.pdf"].parent)])
            async with Client(stdio_client(server), raise_exceptions=True) as client:
                names = [tool.name for tool in (await client.list_tools()).tools]
                first = await client.call_tool("convert_document", {"path": str(files["paper.pdf"]), "sections": "1"})
                second = await client.call_tool("document_outline", {"path": str(files["book.xlsx"])})
                return names, first.content[0].text + "\n" + second.content[0].text

        names, text = asyncio.run(run())

        assert sorted(names) == ["convert_document", "document_outline"]
        assert text.startswith("<!-- Page 1 -->") and "Format: xlsx" in text
