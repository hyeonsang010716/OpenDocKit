"""Per-format extractors. An extractor implements just one thing: `(bytes, ConvertOptions) -> Extraction`."""

from collections.abc import Callable

from opendockit.extractors.docx import extract_docx
from opendockit.extractors.html import extract_html
from opendockit.extractors.hwp import extract_hwp
from opendockit.extractors.hwpx import extract_hwpx
from opendockit.extractors.image import extract_image
from opendockit.extractors.pdf import extract_pdf
from opendockit.extractors.pptx import extract_pptx
from opendockit.extractors.spreadsheet import (
    extract_csv,
    extract_xls,
    extract_xlsx,
    extract_xml_spreadsheet,
)
from opendockit.extractors.text import extract_markdown, extract_text
from opendockit.models import ConvertOptions, DocumentFormat, Extraction


Extractor = Callable[[bytes, ConvertOptions], Extraction]

EXTRACTORS: dict[DocumentFormat, Extractor] = {
    DocumentFormat.PDF: extract_pdf,
    DocumentFormat.DOCX: extract_docx,
    DocumentFormat.PPTX: extract_pptx,
    DocumentFormat.XLSX: extract_xlsx,
    DocumentFormat.XLS: extract_xls,
    DocumentFormat.XML_SPREADSHEET: extract_xml_spreadsheet,
    DocumentFormat.CSV: extract_csv,
    DocumentFormat.HWP: extract_hwp,
    DocumentFormat.HWPX: extract_hwpx,
    DocumentFormat.HTML: extract_html,
    DocumentFormat.MARKDOWN: extract_markdown,
    DocumentFormat.TEXT: extract_text,
    DocumentFormat.IMAGE: extract_image,
}
