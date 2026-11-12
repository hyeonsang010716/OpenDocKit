"""Conversion input options and result models."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
import math
import re
import threading
from typing import Final
import warnings


class DocumentFormat(StrEnum):
    """The actual document format, detected from the content (magic bytes). It may differ from the extension."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    XLS = "xls"
    XML_SPREADSHEET = "xml_spreadsheet"  # Excel 2003 XML
    CSV = "csv"
    HWP = "hwp"
    HWPX = "hwpx"
    HTML = "html"
    MARKDOWN = "markdown"
    TEXT = "text"
    IMAGE = "image"


class SectionKind(StrEnum):
    """The physical unit a document is divided into. Used for chunking and source attribution (which page, which slide)."""

    PAGE = "page"
    SLIDE = "slide"
    SHEET = "sheet"
    DOCUMENT = "document"


class ImageKind(StrEnum):
    """The role of an image. It decides which prompt the vision model gets (describe.py)."""

    # A picture, photo, chart or diagram inside the document body. The describer may SKIP it if it is decoration with no information.
    FIGURE = "figure"
    # A PDF page whose text exists only as an image (a scan, etc.). The whole page content is transcribed.
    PAGE = "page"
    # An image file uploaded by itself. The user uploaded it on purpose, so it is never SKIPped.
    STANDALONE = "standalone"


# Lower bounds of the limit options. A negative value quietly produced odd results — `max_chars=-5` cut the last five characters
# with a Python slice and reported 'exceeded', and `max_images=-1` dropped only the last picture. 0 is allowed only for limits
# where 'allow none at all' is meaningful (max_chars, max_images, min_image_side, pdf_time_budget)
# (Pinned by: test_converter.py::TestLimits::test_negative_or_meaningless_zero_limits_are_rejected_when_options_are_made).
_OPTION_MINIMUMS = {
    "max_file_bytes": 1, "max_uncompressed_bytes": 1, "max_chars": 0, "max_pages": 1, "pdf_time_budget": 0,
    "max_sheet_cols": 1, "max_images": 0, "min_image_side": 0, "repeated_image_threshold": 1, "image_max_side": 1,
}

# Integer limits and float limits, and which of them accept None ('no limit'). A value of the wrong type (None, 2.5, "100") passed
# at construction and only blew up during conversion as a 'corrupted document' or a TypeError — `max_sheet_cols=None` rejected an
# Excel file as 'corrupted'. A whole number written as a float (`max_chars=5e5`) is accepted and turned into an integer. bool is
# not accepted as a number (Pinned by: test_converter.py::TestLimits::test_limit_of_the_wrong_type_is_rejected_when_options_are_made,
# test_whole_number_float_limit_becomes_an_integer).
_INTEGER_OPTIONS = frozenset(("max_file_bytes", "max_uncompressed_bytes", "max_chars", "max_pages", "max_sheet_cols", "max_images",
                              "min_image_side", "repeated_image_threshold", "image_max_side"))
_NUMBER_OPTIONS = frozenset(("pdf_time_budget", "pdf_render_scale"))
_OPTIONAL_OPTIONS = frozenset(("max_chars", "max_pages", "max_images", "pdf_time_budget"))


def _checked_number(name: str, value: object) -> object:
    """Check the type of an option value. A whole-number float (5e5) for an integer limit is returned as an int. Otherwise an English `TypeError`."""
    if value is None and name in _OPTIONAL_OPTIONS:
        return value
    if isinstance(value, float) and name in _INTEGER_OPTIONS and value.is_integer():
        return int(value)
    expected = (int,) if name in _INTEGER_OPTIONS else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        kind = "an integer" if name in _INTEGER_OPTIONS else "a number"
        allowed = f"{kind} or None" if name in _OPTIONAL_OPTIONS else kind
        raise TypeError(f"ConvertOptions.{name} must be {allowed} (got {value!r}).")
    # Every comparison with NaN is false, so it passed the lower-bound check and a normal PDF was rejected as 'corrupted'. Infinity
    # is not accepted either — 'no limit' is None
    # (Pinned by: test_converter.py::TestLimits::test_non_finite_number_is_rejected_when_options_are_made).
    if not math.isfinite(value):
        raise ValueError(f"ConvertOptions.{name} must be a finite number (got {value!r}).")
    return value


@dataclass(frozen=True, slots=True)
class ConvertOptions:
    """Conversion limits and what to include.

    The defaults assume "one uploaded file goes into an LLM context". Going over the character limit is reported through
    `ConversionResult.exceeded`, not an exception, and stops the conversion. Only the input-size limits raise
    (`DocumentTooLargeError`).
    """

    max_file_bytes: int = 50 * 1024 * 1024
    # Limit on the total uncompressed size of ZIP-based formats (docx, pptx, xlsx, hwpx) — zip-bomb defense.
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    # Character limit of one file (picture descriptions included). It is a **stopping point**, not a cutting point: the moment it
    # is exceeded, the remaining pages and rows are not read, the remaining pictures are not sent to the vision model, and the
    # result gets `exceeded=True`. This avoids spending time and calls on a document that will be rejected anyway. None converts
    # to the end with no limit.
    max_chars: int | None = 500_000
    max_pages: int | None = None
    # Time (seconds) to spend on PDF layout analysis (pdfminer). Once exceeded, the remaining pages are read quickly with plain
    # pdfium extraction (no table or heading detection). A design PDF full of vector shapes takes over 2 seconds per page.
    # None means no limit.
    pdf_time_budget: float | None = 60.0
    # The number of rows in a sheet is not limited (Deliberate). `max_chars` is the only volume limit.
    max_sheet_cols: int = 100
    include_speaker_notes: bool = True
    include_hidden_sheets: bool = False
    include_headers_footers: bool = False
    include_footnotes: bool = True
    # Remove header, footer and page-number lines that repeat across the pages of a PDF.
    remove_repeated_page_lines: bool = True
    # Put page boundary markers (`<!-- Page 3 -->`) into the full text. Slide and sheet headings are always included.
    page_markers: bool = True

    # ---- Images ----------------------------------------------------------------------------------------------
    # Extract the images in the document into `ConversionResult.images` and mark their place in the body. Describing them
    # (vision model) is done by describe_images.
    extract_images: bool = True
    # Upper bound on the number of images described per document (a cost cap). None describes all of them with no limit (the
    # default). Real Hangul (HWP) documents often had more than 30 pictures, and a picture cut by the limit loses its content.
    # Given a number, images beyond it keep only a placeholder and a warning is added.
    max_images: int | None = None
    # An image whose short side is smaller than this is treated as an icon, bullet or divider and dropped (px).
    min_image_side: int = 64
    # An image that appears this many times or more is treated as a logo or background and dropped.
    repeated_image_threshold: int = 3
    # Maximum long side sent to the vision model (px). OpenAI downscales anyway, so anything larger only slows the upload.
    image_max_side: int = 2048
    # Scale for rendering a PDF to an image (1.0 = 72dpi). 2.0 (144dpi) is enough for OCR of body text.
    pdf_render_scale: float = 2.0

    def __post_init__(self) -> None:
        for name in _INTEGER_OPTIONS | _NUMBER_OPTIONS:
            object.__setattr__(self, name, _checked_number(name, getattr(self, name)))
        for name, minimum in _OPTION_MINIMUMS.items():
            value = getattr(self, name)
            if value is not None and value < minimum:
                raise ValueError(f"ConvertOptions.{name} must be at least {minimum} (got {value!r}).")
        if self.pdf_render_scale <= 0:
            raise ValueError(f"ConvertOptions.pdf_render_scale must be greater than 0 (got {self.pdf_render_scale!r}).")


@dataclass(slots=True)
class Section:
    """The Markdown of one page, slide or sheet. A document with no physical unit is a single DOCUMENT.

    `template` is the original that still holds the image placeholders (⟦IMG:<key>:n⟧). `text` is rebuilt from it once the
    image descriptions are filled in.
    """

    kind: SectionKind
    index: int
    text: str
    title: str | None = None
    template: str = field(default="", repr=False)


@dataclass(slots=True)
class DocumentImage:
    """One image extracted from the document. `data` is PNG/JPEG normalized so it can be sent to a vision model as is."""

    id: int
    data: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    kind: ImageKind
    section_kind: SectionKind
    section_index: int
    alt: str | None = None
    # Part of the body text right before the image. Sent along so the vision model reads the picture in the document context.
    context: str = field(default="", repr=False)
    description: str | None = None
    # An image the describer judged to be "decoration with no information". It is removed from the body.
    decorative: bool = False
    # An image left as placeholder text because the vision-model call failed. Calling describe_images again retries only these.
    failed: bool = False
    # Tiles of a very tall picture (a product detail page, a long screen capture) cut in order at a readable width (PNG/JPEG, same
    # format as `data`). An empty tuple for an ordinary picture. `data` is still the downscaled whole picture, so a describer that
    # knows nothing about tiles can use it (`image._tiles`).
    tiles: tuple[bytes, ...] = field(default=(), repr=False)


@dataclass(slots=True)
class ConversionResult:
    """The conversion result. `text` is the final string to feed to the LLM."""

    format: DocumentFormat
    text: str
    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # The conversion stopped because the character count went over `max_chars`. `text` then holds only up to the limit, and
    # `char_count` is what was counted until the stop (the real length of the document is at least that).
    exceeded: bool = False
    # Number of characters in the body. It grows as picture descriptions are filled in (`describe_images` counts again).
    char_count: int = 0
    images: list[DocumentImage] = field(default_factory=list)
    # The original state needed to render again after image description. Not part of the public API.
    base_warnings: list[str] = field(default_factory=list, repr=False)
    options: ConvertOptions = field(default_factory=lambda: ConvertOptions(), repr=False)
    fallbacks: dict[int, str] = field(default_factory=dict, repr=False)
    # Characters counted up to the point of stopping over the limit (an estimate from the extractor and description stages).
    # Keeps char_count from coming out smaller than this.
    counted: int = field(default=0, repr=False)
    # The image placeholder key of this result (`image_marker`). Placeholders are found by the same key even when the descriptions
    # are filled in from another process.
    marker_key: str = field(default="", repr=False)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(slots=True)
class RawImage:
    """A raw image taken out by an extractor. The converter decodes, filters and downscales it into a `DocumentImage`."""

    id: int
    data: bytes = field(repr=False)
    kind: ImageKind = ImageKind.FIGURE
    alt: str | None = None


@dataclass(slots=True)
class Extraction:
    """The extractor → converter boundary. An extractor only builds sections; the converter normalizes, renders and cuts."""

    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    images: list[RawImage] = field(default_factory=list)
    # Characters counted during extraction, and whether that went over the limit and stopped extraction. Extractors that read
    # page by page, row by row or slide by slide fill these in with `count`.
    chars: int = 0
    exceeded: bool = False

    def count(self, chars: int, options: ConvertOptions) -> bool:
        """Add the number of characters just extracted. True once the limit (`options.max_chars`) is exceeded — the signal to stop extracting.

        The value counted here must **never be larger than the real one**. If it were, a document under the limit would be blocked
        as exceeded. So extractors pass a value measured with `markdown.solid_length` (the length without whitespace, placeholders
        and characters that will be removed). The exact character count is taken by the converter after rendering
        (Pinned by: test_converter.py::TestLimitIsExact).
        """
        self.chars += chars
        if options.max_chars is not None and self.chars > options.max_chars:
            self.exceeded = True
        return self.exceeded

    def add_image(self, data: bytes, *, kind: ImageKind = ImageKind.FIGURE, alt: str | None = None) -> str:
        """Register an image and return the placeholder to put in the body. Set the placeholder off with blank lines so it stands as a block."""
        image_id = len(self.images) + 1
        self.images.append(RawImage(image_id, data, kind, alt.strip() if alt and alt.strip() else None))
        return image_marker(image_id)


# The image placeholder in the body, `⟦IMG:<key>:<number>⟧`. Rendering turns it into a description or placeholder text, or removes it.
# convert picks the key per document (the start of a hash of the file content). Even if the body contains text of the same shape
# (`⟦IMG:1⟧`), a file cannot write down its own hash in advance, so it never mixes with real placeholders. Without the key, a
# placeholder written as text was erased and the picture with the same number was inserted twice
# (Pinned by: test_converter.py::TestAssembly::test_text_that_looks_like_an_image_placeholder_is_kept_as_text).
_MARKER_KEY: ContextVar[str] = ContextVar("marker_key", default="")
# Finds every placeholder shape regardless of the key. Used only to subtract from the character estimate (it counts less, which
# is the safe side for the limit decision).
IMAGE_MARKER: Final = re.compile(r"⟦IMG:(?:[0-9a-f]*:)?\d+⟧")


@contextmanager
def marker_key(key: str) -> Iterator[None]:
    """Use `key` for the placeholders made inside this block (extractors need not know the key)."""
    token = _MARKER_KEY.set(key)
    try:
        yield
    finally:
        _MARKER_KEY.reset(token)


def image_marker(image_id: int) -> str:
    # Uses ordinary Unicode brackets that normalization (clean_text) does not remove.
    return f"⟦IMG:{_MARKER_KEY.get()}:{image_id}⟧"


@lru_cache(maxsize=64)
def image_marker_pattern(key: str) -> re.Pattern[str]:
    """A regex that finds only the placeholders made with `key` (the number is the first group)."""
    return re.compile(rf"⟦IMG:{re.escape(key)}:(\d+)⟧")


# The filters `quiet_warnings` currently has in place: (category, module regex) → [the entry put into warnings.filters (None if it
# was already there), number of users].
_QUIET_FILTERS: dict[tuple[type[Warning], str], list] = {}
_QUIET_LOCK = threading.Lock()


@contextmanager
def quiet_warnings(*filters: tuple[type[Warning], str]) -> Iterator[None]:
    """Ignore warnings matching (warning category, regex of the module that issued it) for the duration of this block. Even when
    several threads overlap, only the entry that was added is removed, when the last user leaves, so the warning configuration is
    the same as before the block.

    `warnings.catch_warnings` is not thread-safe (Python 3.12): it saves the list on entry and restores it wholesale on exit, so
    when the first of two overlapping conversions to enter also left first, the filter stayed in the process for good (Pinned by:
    test_core_spreadsheet.py::TestOpenpyxlWarnings::test_overlapping_conversions_on_threads_leave_the_filters_as_they_were).
    Deliberate (Known accepted gap): the warning configuration is process-global, so while even one block is open, warnings of the
    same category and module from other threads are ignored too (which is why the category and module are given narrowly). The
    whole conversion is not serialized behind a lock — one warning would turn concurrent conversions sequential. Overlap with a
    `catch_warnings` outside this library cannot be prevented.
    """
    with _QUIET_LOCK:
        for category, module in filters:
            held = _QUIET_FILTERS.get((category, module))
            if held is None:
                entry = ("ignore", None, category, re.compile(module) if module else None, 0)
                added = entry not in warnings.filters
                if added:
                    warnings.filterwarnings("ignore", category=category, module=module)
                held = _QUIET_FILTERS[(category, module)] = [warnings.filters[0] if added else None, 0]
            held[1] += 1

    try:
        yield
    finally:
        with _QUIET_LOCK:
            for key in filters:
                held = _QUIET_FILTERS[key]
                held[1] -= 1
                if held[1] == 0:
                    del _QUIET_FILTERS[key]
                    _remove_filter(held[0])


def _remove_filter(entry: tuple | None) -> None:
    if entry is None or entry not in warnings.filters:
        return
    warnings.filters.remove(entry)
    warnings._filters_mutated()  # drop the 'already seen' warning records where it was ignoring (as filterwarnings and resetwarnings do)
