"""Word and PowerPoint 97-2003. Placeholder until the extractor is written."""

from opendockit.exceptions import UnsupportedFormatError
from opendockit.models import ConvertOptions, Extraction


def extract_doc(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("Legacy Word (.doc) documents are not supported yet.")

def extract_ppt(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("Legacy PowerPoint (.ppt) documents are not supported yet.")

