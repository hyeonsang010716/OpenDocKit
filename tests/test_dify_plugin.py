"""The Dify plugin (`dify-plugin/`) is a package the Dify Marketplace accepts and that converts what this package converts.

The plugin runs only inside Dify, with the Dify SDK, so nothing in this repository runs it and nothing fails when it goes
stale: the pin moves in one file and not the other, a tool is declared that no file implements, a tag leaves the
Marketplace vocabulary. These tests are what notices. The conversion itself lives in a module without the SDK
(`tools/conversion.py`), so it is run here as Dify runs it.
"""

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest

import opendockit
from tests import builder


yaml = pytest.importorskip("yaml")

PLUGIN = Path(__file__).parent.parent / "dify-plugin"
pytestmark = pytest.mark.skipif(not PLUGIN.is_dir(), reason="the Dify plugin is not part of the source distribution")

# From the Marketplace curation policy (langgenius/dify-plugins, docs/marketplace-curation-policy.md): CI refuses any other tag.
_TAGS = {"agent", "business", "design", "education", "entertainment", "finance", "image", "news", "other", "productivity", "rag",
         "search", "social", "travel", "utilities", "videos", "weather", "llm", "model_provider", "audio", "document",
         "devtools", "database", "security", "translation"}
# What Dify itself accepts, though the Marketplace vocabulary above is larger ('document' is in neither list below):
# the tags of a tool provider (`ToolLabelEnum`, Dify 1.19), refused on upload, and the tags of the plugin
# (`manifest_entities.PluginTag`, dify-plugin-daemon 0.6.15), refused by `dify plugin package`.
_TOOL_LABELS = {"search", "image", "videos", "weather", "finance", "design", "travel", "social", "news", "medical",
                "productivity", "education", "business", "entertainment", "utilities", "rag", "other"}
_PLUGIN_TAGS = _TOOL_LABELS | {"agent", "trigger"}
_PIN = re.compile(r"([A-Za-z0-9_.-]+)==(\d+\.\d+\.\d+)")


def _yaml(name: str) -> dict:
    return yaml.safe_load((PLUGIN / name).read_text(encoding="utf-8"))


def _requirements() -> dict[str, str]:
    lines = [line.strip() for line in (PLUGIN / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    pins = [_PIN.fullmatch(line) for line in lines if line and not line.startswith("#")]
    assert all(pins), "every requirement is one exact version from the index: no ranges, URLs or git installs"
    return {match.group(1): match.group(2) for match in pins}


def _conversion():
    spec = importlib.util.spec_from_file_location("dify_plugin_conversion", PLUGIN / "tools" / "conversion.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Two conversions at once in one thread, the way the Dify plugin runtime serves requests (gevent, patched as dify_plugin
# patches on import). Prints how many of them finished, then leaves at once: under gevent, multiprocessing's exit handlers
# never return (the plugin runtime is never meant to exit, and nothing builds up from one conversion to the next).
_TWO_AT_ONCE = """
from gevent import monkey
monkey.patch_all(sys=True)
import importlib.util, os, sys
import gevent
spec = importlib.util.spec_from_file_location("conversion", sys.argv[1])
conversion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conversion)
data = open(sys.argv[2], "rb").read()
jobs = [gevent.spawn(conversion.convert_upload, data, "notice.hwpx") for _ in range(2)]
gevent.joinall(jobs)
print(sum(job.successful() for job in jobs), flush=True)
os._exit(0)
"""


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3])


class TestManifest:
    def test_names_and_fields_are_ones_the_marketplace_accepts(self):
        manifest = _yaml("manifest.yaml")

        assert manifest["type"] == "plugin"
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", manifest["author"])
        assert re.fullmatch(r"[a-z0-9_-]{1,128}", manifest["name"])
        assert manifest["label"]["en_US"] and manifest["description"]["en_US"]
        assert (PLUGIN / "_assets" / manifest["icon"]).is_file()
        assert manifest["repo"] == "https://github.com/hyeonsang010716/OpenDocKit"
        # Required by the Marketplace checks (dify-marketplace-toolkit, checks/manifest.py), which want an email address.
        assert re.fullmatch(r"[^@\s]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", manifest["contact"])

    def test_tags_come_from_the_marketplace_vocabulary(self):
        for tags in (_yaml("manifest.yaml")["tags"], _yaml("provider/opendockit.yaml")["identity"]["tags"]):
            assert 1 <= len(tags) <= 3 and set(tags) <= _TAGS

    def test_tags_are_ones_dify_packages_and_installs(self):
        """'document' in the provider made Dify 1.19 refuse the package on upload, and in the manifest made the official
        CLI refuse to build it."""
        assert set(_yaml("provider/opendockit.yaml")["identity"]["tags"]) <= _TOOL_LABELS
        assert set(_yaml("manifest.yaml")["tags"]) <= _PLUGIN_TAGS

    def test_privacy_policy_is_shipped(self):
        policy = PLUGIN / _yaml("manifest.yaml")["privacy"]

        assert policy.read_text(encoding="utf-8").strip()

    def test_version_is_the_version_of_the_package_it_installs(self):
        """One version for both: an update to the Marketplace is an update of OpenDocKit."""
        assert _yaml("manifest.yaml")["version"] == _requirements()["opendockit"]

    def test_pinned_release_is_not_newer_than_this_checkout(self):
        """A pin ahead of the code would name a release that does not exist yet."""
        assert _numbers(_requirements()["opendockit"]) <= _numbers(opendockit.__version__)

    def test_runner_is_a_python_this_package_supports(self):
        runner = _yaml("manifest.yaml")["meta"]["runner"]

        assert runner == {"language": "python", "version": "3.12", "entrypoint": "main"}
        assert (PLUGIN / "main.py").is_file()

    def test_installs_the_dify_sdk_and_opendockit_without_extras(self):
        """The default install is permissively licensed; an extra would bring in what it is not (heic) or a network
        client (openai)."""
        assert set(_requirements()) == {"dify_plugin", "opendockit"}


class TestDeclarations:
    def test_every_declared_file_exists(self):
        manifest = _yaml("manifest.yaml")
        assert manifest["plugins"] == {"tools": ["provider/opendockit.yaml"]}
        provider = _yaml("provider/opendockit.yaml")

        assert (PLUGIN / provider["extra"]["python"]["source"]).is_file()
        for tool in provider["tools"]:
            assert (PLUGIN / _yaml(tool)["extra"]["python"]["source"]).is_file()

    def test_the_tool_reads_the_one_parameter_it_declares(self):
        tool = _yaml("tools/convert_document.yaml")
        code = (PLUGIN / tool["extra"]["python"]["source"]).read_text(encoding="utf-8")

        assert [parameter["name"] for parameter in tool["parameters"]] == ["file"]
        assert tool["parameters"][0]["type"] == "file" and tool["parameters"][0]["required"] is True
        assert re.findall(r'tool_parameters\["(\w+)"\]', code) == ["file"]

    def test_the_variables_it_sends_are_the_ones_its_schema_names(self):
        tool = _yaml("tools/convert_document.yaml")
        code = (PLUGIN / tool["extra"]["python"]["source"]).read_text(encoding="utf-8")

        assert set(re.findall(r'create_variable_message\("(\w+)"', code)) == set(tool["output_schema"]["properties"])

    def test_the_plugin_is_made_only_when_main_is_run(self):
        """Each conversion runs in a worker that imports the main module again; a plugin made at import time would start
        there too."""
        lines = (PLUGIN / "main.py").read_text(encoding="utf-8").splitlines()
        made = next(index for index, line in enumerate(lines) if "Plugin(" in line and not line.lstrip().startswith("#"))

        assert 'if __name__ == "__main__":' in lines[:made] and lines[made].startswith("    ")


class TestWhatIsShipped:
    def test_ignore_file_keeps_local_state_out_of_the_package(self):
        ignored = (PLUGIN / ".difyignore").read_text(encoding="utf-8").split()

        assert {".venv/", "__pycache__/", ".env", "*.difypkg", ".DS_Store"} <= set(ignored)

    def test_documents_are_readable_text(self):
        for name in ("README.md", "PRIVACY.md", "LICENSE"):
            assert (PLUGIN / name).read_text(encoding="utf-8").strip()


@pytest.mark.timing  # each conversion starts a worker process
class TestConversion:
    def test_an_hwpx_upload_gives_its_markdown_with_merged_cells_repeated(self):
        converted = _conversion().convert_upload(builder.hwpx_bytes(), "notice.hwpx")

        assert "| 일정 | 3월 |" in converted.text and "| 일정 | 4월 |" in converted.text
        assert len(converted.documents) == 1
        assert converted.documents[0]["page_content"] == converted.text
        assert converted.documents[0]["metadata"]["source"] == "notice.hwpx"

    def test_a_pdf_gives_one_document_per_page(self):
        converted = _conversion().convert_upload(builder.pdf_bytes(pages=3), "report.pdf")

        assert [document["metadata"]["section_index"] for document in converted.documents] == [1, 2, 3]
        assert {document["metadata"]["section_kind"] for document in converted.documents} == {"page"}

    def test_a_file_without_text_fails_with_the_reason_said_once(self):
        """A scanned page: the error says so, and does not repeat the library's own 'No text was extracted.' after it."""
        conversion = _conversion()

        with pytest.raises(conversion.NoTextError) as raised:
            conversion.convert_upload(builder.png_bytes(), "scan.png")
        message = str(raised.value)
        assert message.startswith(conversion.NO_TEXT) and message.count("No text") == 1

    def test_two_requests_at_once_in_one_thread_both_finish(self, tmp_path):
        """Dify serves a plugin's requests side by side in one thread; without taking turns the second conversion failed
        with "asyncio.run() cannot be called from a running event loop". Runs where gevent is installed (the Dify SDK
        brings it)."""
        pytest.importorskip("gevent")
        document = tmp_path / "notice.hwpx"
        document.write_bytes(builder.hwpx_bytes())

        finished = subprocess.run([sys.executable, "-c", _TWO_AT_ONCE, str(PLUGIN / "tools" / "conversion.py"), str(document)],
                                  capture_output=True, text=True, timeout=120, check=True)

        assert finished.stdout.split()[-1] == "2"

    def test_a_runtime_that_cannot_start_a_worker_still_converts(self, monkeypatch):
        """Some serverless runtimes cannot start a process (no shared memory for its semaphores)."""
        conversion = _conversion()

        class NoProcesses:
            def __init__(self, **_):
                raise OSError(38, "Function not implemented")

        monkeypatch.setattr(conversion, "IsolatedConverter", NoProcesses)
        converted = conversion.convert_upload(builder.hwpx_bytes(), "notice.hwpx")

        assert "| 일정 | 3월 |" in converted.text

    def test_a_broken_file_fails_with_the_library_error(self):
        with pytest.raises(opendockit.ConversionError):
            _conversion().convert_upload(b"PK\x03\x04 not really a zip", "broken.docx")
