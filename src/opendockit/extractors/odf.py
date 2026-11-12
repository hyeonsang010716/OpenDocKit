"""OpenDocument (ODT, ODS, ODP). Placeholder until the extractor is written."""

from opendockit.exceptions import UnsupportedFormatError
from opendockit.models import ConvertOptions, Extraction


def extract_odt(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("OpenDocument Text documents are not supported yet.")

def extract_ods(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("OpenDocument Spreadsheet documents are not supported yet.")

def extract_odp(data: bytes, options: ConvertOptions) -> Extraction:
    raise UnsupportedFormatError("OpenDocument Presentation documents are not supported yet.")

