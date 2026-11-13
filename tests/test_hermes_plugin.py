"""The Hermes Agent plugin (`hermes-plugin/`) describes the server this package really ships.

The plugin is data: a manifest, a server declaration and a skill, read by Hermes at the commit its catalog pins. Nothing
here runs when the library is used, so nothing fails when it goes stale: the skill names a tool that was renamed, or the
pin moves in one file and not in the others. These tests are what notices.
"""

import asyncio
import json
from pathlib import Path
import re

import pytest

import opendockit
from opendockit import mcp_server


PLUGIN = Path(__file__).parent.parent / "hermes-plugin"
pytestmark = pytest.mark.skipif(not PLUGIN.is_dir(), reason="the Hermes plugin is not part of the source distribution")

# Agent Plugins v1.0.0, as Hermes checks it when a package is loaded.
_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_PLUGIN_FIELDS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords",
                  "extensions"}
_PLUGIN_NAME = re.compile(r"(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
_SKILL_NAME = re.compile(r"(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*")
_PIN = re.compile(r"opendockit\[mcp\]==(\d+\.\d+\.\d+)")
_TOOL = re.compile(r"\b(?:mcp__opendockit__)?((?:convert|document)_[a-z_]+)\b")


def _json(name: str) -> dict:
    return json.loads((PLUGIN / name).read_text(encoding="utf-8"))


def _server() -> dict:
    return _json("mcp.json")["mcpServers"]["opendockit"]


def _pinned_version() -> str:
    pins = [match.group(1) for argument in _server()["args"] if (match := _PIN.fullmatch(argument))]
    assert len(pins) == 1, "mcp.json must install exactly one pinned opendockit"
    return pins[0]


def _frontmatter(skill: Path) -> dict[str, str]:
    """The top-level `key: value` lines of the YAML header (the nested `metadata` block is skipped)."""
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    header = text[4:text.index("\n---\n", 4)]
    pairs = (line.split(":", 1) for line in header.splitlines() if line and not line.startswith(" "))
    return {key: value.strip().strip('"') for key, value in pairs}


class TestManifest:
    def test_is_an_agent_plugins_v1_manifest_hermes_accepts(self):
        manifest = _json("plugin.json")

        assert manifest["$schema"] == _PLUGIN_SCHEMA
        assert set(manifest) <= _PLUGIN_FIELDS
        assert _PLUGIN_NAME.fullmatch(manifest["name"]) and len(manifest["name"]) <= 64
        assert set(manifest["author"]) <= {"name", "email", "url"}
        assert all(isinstance(keyword, str) for keyword in manifest["keywords"])
        assert manifest["license"] == "MIT" and (PLUGIN / "LICENSE").is_file()

    def test_version_is_the_version_of_the_package_the_server_is_started_from(self):
        assert _json("plugin.json")["version"] == _pinned_version()


class TestServerDeclaration:
    def test_starts_the_console_script_of_one_exact_release_without_a_shell(self):
        declaration = _json("mcp.json")
        server = _server()

        assert declaration["$schema"] == _MCP_SCHEMA
        assert set(server) <= {"type", "command", "args", "env", "cwd"} and server["type"] == "stdio"
        assert server["command"] == "uvx"  # a bare executable: Hermes refuses a path or a command line here
        assert server["args"][-1] == "opendockit-mcp"
        assert not any("latest" in argument for argument in server["args"])

    def test_dependencies_are_resolved_as_of_one_date(self):
        """The requirements of the package are floors without a ceiling. Without a date the same pinned commit would
        install different code next month, and a new major release of a dependency could break it."""
        arguments = _server()["args"]
        date = arguments[arguments.index("--exclude-newer") + 1]

        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", date)
        assert arguments.index("--exclude-newer") < arguments.index("--from")

    def test_pinned_release_is_not_newer_than_this_checkout(self):
        """A pin ahead of the code would name a release that does not exist yet."""
        def numbers(version: str) -> tuple[int, ...]:
            return tuple(int(part) for part in version.split(".")[:3])

        assert numbers(_pinned_version()) <= numbers(opendockit.__version__)

    def test_forwards_the_roots_variable_and_nothing_else(self):
        """Hermes hands a stdio server a filtered environment; `"NAME": "${NAME}"` lets the user's value through and is
        dropped when the variable is unset, which matters because an empty root stops the server."""
        name = mcp_server.ROOTS_VARIABLE

        assert _server()["env"] == {name: "${" + name + "}"}

    def test_no_root_is_passed_on_the_command_line(self):
        """A root in `args` would be the same for every user of the catalog."""
        assert "--root" not in _server()["args"]


class TestSkill:
    def test_header_satisfies_the_agent_skills_rules(self):
        skills = sorted(path for path in (PLUGIN / "skills").iterdir() if path.is_dir())
        assert [path.name for path in skills] == ["opendockit"]
        header = _frontmatter(skills[0] / "SKILL.md")

        assert header["name"] == skills[0].name and _SKILL_NAME.fullmatch(header["name"])
        assert 1 <= len(header["description"]) <= 1024
        assert header["version"] == _pinned_version()

    @pytest.mark.skipif(mcp_server.MCPServer is None, reason="needs the mcp extra")
    @pytest.mark.parametrize("page", ["skills/opendockit/SKILL.md", "README.md"])
    def test_names_the_tools_and_arguments_the_server_has(self, page):
        tools = {tool.name: tool for tool in asyncio.run(mcp_server.create_server().list_tools())}
        text = (PLUGIN / page).read_text(encoding="utf-8")

        assert set(_TOOL.findall(text)) == set(tools)
        for argument in ("sections", "max_chars"):
            assert argument in text and argument in tools["convert_document"].input_schema["properties"]
        assert str(mcp_server.DEFAULT_MAX_CHARS) in text

    @pytest.mark.parametrize("page", ["skills/opendockit/SKILL.md", "README.md"])
    def test_every_command_that_starts_the_server_names_the_pinned_release(self, page):
        text = (PLUGIN / page).read_text(encoding="utf-8")
        named = re.findall(r"opendockit\[mcp\](?:==([\d.]+))?", text)

        assert named and set(named) == {_pinned_version()}


class TestWhatIsShipped:
    def test_package_holds_only_readable_text(self):
        """The catalog refuses compiled files and anything its reviewers cannot read."""
        files = sorted(path.relative_to(PLUGIN).as_posix() for path in PLUGIN.rglob("*") if path.is_file())

        assert files == ["LICENSE", "README.md", "mcp.json", "plugin.json", "skills/opendockit/SKILL.md"]
        for name in files:
            (PLUGIN / name).read_text(encoding="utf-8")
