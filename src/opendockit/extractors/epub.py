"""EPUB. Placeholder until the extractor is written."""

from opendockit.exceptions import UnsupportedFormatError
from opendockit.models import ConvertOptions, Extraction


def extract_epub(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("EPUB documents are not supported yet.")

