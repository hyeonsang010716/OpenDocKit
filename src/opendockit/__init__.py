"""Convert an uploaded document into a Markdown string ready for LLM input.

    from opendockit import OpenAIImageDescriber, aconvert
    result = await aconvert(data, "report.hwp", describer=OpenAIImageDescriber())
    result.text        # Markdown to feed to the LLM (images are filled in with vision-model descriptions and OCR)
    result.sections    # per page / slide / sheet (for chunking and source attribution)
    result.images      # the extracted images and their descriptions
    result.char_count  # number of characters (picture descriptions included); the basis for upload limits
    result.exceeded    # whether the conversion stopped because the text went over the character limit (500,000 by default)
    result.warnings    # limit exceeded, scanned pages, extension mismatch, and so on

On a server, convert in a worker process with `IsolatedConverter` (so one file's runaway memory or time does not spread to
the server). With `convert` (synchronous) alone, the images are collected in `result.images` and the body keeps only placeholder text.
"""

from importlib.metadata import PackageNotFoundError, version

from opendockit.converter import aconvert, convert, convert_file, render
from opendockit.describe import ImageDescriber, OpenAIImageDescriber, describe_images
from opendockit.detect import detect_format
from opendockit.exceptions import (
    ConversionError,
    CorruptedDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedFormatError,
)
from opendockit.isolated import TOO_COMPLEX_MESSAGE, IsolatedConverter
from opendockit.models import (
    ConversionResult,
    ConvertOptions,
    DocumentFormat,
    DocumentImage,
    ImageKind,
    Section,
    SectionKind,
)


try:
    __version__ = version("opendockit")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

__all__ = [
    "ConversionError",
    "ConversionResult",
    "ConvertOptions",
    "CorruptedDocumentError",
    "DocumentFormat",
    "DocumentImage",
    "DocumentTooLargeError",
    "EncryptedDocumentError",
    "ImageDescriber",
    "ImageKind",
    "IsolatedConverter",
    "OpenAIImageDescriber",
    "Section",
    "SectionKind",
    "TOO_COMPLEX_MESSAGE",
    "UnsupportedFormatError",
    "__version__",
    "aconvert",
    "convert",
    "convert_file",
    "describe_images",
    "detect_format",
    "render",
]
