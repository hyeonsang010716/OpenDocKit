"""The Dify plugin (`dify-plugin/`) is a package the Dify Marketplace accepts and that converts what this package converts.

The plugin runs only inside Dify, with the Dify SDK, so nothing in this repository runs it and nothing fails when it goes
stale: the pin moves in one file and not the other, a tool is declared that no file implements, a tag leaves the
Marketplace vocabulary. These tests are what notices. The conversion itself lives in a module without the SDK
(`tools/conversion.py`), so it is run here as Dify runs it.
"""

import ast
import importlib.util
from io import BytesIO
import logging
import os
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
    # No __pycache__ in dify-plugin/: the Marketplace's content check refuses one in a plugin folder.
    written, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


def _hidden_sheet_only_workbook(name: str) -> bytes:
    """A workbook whose one visible sheet is empty and whose hidden sheet, named `name`, holds the data."""
    from openpyxl import Workbook

    workbook = Workbook()
    hidden = workbook.create_sheet(name)
    hidden["A1"] = "salary"
    hidden.sheet_state = "hidden"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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

    def test_the_sdk_is_imported_and_the_plugin_made_only_when_main_is_run(self):
        """Each conversion runs in a worker that imports the main module again. A plugin made at import time would start
        there too, and the SDK's import patches the worker for gevent, which kept the worker alive after the plugin was
        killed."""
        module = ast.parse((PLUGIN / "main.py").read_text(encoding="utf-8"))
        [guard] = module.body
        guarded = ast.unparse(guard)

        assert isinstance(guard, ast.If) and ast.unparse(guard.test) == "__name__ == '__main__'"
        assert "from dify_plugin import" in guarded and "Plugin(" in guarded

    def test_it_keeps_the_sdk_s_request_timeout(self):
        """`MAX_REQUEST_TIMEOUT` is read by the serverless runtime only: set here it limited nothing on self-hosted Dify
        and cut Dify Cloud's default."""
        assert "MAX_REQUEST_TIMEOUT" not in (PLUGIN / "main.py").read_text(encoding="utf-8")


class TestUploadName:
    @pytest.mark.parametrize(("filename", "extension", "name"), [
        ("notice.hwpx", ".hwpx", "notice.hwpx"),
        ("Report.CSV", ".csv", "Report.CSV"),
        ("download", ".csv", "download.csv"),
        ("download", "csv", "download.csv"),
        (None, ".pdf", "document.pdf"),
        (None, None, "document"),
        ("notes.txt", None, "notes.txt"),
    ])
    def test_a_name_gets_the_extension_dify_recorded(self, filename, extension, name):
        """CSV is told from plain text by its extension only: a remote file named 'download' must still read as a table."""
        assert _conversion().upload_name(filename, extension) == name


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

    def test_a_file_without_text_fails_without_quoting_the_document(self, caplog):
        """The library's warnings can name a part of the document (here a hidden sheet). The error is a fixed sentence,
        and nothing of the document reaches the log."""
        conversion = _conversion()
        data = _hidden_sheet_only_workbook("Salary Kim Minsu")
        assert any("Salary Kim Minsu" in warning for warning in opendockit.convert(data, "pay.xlsx").warnings)

        with caplog.at_level(logging.DEBUG), pytest.raises(conversion.NoTextError) as raised:
            conversion.convert_upload(data, "pay.xlsx")

        assert str(raised.value) == conversion.NO_TEXT
        assert "Salary" not in caplog.text

    def test_a_picture_keeps_its_alt_text(self):
        """No picture is decoded, but the alt text an author gave one stays: with `extract_images=False` a DOCX picture
        described as a revenue chart came out of Dify as nothing."""
        from docx import Document
        from docx.shared import Inches

        document = Document()
        document.add_paragraph("Revenue grew in Q3.")
        document.add_picture(BytesIO(builder.png_bytes(300, 200, seed=1)), width=Inches(2))
        document.inline_shapes[-1]._inline.docPr.set("descr", "Quarterly revenue chart: Q3 up 12%")
        buffer = BytesIO()
        document.save(buffer)

        converted = _conversion().convert_upload(buffer.getvalue(), "report.docx")

        assert converted.text == "Revenue grew in Q3.\n\n[Image: Quarterly revenue chart: Q3 up 12%]"

    def test_a_normal_file_is_converted_in_a_worker(self, monkeypatch):
        """The conversion in the plugin process is for runtimes without worker pools only."""
        conversion = _conversion()

        def here(*_):
            raise AssertionError("converted in the plugin process")

        monkeypatch.setattr(conversion, "convert", here)

        assert "| 일정 | 3월 |" in conversion.convert_upload(builder.hwpx_bytes(), "notice.hwpx").text

    def test_two_requests_at_once_in_one_thread_both_finish(self, tmp_path):
        """Dify serves a plugin's requests side by side in one thread; without taking turns the second conversion failed
        with "asyncio.run() cannot be called from a running event loop"."""
        pytest.importorskip("gevent")
        if os.name != "posix":
            pytest.skip("the plugin runtime is Linux; IsolatedConverter is verified on Linux and macOS only")
        # Run on the Python the plugin runs on: on 3.14 gevent with asyncio and multiprocessing hangs, old code or new.
        if f"{sys.version_info.major}.{sys.version_info.minor}" != _yaml("manifest.yaml")["meta"]["runner"]["version"]:
            pytest.skip("runs on the Python version of the plugin's runner")
        document = tmp_path / "notice.hwpx"
        document.write_bytes(builder.hwpx_bytes())

        finished = subprocess.run([sys.executable, "-B", "-c", _TWO_AT_ONCE, str(PLUGIN / "tools" / "conversion.py"), str(document)],
                                  capture_output=True, text=True, timeout=120, check=True)

        assert finished.stdout.split()[-1] == "2"

    def test_a_runtime_that_cannot_make_a_worker_pool_still_converts(self, monkeypatch):
        """Some serverless runtimes cannot make a process pool at all (no shared memory for its semaphores)."""
        conversion = _conversion()

        class NoPools:
            def __init__(self, **_):
                raise OSError(38, "Function not implemented")

        monkeypatch.setattr(conversion, "IsolatedConverter", NoPools)
        converted = conversion.convert_upload(builder.hwpx_bytes(), "notice.hwpx")

        assert "| 일정 | 3월 |" in converted.text

    def test_a_worker_that_cannot_start_fails_the_file_instead_of_converting_it_here(self, monkeypatch):
        """A worker that cannot start is a machine short of memory or processes: converting the file in the plugin process
        then, without limits, would put every plugin of the Dify at risk."""
        conversion = _conversion()

        class ShortOfMemory:
            def __init__(self, **_):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def convert(self, *_):
                raise OSError(12, "Cannot allocate memory")

        def here(*_):
            raise AssertionError("converted in the plugin process")

        monkeypatch.setattr(conversion, "IsolatedConverter", ShortOfMemory)
        monkeypatch.setattr(conversion, "convert", here)

        with pytest.raises(OSError, match="Cannot allocate memory"):
            conversion.convert_upload(builder.hwpx_bytes(), "notice.hwpx")

    def test_a_broken_file_fails_with_the_library_error(self):
        with pytest.raises(opendockit.ConversionError):
            _conversion().convert_upload(b"PK\x03\x04 not really a zip", "broken.docx")
