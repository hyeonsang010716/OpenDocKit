"""The Claude Code plugin (`claude-plugin/`) and the marketplace that lists it (`.claude-plugin/marketplace.json`) describe
the server this package really ships.

Like the Hermes plugin, it is data that Claude Code reads from this repository: a manifest, a server declaration and a
skill. Nothing here runs when the library is used, so only these tests notice when it goes stale.
"""

import asyncio
import json
from pathlib import Path
import re

import pytest

import opendockit
from opendockit import mcp_server


ROOT = Path(__file__).parent.parent
PLUGIN = ROOT / "claude-plugin"
pytestmark = pytest.mark.skipif(not PLUGIN.is_dir(), reason="the claude-plugin folder is not in this copy of the project")

# The rules `claude plugin validate` applies (plugins/manifest-reference and plugins/marketplace-reference)
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-", "claudeai-")
_PIN = re.compile(r"opendockit\[mcp\]==(\d+\.\d+\.\d+)")
# Claude Code names a plugin's MCP tool `mcp__plugin_<plugin>_<server>__<tool>`
_TOOL = re.compile(r"\b(?:mcp__plugin_opendockit_opendockit__)?((?:convert|document)_[a-z_]+)\b")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _server() -> dict:
    return _json(PLUGIN / ".mcp.json")["mcpServers"]["opendockit"]


def _pinned_version() -> str:
    pins = [match.group(1) for argument in _server()["args"] if (match := _PIN.fullmatch(argument))]
    assert len(pins) == 1, ".mcp.json must install exactly one pinned opendockit"
    return pins[0]


def _frontmatter(skill: Path) -> dict[str, str]:
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    header = text[4:text.index("\n---\n", 4)]
    pairs = (line.split(":", 1) for line in header.splitlines() if line and not line.startswith(" "))
    return {key: value.strip().strip('"') for key, value in pairs}


class TestManifest:
    def test_name_and_fields_are_ones_claude_code_accepts(self):
        manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")

        assert _NAME.fullmatch(manifest["name"]) and not manifest["name"].startswith(_RESERVED_PREFIXES)
        assert set(manifest["author"]) <= {"name", "email", "url"} and manifest["author"]["name"]
        assert manifest["license"] == "MIT" and (PLUGIN / "LICENSE").is_file()

    def test_version_is_the_version_of_the_package_the_server_is_started_from(self):
        assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == _pinned_version()


class TestMarketplace:
    def test_lists_this_plugin_from_its_folder(self):
        """`/plugin install opendockit --marketplace hyeonsang010716/OpenDocKit` reads this file at the top of the repository;
        a relative source starts with './' and is taken from the folder that holds `.claude-plugin/`."""
        marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")
        [entry] = marketplace["plugins"]

        assert _NAME.fullmatch(marketplace["name"]) and not marketplace["name"].startswith(_RESERVED_PREFIXES)
        assert marketplace["owner"]["name"] and marketplace["description"]
        assert entry["source"] == "./claude-plugin" and ".." not in entry["source"]
        assert entry["name"] == _json(PLUGIN / ".claude-plugin" / "plugin.json")["name"]


class TestServerDeclaration:
    def test_starts_the_console_script_of_one_exact_release(self):
        server = _server()

        assert set(server) <= {"type", "command", "args"}
        assert server["command"] == "uvx" and server["args"][-1] == "opendockit-mcp"
        arguments = server["args"]
        date = arguments[arguments.index("--exclude-newer") + 1]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", date)
        assert arguments.index("--exclude-newer") < arguments.index("--from")

    def test_passes_no_environment_and_no_root(self):
        """The server inherits Claude Code's environment, so `OPENDOCKIT_MCP_ROOTS` exported in the shell reaches it. Written
        as `"${OPENDOCKIT_MCP_ROOTS}"` in `env`, an unset variable is passed as that literal text, which the server takes
        for a folder that does not exist and stops; `${…:-}` gives an empty value, which stops it as well. A `--root` in
        `args` would be the same folder for every user."""
        assert "env" not in _server()
        assert "--root" not in _server()["args"]

    def test_starts_the_same_release_as_the_hermes_plugin(self):
        """Both plugins pin one release, resolved as of one date, and move together."""
        hermes = _json(ROOT / "hermes-plugin" / "mcp.json")["mcpServers"]["opendockit"]["args"]

        assert _server()["args"] == hermes

    def test_pinned_release_is_not_newer_than_this_checkout(self):
        def numbers(version: str) -> tuple[int, ...]:
            return tuple(int(part) for part in version.split(".")[:3])

        assert numbers(_pinned_version()) <= numbers(opendockit.__version__)


class TestSkill:
    def test_header_is_a_skill_claude_code_loads(self):
        skills = sorted(path for path in (PLUGIN / "skills").iterdir() if path.is_dir())
        assert [path.name for path in skills] == ["opendockit"]
        header = _frontmatter(skills[0] / "SKILL.md")

        assert header["name"] == skills[0].name
        assert 1 <= len(header["description"]) <= 1024

    @pytest.mark.skipif(mcp_server.MCPServer is None, reason="needs the mcp extra")
    @pytest.mark.parametrize("page", ["skills/opendockit/SKILL.md", "README.md"])
    def test_names_the_tools_and_arguments_the_server_has(self, page):
        tools = {tool.name: tool for tool in asyncio.run(mcp_server.create_server().list_tools())}
        text = (PLUGIN / page).read_text(encoding="utf-8")

        assert set(_TOOL.findall(text)) == set(tools)
        assert all(f"mcp__plugin_opendockit_opendockit__{name}" in text for name in tools)
        for argument in ("sections", "max_chars"):
            assert argument in text and argument in tools["convert_document"].input_schema["properties"]
        assert str(mcp_server.DEFAULT_MAX_CHARS) in text

    @pytest.mark.parametrize("page", ["skills/opendockit/SKILL.md", "README.md"])
    def test_every_command_that_starts_the_server_names_the_pinned_release(self, page):
        named = re.findall(r"opendockit\[mcp\](?:==([\d.]+))?", (PLUGIN / page).read_text(encoding="utf-8"))

        assert named and set(named) == {_pinned_version()}


class TestWhatIsShipped:
    def test_plugin_holds_only_readable_text(self):
        files = sorted(path.relative_to(PLUGIN).as_posix() for path in PLUGIN.rglob("*") if path.is_file())

        assert files == [".claude-plugin/icon.png", ".claude-plugin/plugin.json", ".mcp.json", "LICENSE", "PRIVACY.md", "README.md",
                         "skills/opendockit/SKILL.md"]
        for name in files:
            if not name.endswith(".png"):
                (PLUGIN / name).read_text(encoding="utf-8")

    def test_icon_is_one_the_plugin_directory_takes(self):
        """The directory takes the listing icon once, at the first submission: a square PNG of 512 to 2048 px under 2 MB."""
        from PIL import Image

        icon = PLUGIN / ".claude-plugin" / "icon.png"
        with Image.open(icon) as image:
            assert image.format == "PNG" and image.width == image.height and 512 <= image.width <= 2048
        assert icon.stat().st_size < 2 * 1024 * 1024
