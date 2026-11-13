"""LlamaIndex reader.

    pip install "opendockit[llamaindex]"

    from llama_index.core import SimpleDirectoryReader
    from opendockit.integrations.llama_index import OpenDocKitReader

    reader = OpenDocKitReader(mode="sections")
    docs = reader.load_data("report.hwp")
    docs = SimpleDirectoryReader("inbox", file_extractor=dict.fromkeys([".hwp", ".hwpx", ".pdf", ".xlsx"], reader)).load_data()
"""

from collections.abc import Iterable
import os
from pathlib import Path
from typing import Any

from opendockit.converter import convert, convert_file
from opendockit.integrations._documents import Mode, check_mode, pieces
from opendockit.models import ConvertOptions


try:
    from llama_index.core.readers.base import BaseReader
    from llama_index.core.schema import Document
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('OpenDocKit\'s LlamaIndex integration needs llama-index-core: pip install "opendockit[llamaindex]"') from exc


__all__ = ["OpenDocKitReader"]


class OpenDocKitReader(BaseReader):
    """Read a file as Markdown `Document`s.

    `mode="single"` (the default) gives one Document per file; `mode="sections"` one per page, slide or sheet, with
    `section_kind`, `section_index` and `section_title` in the metadata. Formats without physical units are one Document in
    either mode. The metadata also has `source`, `format`, the document's own `title` / `author` / counts, `warnings` and
    `exceeded` (see `opendockit.integrations.langchain.OpenDocKitLoader`); `extra_info` is merged over it, as
    `SimpleDirectoryReader` expects. A file that cannot be converted raises `opendockit.ConversionError`.
    """

    def __init__(self, *, mode: Mode = "single", options: ConvertOptions | None = None) -> None:
        check_mode(mode)
        self.mode = mode
        self.options = options

    def lazy_load_data(self, file: str | os.PathLike[str], extra_info: dict[str, Any] | None = None, *, fs: Any = None,
                       **_: Any) -> Iterable[Document]:
        """`fs` is the fsspec file system `SimpleDirectoryReader` passes on when it was given one: the file is then read
        through it (a bucket, an archive, memory) instead of from the local disk."""
        path = Path(file)
        if fs is None:
            result = convert_file(path, self.options)
        else:
            with fs.open(str(file), "rb") as stream:
                result = convert(stream.read(), path.name, self.options)
        for text, metadata in pieces(result, str(file), self.mode):
            yield Document(text=text, metadata={**metadata, **(extra_info or {})})

    def load_data(self, file: str | os.PathLike[str], extra_info: dict[str, Any] | None = None, *, fs: Any = None,
                  **_: Any) -> list[Document]:
        return list(self.lazy_load_data(file, extra_info, fs=fs))
