"""The LightRAG parser engine (`opendockit.integrations.lightrag`), run against LightRAG's own registry and contract."""

import asyncio
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from opendockit import CorruptedDocumentError, convert_file
from tests import builder
from tests.test_integrations import _scanned_pdf


try:   # the adapter raises ImportError with the install command; `importorskip` skips only on ModuleNotFoundError
    from opendockit.integrations import lightrag as adapter
except ImportError:
    pytest.skip("needs the lightrag extra", allow_module_level=True)

from lightrag.constants import FULL_DOCS_FORMAT_RAW
from lightrag.parser import plugins, registry, routing
from lightrag.parser.base import ParseContext
import lightrag.pipeline as pipeline


class _Rag:
    """What a parser asks of LightRAG: where the source is, and to store the text."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, dict]] = []

    async def _persist_parsed_full_docs(self, doc_id: str, payload: dict) -> None:
        self.stored.append((doc_id, payload))

    def _resolve_source_file_for_parser(self, file_path: str, *, source_file=None, parser_engine=None) -> str:
        return file_path


@pytest.fixture
def archived(monkeypatch) -> list[str]:
    """The sources LightRAG was asked to move away after a parse, instead of moving them."""
    calls: list[str] = []

    async def record(source_path: str) -> str:
        calls.append(source_path)
        return source_path

    monkeypatch.setattr(pipeline, "archive_docx_source_after_full_docs_sync", record)
    return calls


@pytest.fixture
def registered():
    adapter.register()
    yield
    registry._REGISTRY.pop(adapter.ENGINE, None)


def _parse(path: Path, rag: _Rag):
    return asyncio.run(registry.get_parser(adapter.ENGINE).parse(ParseContext(rag, "doc-1", str(path), {})))


class TestRegistration:
    def test_the_engine_is_registered_under_its_name_with_its_suffixes(self, registered):
        spec = registry._REGISTRY[adapter.ENGINE]

        assert isinstance(registry.get_parser("opendockit"), adapter.OpenDocKitParser)
        assert {"hwp", "hwpx", "doc", "ppt", "xls", "pdf", "docx"} <= spec.suffixes
        assert spec.queue_group == "native" and spec.concurrency is None

    def test_what_lightrag_reads_already_is_untouched(self, registered):
        """Installing OpenDocKit next to LightRAG sends no document to it: the built-in engines keep their names and suffixes."""
        before = {name: registry._REGISTRY[name].suffixes for name in ("native", "legacy", "mineru", "docling")}

        adapter.register()

        assert {name: registry._REGISTRY[name].suffixes for name in before} == before

    def test_lightrag_finds_the_engine_by_the_entry_point(self, monkeypatch):
        (entry,) = [entry for entry in entry_points(group=plugins.ENTRY_POINT_GROUP) if entry.name == "opendockit"]
        assert entry.load() is adapter.register
        monkeypatch.setattr(plugins, "_loaded", False)
        try:
            assert "opendockit" in plugins.load_third_party_parsers()
            assert adapter.ENGINE in registry._REGISTRY
        finally:
            registry._REGISTRY.pop(adapter.ENGINE, None)

    def test_the_rule_of_the_guide_and_a_hint_in_a_name_send_a_document_to_the_engine(self, registered):
        rules = "hwp:opendockit,hwpx:opendockit,doc:opendockit,ppt:opendockit,xls:opendockit"

        routing.validate_parser_routing_config(rules)
        engines = {name: routing.resolve_file_parser_engine(name, parser_rules=rules)
                   for name in ("a.hwp", "b.hwpx", "c.doc", "d.pdf", "e.[opendockit].pdf")}

        assert engines == {"a.hwp": "opendockit", "b.hwpx": "opendockit", "c.doc": "opendockit", "d.pdf": "legacy",
                           "e.[opendockit].pdf": "opendockit"}

    def test_no_suffix_is_one_the_converter_reads_only_with_a_vision_model(self):
        assert not adapter.SUFFIXES & {"png", "jpg", "jpeg", "tif", "tiff", "gif", "bmp", "webp", "heic"}
        assert all(suffix == suffix.lower() and "." not in suffix for suffix in adapter.SUFFIXES)


class TestParse:
    @pytest.mark.parametrize("name, data, expected", [
        ("notice.hwpx", builder.hwpx_bytes, "| "), ("report.docx", builder.docx_bytes, "| "),
        ("book.xlsx", builder.xlsx_bytes, "## Sheet: "), ("deck.pptx", builder.pptx_bytes, "## Slide 1"), ("paper.pdf", builder.pdf_bytes, "| 품목 | 수량 | 단가 |")])
    def test_a_document_is_stored_as_markdown_and_its_source_archived(self, tmp_path, registered, archived, name, data, expected):
        path = tmp_path / name
        path.write_bytes(data())
        rag = _Rag()

        result = _parse(path, rag)

        assert expected in result.content
        assert (result.parse_format, result.parse_engine, result.blocks_path) == (FULL_DOCS_FORMAT_RAW, "opendockit", "")
        (doc_id, payload), = rag.stored
        assert doc_id == "doc-1"
        assert (payload["content"], payload["parse_format"], payload["parse_engine"]) == (result.content, FULL_DOCS_FORMAT_RAW,
                                                                                           "opendockit")
        assert archived == [str(path)]
        assert path.suffix.lstrip(".") in adapter.SUFFIXES

    def test_the_whole_document_is_stored_without_page_markers(self, tmp_path, registered, archived):
        """A knowledge base indexes all of a document: the default limit of 500,000 characters is for one model context."""
        pdf, sheet = tmp_path / "paper.pdf", tmp_path / "rows.csv"
        pdf.write_bytes(builder.pdf_bytes(pages=2))
        sheet.write_text("".join(f"row {n},{'x' * 60}\n" for n in range(9000)), encoding="utf-8")

        assert "<!-- Page" not in _parse(pdf, _Rag()).content
        rows = _parse(sheet, _Rag())
        assert len(rows.content) > 500_000 and "row 8999" in rows.content

    def test_a_document_without_text_fails_and_stores_nothing(self, tmp_path, registered, archived):
        """LightRAG's rule for every engine: an empty text must not enter chunking as a document that says nothing."""
        path = tmp_path / "scan.pdf"
        path.write_bytes(_scanned_pdf())
        rag = _Rag()

        with pytest.raises(ValueError, match="found no text"):
            _parse(path, rag)

        assert rag.stored == [] and archived == []

    def test_a_document_that_cannot_be_read_raises_the_conversion_error(self, tmp_path, registered, archived):
        path = tmp_path / "broken.docx"
        path.write_bytes(builder.docx_bytes()[:400])
        rag = _Rag()

        with pytest.raises(CorruptedDocumentError):
            _parse(path, rag)

        assert rag.stored == [] and archived == []

    def test_a_missing_source_is_said_without_its_path(self, tmp_path, registered, archived):
        with pytest.raises(FileNotFoundError) as raised:
            _parse(tmp_path / "gone.pdf", _Rag())

        assert str(raised.value) == "The source file of the document was not found."

    def test_warnings_are_kept_with_the_document_and_absent_when_there_are_none(self, tmp_path, registered, archived):
        clean, misnamed = tmp_path / "clean.docx", tmp_path / "misnamed.pptx"
        clean.write_bytes(builder.docx_bytes())
        misnamed.write_bytes(builder.docx_bytes())

        assert "parse_warnings" not in _parse(clean, _Rag()).to_dict()
        warnings = _parse(misnamed, _Rag()).to_dict()["parse_warnings"]

        assert list(warnings) == ["opendockit"]
        assert warnings["opendockit"] == convert_file(misnamed).warnings != []
