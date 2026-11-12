"""RTF. Placeholder until the extractor is written."""

from opendockit.exceptions import UnsupportedFormatError
from opendockit.models import ConvertOptions, Extraction


def extract_rtf(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("RTF documents are not supported yet.")

