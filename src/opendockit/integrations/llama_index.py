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

from opendockit.converter import convert, convert_file, read_limited
from opendockit.integrations._documents import Mode, check_mode, pieces
from opendockit.models import ConvertOptions


_DEFAULT_OPTIONS = ConvertOptions()


try:
    from llama_index.core.readers.base import BaseReader
    from llama_index.core.schema import Document
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('OpenDocKit\'s LlamaIndex integration needs llama-index-core: pip install "opendockit[llamaindex]"') from exc


__all__ = ["OpenDocKitReader"]

# Metadata kept off the text LlamaIndex embeds and puts in front of every node it sends to the LLM. The warnings are a line
# per warning: a workbook with 80 hidden sheets had 5,269 characters of them in every node, and `SentenceSplitter` refused
# the document ('Metadata length (1132) is longer than chunk size (1024)'); they are about the conversion, not the content,
# and so is `exceeded`. The path is kept for the LLM, which cites it, and off the embedding: it is the same folder names in
# every node and says nothing of what the node holds. Format, title, page and section stay in both
# (Pinned by: test_integrations.py::TestLlamaIndexReader::test_warnings_and_the_path_are_kept_out_of_the_text_that_is_embedded).
_NOT_EMBEDDED = ("warnings", "exceeded", "source")
_NOT_FOR_THE_LLM = ("warnings", "exceeded")


class OpenDocKitReader(BaseReader):
    """Read a file as Markdown `Document`s.

    `mode="single"` (the default) gives one Document per file; `mode="sections"` one per page, slide or sheet, with
    `section_kind`, `section_index` and `section_title` in the metadata. Formats without physical units are one Document in
    either mode. The metadata also has `source`, `format`, the document's own `title` / `author` / counts, `warnings` and
    `exceeded` (see `opendockit.integrations.langchain.OpenDocKitLoader`); `extra_info` is merged over it, as
    `SimpleDirectoryReader` expects. `warnings` and `exceeded` are left out of the text that is embedded and sent to the
    LLM, and `source` out of the embedded text. A file that cannot be converted raises `opendockit.ConversionError`.
    """

    def __init__(self, *, mode: Mode = "single", options: ConvertOptions | None = None) -> None:
        check_mode(mode)
        self.mode = mode
        self.options = options

    def lazy_load_data(self, file: str | os.PathLike[str], extra_info: dict[str, Any] | None = None, *, fs: Any = None,
                       **_: Any) -> Iterable[Document]:
        """`fs` is the fsspec file system `SimpleDirectoryReader` passes on when it was given one: the file is then read
        through it (a bucket, an archive, memory) instead of from the local disk. It is read only up to `max_file_bytes`: read
        whole, a file over the limit was held in memory before it was refused
        (Pinned by: test_integrations.py::TestLlamaIndexReader::test_file_from_a_file_system_is_read_only_up_to_the_size_limit)."""
        path = Path(file)
        if fs is None:
            result = convert_file(path, self.options)
        else:
            with fs.open(str(file), "rb") as stream:
                data = read_limited(stream, (self.options or _DEFAULT_OPTIONS).max_file_bytes)
            result = convert(data, path.name, self.options)
        for text, metadata in pieces(result, str(file), self.mode):
            yield Document(text=text, metadata={**metadata, **(extra_info or {})},
                           excluded_embed_metadata_keys=list(_NOT_EMBEDDED), excluded_llm_metadata_keys=list(_NOT_FOR_THE_LLM))

    def load_data(self, file: str | os.PathLike[str], extra_info: dict[str, Any] | None = None, *, fs: Any = None,
                  **_: Any) -> list[Document]:
        return list(self.lazy_load_data(file, extra_info, fs=fs))
