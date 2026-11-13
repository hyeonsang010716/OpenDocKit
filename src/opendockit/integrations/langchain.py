"""LangChain document loader and blob parser.

    pip install "opendockit[langchain]"

    from opendockit.integrations.langchain import OpenDocKitLoader

    docs = OpenDocKitLoader("report.hwp").load()                         # one Document for the file
    docs = OpenDocKitLoader(["a.pdf", "b.xlsx"], mode="sections").load()   # one per page / slide / sheet

`OpenDocKitParser` does the same for a `Blob`, for use with `GenericLoader` and other blob loaders.
"""

from collections.abc import Iterator, Sequence
import os
from pathlib import Path

from opendockit.converter import convert, convert_file
from opendockit.integrations._documents import Mode, check_mode, pieces
from opendockit.models import ConvertOptions


try:
    from langchain_core.document_loaders import BaseBlobParser, BaseLoader, Blob
    from langchain_core.documents import Document
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('OpenDocKit\'s LangChain integration needs langchain-core: pip install "opendockit[langchain]"') from exc


__all__ = ["OpenDocKitLoader", "OpenDocKitParser"]


class OpenDocKitLoader(BaseLoader):
    """Load files as Markdown `Document`s.

    `mode="single"` (the default) gives one Document per file; `mode="sections"` one per page, slide or sheet, with
    `section_kind`, `section_index` and `section_title` in the metadata (for citations such as "page 3"). Formats without
    physical units — DOCX, HWP, HTML — are one Document in either mode.

    The metadata also has `source`, `format`, what the document says about itself (`title`, `author`, `page_count`, …),
    `warnings` (one line each) when the converter raised any, and `exceeded` when `options.max_chars` stopped the conversion.
    A file that cannot be converted raises the `opendockit.ConversionError` that `convert_file` raises.
    """

    def __init__(self, file_path: str | os.PathLike[str] | Sequence[str | os.PathLike[str]], *,
                 mode: Mode = "single", options: ConvertOptions | None = None) -> None:
        check_mode(mode)
        single = isinstance(file_path, (str, os.PathLike))
        self.file_paths = [Path(file_path)] if single else [Path(path) for path in file_path]
        self.mode = mode
        self.options = options

    def lazy_load(self) -> Iterator[Document]:
        for path in self.file_paths:
            result = convert_file(path, self.options)
            for text, metadata in pieces(result, str(path), self.mode):
                yield Document(page_content=text, metadata=metadata)


class OpenDocKitParser(BaseBlobParser):
    """Parse a `Blob` into Markdown `Document`s; see `OpenDocKitLoader` for `mode` and the metadata.

    The format is detected from the content; the blob's path, when it has one, is only a hint and the `source`.
    """

    def __init__(self, *, mode: Mode = "single", options: ConvertOptions | None = None) -> None:
        check_mode(mode)
        self.mode = mode
        self.options = options

    def lazy_parse(self, blob: Blob) -> Iterator[Document]:
        source = blob.source
        name = Path(str(blob.path)).name if blob.path else None
        result = convert(blob.as_bytes(), name, self.options)
        for text, metadata in pieces(result, source, self.mode):
            yield Document(page_content=text, metadata={**blob.metadata, **metadata})
