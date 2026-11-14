"""The LangChain loader / parser and the LlamaIndex reader (`opendockit.integrations`)."""

from pathlib import Path
import sys

import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, DocumentTooLargeError
from tests import builder
from tests.test_converter import WritingPipe


try:   # the adapters raise ImportError with the install command; `importorskip` skips only on ModuleNotFoundError
    from opendockit.integrations import langchain, llama_index
except ImportError:
    pytest.skip("needs the langchain and llamaindex extras", allow_module_level=True)


def _scanned_pdf() -> bytes:
    """One page that is a picture from edge to edge, with no text layer."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(600, 800, seed=21))), 0, 0, width=width, height=height)
    pdf.save()
    return buffer.getvalue()


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    """One document per kind of section: sheets, slides, pages, and a document without units."""
    documents = {"book.xlsx": builder.xlsx_bytes(), "deck.pptx": builder.pptx_bytes(), "paper.pdf": builder.pdf_bytes(pages=2),
                 "report.docx": builder.docx_bytes(), "notice.hwpx": builder.hwpx_bytes()}
    for name, data in documents.items():
        (tmp_path / name).write_bytes(data)
    return {name: tmp_path / name for name in documents}


class TestLangChainLoader:
    def test_one_document_per_file_by_default(self, files):
        """Two files, default mode: one Document each, holding the full Markdown, with the path as `source` and the format."""
        documents = langchain.OpenDocKitLoader([files["report.docx"], files["book.xlsx"]]).load()

        assert [document.metadata["format"] for document in documents] == ["docx", "xlsx"]
        assert [document.metadata["source"] for document in documents] == [str(files["report.docx"]), str(files["book.xlsx"])]
        assert documents[0].page_content.startswith("# 1장 개요")
        assert "## Sheet: " in documents[1].page_content
        assert "section_index" not in documents[0].metadata

    def test_sections_mode_gives_a_document_per_sheet_slide_and_page(self, files):
        """A workbook, a deck and a PDF in `sections` mode. Each piece says which unit it is in its metadata (for a citation such
        as "page 2"), and a sheet or slide also starts with the heading the full text gives it, so that it reads alone."""
        sheets = langchain.OpenDocKitLoader(files["book.xlsx"], mode="sections").load()
        slides = langchain.OpenDocKitLoader(str(files["deck.pptx"]), mode="sections").load()
        pages = langchain.OpenDocKitLoader(files["paper.pdf"], mode="sections").load()

        assert [(d.metadata["section_kind"], d.metadata["section_index"]) for d in sheets][:1] == [("sheet", 1)]
        assert all(d.page_content.startswith(f"## Sheet: {d.metadata['section_title']}") for d in sheets)
        assert [d.metadata["section_index"] for d in slides] == list(range(1, len(slides) + 1)) and len(slides) > 1
        assert all(d.page_content.startswith(f"## Slide {d.metadata['section_index']}") for d in slides)
        assert [(d.metadata["section_kind"], d.metadata["section_index"]) for d in pages] == [("page", 1), ("page", 2)]
        assert not any("<!-- Page" in d.page_content for d in pages)   # the page number is metadata, not text
        assert pages[0].metadata["page_count"] == "2"

    def test_document_without_units_is_one_piece_in_either_mode(self, files):
        for name in ("report.docx", "notice.hwpx"):
            whole = langchain.OpenDocKitLoader(files[name]).load()
            split = langchain.OpenDocKitLoader(files[name], mode="sections").load()
            assert len(whole) == len(split) == 1 and whole[0].page_content == split[0].page_content

    def test_metadata_values_are_plain_scalars(self, files):
        """Vector stores reject lists and nested values in metadata. Warnings are one string (a line per warning) and are there
        only when there are any; `exceeded` only when the limit stopped the conversion."""
        limited = langchain.OpenDocKitLoader(files["paper.pdf"], options=ConvertOptions(max_chars=40)).load()[0]
        plain = langchain.OpenDocKitLoader(files["report.docx"]).load()[0]

        assert limited.metadata["exceeded"] is True and len(limited.page_content) <= 40
        assert "exceeded 40 characters" in limited.metadata["warnings"]
        assert "exceeded" not in plain.metadata and "warnings" not in plain.metadata
        for document in (limited, plain):
            assert all(isinstance(value, (str, int, bool)) for value in document.metadata.values())

    @pytest.mark.parametrize("mode", ["single", "sections"])
    def test_file_without_any_text_gives_no_document_and_a_logged_warning(self, tmp_path, caplog, mode):
        """A scanned PDF: every page is a picture. Read page by page there is no piece; read whole there would be a Document
        holding nothing but `<!-- Page 1 -->`, which is not content either. A file missing from an index without a word is
        the failure nobody notices, and the converter's warning has no Document to travel on — it is logged, with the path."""
        scan = tmp_path / "scan.pdf"
        scan.write_bytes(_scanned_pdf())

        with caplog.at_level("WARNING", logger="opendockit.integrations"):
            assert langchain.OpenDocKitLoader(scan, mode=mode).load() == []
        messages = [record.getMessage() for record in caplog.records]
        assert len(messages) == 1 and str(scan) in messages[0] and "image description (OCR)" in messages[0]

    def test_slide_that_is_only_a_title_is_kept(self, tmp_path):
        """A deck of divider slides: titles and nothing else. The title lives in the slide's heading, not in its text, so the
        slides used to vanish in `sections` mode (and the log said no text was extracted). A titled slide is a piece; a slide
        with neither title nor text is not."""
        from io import BytesIO

        from pptx import Presentation

        deck = Presentation()
        for title in ("Agenda", None, "Results"):
            slide = deck.slides.add_slide(deck.slide_layouts[5])
            if title:
                slide.shapes.title.text = title
        buffer = BytesIO()
        deck.save(buffer)
        (tmp_path / "dividers.pptx").write_bytes(buffer.getvalue())

        documents = langchain.OpenDocKitLoader(tmp_path / "dividers.pptx", mode="sections").load()

        assert [document.page_content for document in documents] == ["## Slide 1: Agenda", "## Slide 3: Results"]
        assert [document.metadata["section_index"] for document in documents] == [1, 3]

    def test_lazy_load_converts_a_file_only_when_it_is_reached(self, files, tmp_path):
        """The second path does not exist: the first Document still comes out, and the error is raised when the second is asked for."""
        loader = langchain.OpenDocKitLoader([files["report.docx"], tmp_path / "missing.docx"])
        iterator = loader.lazy_load()

        assert next(iterator).metadata["format"] == "docx"
        with pytest.raises(FileNotFoundError):
            next(iterator)

    def test_file_that_cannot_be_converted_raises_the_conversion_error(self, tmp_path):
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"PK\x03\x04 not a real package")
        with pytest.raises(CorruptedDocumentError):
            langchain.OpenDocKitLoader(broken).load()

    def test_unknown_mode_is_rejected_when_the_loader_is_made(self, files):
        with pytest.raises(ValueError, match="'single' or 'sections'"):
            langchain.OpenDocKitLoader(files["report.docx"], mode="pages")

    def test_blob_parser_reads_bytes_and_keeps_the_blob_metadata(self, files):
        """A `Blob` built from bytes with a path hint and its own metadata: the format comes from the content, the blob's
        metadata stays, and the parser's is added to it."""
        from langchain_core.document_loaders import Blob

        blob = Blob.from_data(files["deck.pptx"].read_bytes(), path="uploads/quarterly.bin", metadata={"tenant": "acme"})
        documents = langchain.OpenDocKitParser(mode="sections").parse(blob)

        assert len(documents) > 1
        assert documents[0].metadata["tenant"] == "acme" and documents[0].metadata["format"] == "pptx"
        assert documents[0].metadata["source"] == "uploads/quarterly.bin"

    @pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX")
    def test_loader_reads_a_pipe_only_up_to_the_size_limit(self, tmp_path):
        """The loader goes through `convert_file`, which reads a file without a reported size (a pipe) only up to
        `max_file_bytes` instead of to its end."""
        pipe = WritingPipe(tmp_path / "upload.txt", total=64 * 1024 * 1024)

        with pytest.raises(DocumentTooLargeError):
            langchain.OpenDocKitLoader(pipe.path, options=ConvertOptions(max_file_bytes=1024)).load()

        assert pipe.finish() < 8 * 1024 * 1024

    @pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX")
    def test_parser_reads_a_blob_only_up_to_the_size_limit(self, tmp_path):
        """A blob backed by a file was read whole (`as_bytes`) before the size limit refused it."""
        from langchain_core.document_loaders import Blob

        pipe = WritingPipe(tmp_path / "upload.txt", total=64 * 1024 * 1024)

        with pytest.raises(DocumentTooLargeError):
            langchain.OpenDocKitParser(options=ConvertOptions(max_file_bytes=1024)).parse(Blob.from_path(pipe.path))

        assert pipe.finish() < 8 * 1024 * 1024

    def test_parser_reads_a_blob_of_text(self):
        """A blob holding text (not bytes) is still read: only blobs backed by a file are streamed."""
        from langchain_core.document_loaders import Blob

        documents = langchain.OpenDocKitParser().parse(Blob.from_data("plain note", path="note.txt"))

        assert [document.page_content for document in documents] == ["plain note"]


class TestLlamaIndexReader:
    def test_load_data_gives_documents_with_the_same_pieces_as_the_loader(self, files):
        """Same cut and same metadata as the LangChain loader, so an index built with either cites the same units."""
        reader = llama_index.OpenDocKitReader(mode="sections")
        documents = reader.load_data(files["book.xlsx"])
        expected = langchain.OpenDocKitLoader(files["book.xlsx"], mode="sections").load()

        assert [document.text for document in documents] == [document.page_content for document in expected]
        assert [document.metadata for document in documents] == [document.metadata for document in expected]

    def test_extra_info_is_merged_into_the_metadata(self, files):
        document = llama_index.OpenDocKitReader().load_data(files["report.docx"], extra_info={"file_name": "report.docx", "team": "ops"})[0]

        assert document.metadata["team"] == "ops" and document.metadata["format"] == "docx"

    def test_works_as_a_file_extractor_of_simple_directory_reader(self, files, tmp_path):
        """The way LlamaIndex users plug a reader in: by extension. A Hangul file and a workbook in one folder."""
        from llama_index.core import SimpleDirectoryReader

        reader = llama_index.OpenDocKitReader(mode="sections")
        documents = SimpleDirectoryReader(input_files=[str(files["notice.hwpx"]), str(files["book.xlsx"])],
                                          file_extractor={".hwpx": reader, ".xlsx": reader}).load_data()

        formats = {document.metadata["format"] for document in documents}
        assert formats == {"hwpx", "xlsx"}
        assert all(document.metadata["file_name"] in ("notice.hwpx", "book.xlsx") for document in documents)

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError, match="'single' or 'sections'"):
            llama_index.OpenDocKitReader(mode="chunks")

    def test_file_system_given_by_the_directory_reader_is_used(self, files):
        """`SimpleDirectoryReader(fs=...)` passes its fsspec file system to the reader. The file here exists only in an
        in-memory file system: ignoring `fs` read the local disk, found nothing, and the directory reader skipped the file."""
        import fsspec
        from llama_index.core import SimpleDirectoryReader

        memory = fsspec.filesystem("memory")
        memory.pipe("/inbox/book.xlsx", files["book.xlsx"].read_bytes())
        try:
            documents = SimpleDirectoryReader("/inbox", fs=memory, raise_on_error=True,
                                              file_extractor={".xlsx": llama_index.OpenDocKitReader(mode="sections")}).load_data()
        finally:
            memory.rm("/inbox", recursive=True)

        assert documents and {document.metadata["format"] for document in documents} == {"xlsx"}

    def test_lazy_loading_gives_the_same_documents(self, files):
        reader = llama_index.OpenDocKitReader(mode="sections")

        assert [d.text for d in reader.lazy_load_data(files["book.xlsx"])] == [d.text for d in reader.load_data(files["book.xlsx"])]

    def test_file_from_a_file_system_is_read_only_up_to_the_size_limit(self):
        """Read through `fs.open(...).read()`, a file over `max_file_bytes` was held in memory whole before it was refused."""
        class Endless:
            handed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size=-1):
                assert size >= 0, "never read to the end"
                Endless.handed += size
                return b"a" * size

        class FileSystem:
            def open(self, path, mode):
                return Endless()

        with pytest.raises(DocumentTooLargeError):
            llama_index.OpenDocKitReader(options=ConvertOptions(max_file_bytes=1024)).load_data("bucket/upload.txt", fs=FileSystem())

        assert Endless.handed == 1025
