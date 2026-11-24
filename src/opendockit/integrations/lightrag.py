"""LightRAG parser engine: LightRAG reads documents with OpenDocKit.

    pip install "opendockit[lightrag]"
    LIGHTRAG_PARSER="hwp:opendockit,hwpx:opendockit,doc:opendockit,ppt:opendockit,xls:opendockit"

LightRAG finds the engine by itself when both packages are installed (the `lightrag.parsers` entry point calls `register`).
A document goes to it by a routing rule as above, by a hint in its name (`report.[opendockit].pdf`) or by `parse_engine` in an
upload. Nothing changes for a document that is not sent to it: LightRAG's own engines stay the default.

Used as a library, without the server, LightRAG loads its plugins when asked to:

    from lightrag.parser.plugins import load_third_party_parsers
    load_third_party_parsers()
"""

import asyncio
import time

from opendockit.converter import convert_file
from opendockit.models import ConvertOptions


try:
    from lightrag.parser.base import BaseParser, ParseContext, ParseResult
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError('OpenDocKit\'s LightRAG integration needs LightRAG: pip install "opendockit[lightrag]"') from exc


__all__ = ["ENGINE", "SUFFIXES", "OpenDocKitParser", "register"]

ENGINE = "opendockit"
# The documents the engine may be sent, as LightRAG writes a suffix. Plain text and code are left to LightRAG's own engine,
# and an image file is not listed: without a vision model it converts to nothing.
SUFFIXES = frozenset({"pdf", "docx", "pptx", "xlsx", "xlsm", "doc", "ppt", "xls", "hwp", "hwpx", "odt", "ods", "odp", "rtf", "epub",
                      "html", "htm", "xhtml", "mht", "mhtml", "eml", "csv", "tsv"})
# What LightRAG stores as a raw document (`lightrag.constants.FULL_DOCS_FORMAT_RAW`).
_RAW = "raw"
# The whole document is indexed, so the character limit of "one file into one context" is off; a page marker is no content of
# a knowledge base; pictures are not decoded because nothing here describes them, and their alt text stays
# (Pinned by: test_lightrag.py::TestParse::test_the_whole_document_is_stored_without_page_markers).
_OPTIONS = ConvertOptions(max_chars=None, page_markers=False, decode_images=False)
_NO_TEXT = "OpenDocKit found no text in the document. A scanned document needs an engine that reads images."


def register() -> None:
    """Add the engine to LightRAG's registry. The entry point `lightrag.parsers` names this function."""
    from lightrag.parser.registry import ParserSpec, register_parser

    # No queue group of its own: the conversion is local and makes no request, so it shares the pool of LightRAG's local engines.
    register_parser(ParserSpec(engine_name=ENGINE, impl=f"{__name__}:{OpenDocKitParser.__name__}", suffixes=SUFFIXES))


class OpenDocKitParser(BaseParser):
    """Convert one document to Markdown and store it as LightRAG's raw text.

    A document that cannot be converted raises the `opendockit.ConversionError` that `convert_file` raises, and one that
    holds no text (a scan) raises too: LightRAG marks that document failed with the message and goes on with the others.
    What the converter skipped or approximated is kept with the document (`parse_warnings`, under `opendockit`).
    """

    engine_name = ENGINE

    async def parse(self, ctx: ParseContext) -> ParseResult:
        source = ctx.resolve(self.engine_name).source_path
        if not source.is_file():
            raise FileNotFoundError("The source file of the document was not found.")
        result = await asyncio.to_thread(convert_file, source, _OPTIONS)
        # Pinned by: test_lightrag.py::TestParse::test_a_document_without_text_fails_and_stores_nothing
        if result.is_empty:
            raise ValueError(_NO_TEXT)
        await ctx.rag._persist_parsed_full_docs(ctx.doc_id, {
            "content": result.text, "file_path": ctx.file_path, "parse_format": _RAW, "parse_engine": self.engine_name,
            "update_time": int(time.time())})
        await ctx.archive_source(str(source))
        return ParseResult(doc_id=ctx.doc_id, file_path=ctx.file_path, parse_format=_RAW, content=result.text, blocks_path="",
                           parse_engine=self.engine_name,
                           parse_warnings={ENGINE: list(result.warnings)} if result.warnings else None)
