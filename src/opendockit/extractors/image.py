"""An image file uploaded on its own (JPG, PNG, WEBP, GIF, BMP, TIFF, HEIC). The body is just one image placeholder (one per
page for a multi-page TIFF).

Deliberate: this is the only extractor that uses `opendockit.image`. Splitting TIFF pages is decoding, so it lives in that
module, which owns the decode limit (`_MAX_DECODE_PIXELS`) (the dependency-direction exception in docs/decisions/core.md).
"""

from opendockit.image import split_tiff_pages
from opendockit.models import ConvertOptions, Extraction, ImageKind, Section, SectionKind


def extract_image(data: bytes, options: ConvertOptions) -> Extraction:
    extraction = Extraction()
    if not options.extract_images:
        # `extract_images=False` ignores images entirely (the options table in docs/options.md). The uploaded file is a single
        # picture, so the result is empty; say why
        # (Pinned by: test_image.py::TestAuditedImageGaps::test_extract_images_off_does_not_send_an_uploaded_image_to_the_describer).
        extraction.warnings.append("This is an image file, but image extraction (extract_images) is turned off, so its content was not read.")
        return extraction
    split = split_tiff_pages(data)
    if not split.pages:
        marker = extraction.add_image(data, kind=ImageKind.STANDALONE)
        extraction.sections.append(Section(SectionKind.DOCUMENT, 1, marker))
        return extraction

    # A multi-page TIFF (a fax or a bundle of scans) is one picture and one page section per page. Only the first page was read
    # and the rest vanished without a warning (Pinned by: test_image.py::TestMultiPageTiff). The limit on the number of pictures
    # to describe (`max_images`) is applied by the converter, as for any other picture.
    for number, page in enumerate(split.pages, start=1):
        extraction.sections.append(Section(SectionKind.PAGE, number, extraction.add_image(page, kind=ImageKind.STANDALONE)))

    if split.unreadable:
        extraction.warnings.append(f"Page {split.stopped_at} of the TIFF is corrupted or too large to read, "
                                   f"so only the first {len(split.pages)} page(s) were read as images.")
    elif split.too_many_pixels:
        extraction.warnings.append(f"The pages of the TIFF are too large in total, so only the first {len(split.pages)} page(s) were read as images.")
    elif split.stopped_at:
        extraction.warnings.append(f"The TIFF has too many pages, so only the first {len(split.pages)} page(s) were read as images.")
    return extraction
