"""The launcher package (`packages/opendockit-mcp`) and its MCP Registry entry name this package, at this version.

`opendockit-mcp` on PyPI holds no code. It exists so that `uvx opendockit-mcp` starts the server: registries and clients
build that command from a package name alone and cannot express "the `mcp` extra of `opendockit`, and a command with
another name". Everything in it is a statement about this package — the version, the pin, the entry point, the tools —
and nothing fails at run time when one of them goes stale. These tests are what notices.
"""

import asyncio
import json
from pathlib import Path
import re
import tomllib

import pytest

import opendockit
from opendockit import mcp_server


ROOT = Path(__file__).parent.parent
LAUNCHER = ROOT / "packages" / "opendockit-mcp"
pytestmark = pytest.mark.skipif(not LAUNCHER.is_dir(), reason="the launcher package is not part of the source distribution")

_MARKER = re.compile(r"mcp-name: (\S+)")
_TOOL = re.compile(r"\b((?:convert|document)_[a-z_]+)\b")


def _project(path: Path) -> dict:
    return tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _entry() -> dict:
    return json.loads((LAUNCHER / "server.json").read_text(encoding="utf-8"))


class TestPackage:
    def test_version_is_the_version_of_this_package_and_pins_it(self):
        """`uvx opendockit-mcp==X` must mean OpenDocKit X, so the two are released together."""
        launcher, package = _project(LAUNCHER), _project(ROOT)

        assert launcher["version"] == package["version"]
        assert launcher["dependencies"] == [f"opendockit[mcp]=={package['version']}"]
        assert launcher["requires-python"] == package["requires-python"]

    def test_command_is_the_server_of_this_package(self):
        launcher, package = _project(LAUNCHER), _project(ROOT)

        assert launcher["scripts"] == {"opendockit-mcp": package["scripts"]["opendockit-mcp"]}
        assert package["scripts"]["opendockit-mcp"] == "opendockit.mcp_server:main"

    def test_holds_no_code(self):
        """A module here would be a second copy of the server to keep in step."""
        files = sorted(path.name for path in LAUNCHER.iterdir() if path.is_file())

        assert files == ["LICENSE", "README.md", "pyproject.toml", "server.json"]
        assert not [path for path in LAUNCHER.iterdir() if path.is_dir() and path.name not in {"dist", "__pycache__"}]

    def test_license_is_the_license_of_this_package(self):
        assert (LAUNCHER / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()


class TestRegistryEntry:
    def test_names_the_launcher_at_this_version(self):
        entry = _entry()
        package, = entry["packages"]

        assert entry["version"] == package["version"] == _project(ROOT)["version"]
        assert (package["registryType"], package["identifier"]) == ("pypi", _project(LAUNCHER)["name"])
        assert package["transport"] == {"type": "stdio"}

    def test_readme_carries_the_ownership_marker_the_registry_looks_for(self):
        """The registry accepts the entry only when the description of the package on PyPI holds `mcp-name: <name>`, and
        the name must be in the namespace of the GitHub account that publishes it."""
        names = _MARKER.findall((LAUNCHER / "README.md").read_text(encoding="utf-8"))

        assert names == [_entry()["name"]]
        assert names[0].startswith("io.github.hyeonsang010716/")
        assert _project(LAUNCHER)["readme"] == "README.md"

    def test_description_and_title_fit_the_registry(self):
        entry = _entry()

        assert 1 <= len(entry["description"]) <= 100 and 1 <= len(entry["title"]) <= 100

    def test_arguments_and_variables_are_the_ones_the_server_reads(self):
        package, = _entry()["packages"]

        assert [argument["name"] for argument in package["packageArguments"]] == ["--root"]
        assert [variable["name"] for variable in package["environmentVariables"]] == [mcp_server.ROOTS_VARIABLE]
        assert not any(item.get("isRequired") for item in package["packageArguments"] + package["environmentVariables"])


class TestReadme:
    @pytest.mark.skipif(mcp_server.MCPServer is None, reason="needs the mcp extra")
    def test_names_the_tools_and_arguments_the_server_has(self):
        tools = {tool.name: tool for tool in asyncio.run(mcp_server.create_server().list_tools())}
        text = (LAUNCHER / "README.md").read_text(encoding="utf-8")

        assert set(_TOOL.findall(text)) == set(tools)
        assert all(argument in text for argument in tools["convert_document"].input_schema["properties"])
        assert str(mcp_server.DEFAULT_MAX_CHARS) in text and mcp_server.ROOTS_VARIABLE in text

    def test_version_of_the_checkout_is_what_the_server_reports(self):
        assert opendockit.__version__ == _project(ROOT)["version"]
