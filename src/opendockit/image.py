"""Image decoding, filtering and normalization.

Images inside documents come in every format (BMP and WMF from Hangul (HWP), iPhone HEIC, CMYK JPEG, transparent PNG, TIFF).
Unify them into the PNG/JPEG a vision model accepts, shrink the long side, and drop small images that carry no information.
"""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageChops, ImageOps, JpegImagePlugin

from opendockit.models import quiet_warnings


try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:  # the optional `heic` extra is not installed: only HEIC/HEIF files go unread (AVIF is read by Pillow itself)
    HEIF_AVAILABLE = False

_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


# An aspect ratio more extreme than this with a short side under `_CONTENT_SHORT_SIDE` is a divider or a piece of border. With a
# short side at or above it, the image is content such as a product detail page or a long screen capture
# (Pinned by: test_core_image_and_assembly.py::TestTallPictures).
_MAX_ASPECT_RATIO = 20
_CONTENT_SHORT_SIDE = 256
# When a very tall picture is cut into tiles of readable width (`_tiles`): the long side is more than this multiple of the short
# side, and shrinking the long side to `max_side` would shrink it by more than half. A tile is twice the short side long (the
# short side is kept), there are at most `_MAX_TILES` of them, and each overlaps its neighbors by 1/16.
_TILE_ASPECT_RATIO = 4
_MAX_TILES = 8
_JPEG_QUALITY = 85
# An image with more pixels (width × height) than this is not decoded. Twice Pillow's default warning threshold (89 million);
# it stops decompression bombs.
_MAX_DECODE_PIXELS = 178_956_970
# Maximum number of pages taken out of a multi-page TIFF as pictures. Stops a file that drags out decoding with hundreds of
# thousands of tiny pages. The number of pictures to describe is set separately by `max_images`.
MAX_TIFF_PAGES = 1000
# Limit on the total pixels of the pages of a multi-page TIFF (10 times `_MAX_DECODE_PIXELS`, about 200 pages of 300dpi color A4).
# With only a per-page limit, a 650KB file of 100 bilevel pages of 10,000×10,000 took 9 seconds and 400MB. Pages from the one
# that goes over are not read.
_MAX_TIFF_PIXELS = 10 * _MAX_DECODE_PIXELS
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_JPEG_MAGIC = b"\xff\xd8\xff"
# Minimum share for salvaging a JPEG with a truncated end: the height actually drawn must be at least this much of the whole.
# A file with only a few bytes of picture data left is nearly a plain gray sheet with nothing to describe.
_MIN_TRUNCATED_JPEG_SHARE = 0.1


@dataclass(frozen=True, slots=True)
class TiffPages:
    """The result of splitting a multi-page TIFF. Empty `pages` means it was not split (one page, not a TIFF, or unreadable from the
    first page) — the original is used as is."""

    pages: list[bytes]
    # The number of the first page where splitting stopped (from 1) and why. Both False for the page-count limit (`MAX_TIFF_PAGES`),
    # unreadable for a corrupted or too-large page, too_many_pixels for the total page pixel limit (`_MAX_TIFF_PIXELS`).
    stopped_at: int | None = None
    unreadable: bool = False
    too_many_pixels: bool = False


@dataclass(frozen=True, slots=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    # Tiles of a very tall picture cut top → bottom (left → right if it is wide) (`_tiles`). An empty tuple for an ordinary picture.
    tiles: tuple[bytes, ...] = ()


class ImageSkipped(Exception):
    """An image to drop (small, or a divider). Its placeholder is removed from the body."""


class ImageUnreadable(Exception):
    """An image that could not be decoded (WMF/EMF vectors, a broken file). Placeholder text is left in the body."""


class ImageTooLarge(ImageUnreadable):
    """An image not read because it is over the decode pixel limit (`_MAX_DECODE_PIXELS`). An uploaded picture is reported as 'document too large'."""


def prepare_image(data: bytes, *, min_side: int, max_side: int, filter_decorations: bool = True) -> PreparedImage:
    """`filter_decorations=False` is for an image the user uploaded directly — it is not dropped even if small or thin."""
    try:
        image = _decoded(data, max_side)
    except ImageUnreadable:
        raise
    except Exception as exc:  # Pillow raises all sorts of exceptions on a corrupted file (SyntaxError, struct.error, ...).
        # A decoding failure is an "unreadable image" whatever its type. If it leaked, convert would die with an exception outside
        # ConversionError (Pinned by: test_image.py::test_broken_png_is_unreadable_not_a_crash).
        image = _truncated_jpeg(data, max_side)
        if image is None:
            raise ImageUnreadable(type(exc).__name__) from exc

    width, height = image.size
    too_small = min(width, height) < min_side
    too_thin = (max(width, height) / max(min(width, height), 1) > _MAX_ASPECT_RATIO
                and min(width, height) < _CONTENT_SHORT_SIDE)
    if filter_decorations and (too_small or too_thin):
        raise ImageSkipped

    try:
        if image.mode.startswith("I;16"):
            # Pillow's downscaling does not support 16-bit grayscale and raised a ValueError. Reduce it to 8-bit grayscale
            # (Pinned by: test_image.py::TestPrepareImage::test_high_bit_depth_grayscale_is_converted_not_a_crash).
            image = image.convert("I").point(lambda value: value / 256).convert("L")
        elif image.mode in ("I", "F"):
            image = _stretch_to_8bit(image)

        has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        tiles = tuple(_encoded(tile, max_side, has_alpha)[0] for tile in _tiles(image, max_side))
        data, mime_type, (shrunk_width, shrunk_height) = _encoded(image, max_side, has_alpha)
    except Exception as exc:  # a failed downscale or re-encode is also an "unreadable image", like a failed decode (same reason)
        raise ImageUnreadable(type(exc).__name__) from exc
    return PreparedImage(data, mime_type, shrunk_width, shrunk_height, tiles)


def _decoded(data: bytes, max_side: int) -> Image.Image:
    """The picture, decoded and with its orientation (EXIF) corrected."""
    with quiet_warnings((Image.DecompressionBombWarning, r"PIL\.")):  # thread-safe (`models.quiet_warnings`)
        with _open_within_pixel_limit(data, max_side) as opened:
            opened.seek(0)  # first frame only for an animated GIF/AVIF. A multi-page TIFF is split per page by the extractor first (split_tiff_pages)
            image = ImageOps.exif_transpose(opened)
            image.load()
    return image


def _truncated_jpeg(data: bytes, max_side: int) -> Image.Image | None:
    """A JPEG with a truncated end, read again with the end marker (EOI) appended. None if there is no picture data (after SOS), if it
    still cannot be read, or if the drawn part is less than `_MIN_TRUNCATED_JPEG_SHARE`.

    A JPEG missing only the 2-byte end marker, or cut slightly short, was thrown away whole (an uploaded file was rejected as
    'corrupted', a picture inside a document became placeholder text). Browsers display such a picture. On meeting the end
    marker, the decoder fills the remaining rows with gray and finishes. Pillow's `ImageFile.LOAD_TRUNCATED_IMAGES` is
    process-global, so it is not used. Deliberate: no warning — how much was cut cannot be known, and only the visible part is
    described (Pinned by: test_core_round45.py::TestSlightlyTruncatedJpeg).
    """
    if not data.startswith(_JPEG_MAGIC) or data.endswith(b"\xff\xd9") or b"\xff\xda" not in data:
        return None
    try:
        image = _decoded(data + b"\xff\xd9", max_side)
    except Exception:  # noqa: BLE001 — same reason as above. If it cannot be read again, the original failure is reported
        return None
    if _decoded_height(image) < image.height * _MIN_TRUNCATED_JPEG_SHARE:
        return None
    return image


def _decoded_height(image: Image.Image) -> int:
    """The height actually drawn in a truncated JPEG: down to the last row holding a pixel that differs from the bottom-right pixel
    (the color the decoder filled in)."""
    filled = Image.new(image.mode, image.size, image.getpixel((image.width - 1, image.height - 1)))
    box = ImageChops.difference(image, filled).getbbox()
    return box[3] if box else 0


def _encoded(image: Image.Image, max_side: int, has_alpha: bool) -> tuple[bytes, str, tuple[int, int]]:
    """(bytes, MIME, size) of the picture rewritten as PNG (transparent) or JPEG with the long side shrunk to `max_side`.

    Deliberate: PNG is written with default compression, without `optimize`. Searching for the compression level took most of
    the conversion time of picture-heavy PPTX/HWP files (23 of 31.7 seconds for a 122-slide deck) and the size did not go down
    (Pinned by: test_core_round47.py::TestTransparentPictureEncoding). JPEG's `optimize` only rebuilds the Huffman tables and is
    cheap, so it stays.
    """
    image = image.copy()
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    output = BytesIO()
    if has_alpha:
        image.convert("RGBA").save(output, format="PNG")
        return output.getvalue(), "image/png", image.size
    image.convert("RGB").save(output, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return output.getvalue(), "image/jpeg", image.size


def _tiles(image: Image.Image, max_side: int) -> list[Image.Image]:
    """Tiles of a very tall picture (`_TILE_ASPECT_RATIO`) cut in order along the long side. An empty list if the picture is not one to split.

    Shrinking the whole thing to a long side of `max_side` turned an 860×18000 product detail page into 98×2048 and the text
    was unreadable (Pinned by: test_core_image_and_assembly.py::TestTallPictures). A tile is twice the short side long (there
    are at most `_MAX_TILES`, so a longer picture gets longer tiles) and overlaps its neighbors by 1/16 so that a line of text
    on a boundary is whole in at least one tile. Deliberate: the downscaled whole picture (`data`) is kept — a describer that
    knows nothing about tiles sees it as before.
    """
    width, height = image.size
    short, long = min(width, height), max(width, height)
    if long <= _TILE_ASPECT_RATIO * short or long <= 2 * max_side:
        return []

    count = min(_MAX_TILES, -(-long // (2 * short)))
    length = -(-long // count)
    overlap = length // 16
    tiles = []

    for index in range(count):
        start, end = max(index * length - overlap, 0), min((index + 1) * length + overlap, long)
        tiles.append(image.crop((0, start, width, end) if height > width else (start, 0, end, height)))
    return tiles


def _open_within_pixel_limit(data: bytes, max_side: int) -> Image.Image:
    """Open so that at most `_MAX_DECODE_PIXELS` pixels are decoded. A JPEG over it is read reduced to 1/2–1/8 at decode time (draft).

    Normal photos over Pillow's decompression-bomb limit (the same value), such as panoramas and large scans, were rejected as a
    'corrupted image' (Pinned by: test_core_image_and_assembly.py::TestVeryLargePictures). A JPEG is decoded reduced at the DCT
    stage, so it uses only the memory of the reduced size — the over-limit original is never decoded whole. Image.open rejects
    it in its limit check, so the JPEG decoder is opened directly (global settings such as `MAX_IMAGE_PIXELS` are not changed).
    If it is still over the limit after reduction, or the format cannot be read reduced (PNG, TIFF, ...), raise `ImageTooLarge`.
    """
    try:
        opened = Image.open(BytesIO(data))
    except Image.DecompressionBombError:
        opened = None
    if opened is not None and opened.width * opened.height <= _MAX_DECODE_PIXELS:
        return opened
    if opened is not None:
        opened.close()
    if not data.startswith(_JPEG_MAGIC):
        raise ImageTooLarge("image too large")

    jpeg = JpegImagePlugin.JpegImageFile(BytesIO(data))
    width, height = jpeg.size
    scale = next((scale for scale in (2, 4, 8) if -(-width // scale) * -(-height // scale) <= _MAX_DECODE_PIXELS), None)
    if scale is None:
        jpeg.close()
        raise ImageTooLarge("image too large")

    # While the reduced long side stays at or above the downscale target (max_side), reduce further to save decoding memory and
    # time (the resulting picture is the same size).
    while scale < 8 and -(-max(width, height) // (scale * 2)) >= max_side:
        scale *= 2

    jpeg.draft(None, (max(width // scale, 1), max(height // scale, 1)))
    if jpeg.width * jpeg.height > _MAX_DECODE_PIXELS:
        jpeg.close()
        raise ImageTooLarge("image too large")
    return jpeg


def split_tiff_pages(data: bytes) -> TiffPages:
    """Split a multi-page TIFF (a fax or a bundle of scans) into one single-page TIFF (lossless deflate) per page.

    Only the first page was decoded and the other pages vanished without a warning (Pinned by: test_image.py::TestMultiPageTiff).
    Each page has to be registered as its own picture for the describer (OCR) to see every page. `_MAX_DECODE_PIXELS` is kept
    per page, and on meeting a corrupted or too-large page only the pages before it are returned. The frames of an animated GIF
    are not pages, so they are not split (Deliberate). Pages from the one where the total page pixels go over `_MAX_TIFF_PIXELS`
    are not read (Pinned by: test_core_front_cut_and_tiff_pixels.py::TestMultiPageTiffPixelTotal).
    """
    if not data.startswith(_TIFF_MAGIC):
        return TiffPages([])
    pages: list[bytes] = []
    pixels = 0
    try:
        # EXIF warnings from a corrupted later page and the like (Pillow's UserWarning). The outcome (the page it stopped at) is
        # reported in the warning text. This used to ignore every warning — the thread-safe `models.quiet_warnings` applies to the
        # whole process for the duration of the block, so it was narrowed to Pillow warnings
        with quiet_warnings((UserWarning, r"PIL\."), (Image.DecompressionBombWarning, r"PIL\.")):
            with Image.open(BytesIO(data)) as opened:
                if not _has_frame(opened, 1):
                    return TiffPages([])
                for index in range(MAX_TIFF_PAGES):
                    if not _has_frame(opened, index):
                        return TiffPages(pages)
                    if opened.width * opened.height > _MAX_DECODE_PIXELS:
                        raise ImageUnreadable("page too large")
                    pixels += opened.width * opened.height
                    if pixels > _MAX_TIFF_PIXELS and pages:
                        return TiffPages(pages, len(pages) + 1, too_many_pixels=True)
                    output = BytesIO()
                    ImageOps.exif_transpose(opened).save(output, format="TIFF", compression="tiff_deflate")
                    pages.append(output.getvalue())
                more = _has_frame(opened, MAX_TIFF_PAGES)
    except Exception:  # noqa: BLE001 — same reason as prepare_image (Pillow's assorted decoding exceptions). The pages before it are used
        return TiffPages(pages, len(pages) + 1, unreadable=True) if pages else TiffPages([])

    return TiffPages(pages, MAX_TIFF_PAGES + 1 if more else None)


def _has_frame(opened: Image.Image, index: int) -> bool:
    try:
        opened.seek(index)
    except EOFError:
        return False
    return True


def _stretch_to_8bit(image: Image.Image) -> Image.Image:
    """Convert 32-bit integer (I) or float (F) grayscale to 8-bit grayscale, stretching the picture's minimum–maximum to 0–255.

    The value range is not fixed (floats are commonly 0–1, integers 0–65535). Converted as is, values outside 255 were clipped:
    a 0–1 float picture became all black and a 0–65535 integer one nearly white
    (Pinned by: test_image.py::TestAuditedImageGaps::test_wide_range_grayscale_keeps_its_gradient).
    Only the shapes a vision model reads need to survive, so contrast is kept rather than the original brightness. A picture
    with a single value has that value clipped to 0–255.
    """
    low, high = image.getextrema()
    if high > low:
        scale = 255 / (high - low)
        image = image.convert("F").point(lambda value: value * scale - low * scale)
    else:
        level = min(max(low, 0), 255)
        image = image.convert("F").point(lambda value: value * 0 + level)

    return image.convert("L")


# DIB header sizes of BMP (BITMAPCOREHEADER 12, OS/2 v2 64, BITMAPINFOHEADER 40 and V2–V5). The same ones Pillow reads.
_BMP_DIB_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


def is_image_file(data: bytes) -> bool:
    """Whether this is an image file uploaded on its own (magic bytes).

    BMP is told by 'BM' and the DIB header size. The file size in the header (bfSize) is not trusted: files that write 0 there
    or have padding bytes appended are common
    (Pinned by: test_image.py::TestStandaloneImage::test_bmp_is_detected_without_trusting_the_file_size_field).
    """
    head = data[:32]
    return (
        head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
        or (head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"))
        or (head.startswith(b"BM") and len(head) >= 26 and int.from_bytes(head[14:18], "little") in _BMP_DIB_HEADER_SIZES)
    )


def needs_heif_plugin(data: bytes) -> bool:
    """A HEIC/HEIF file that cannot be read only because the optional `heic` extra (pillow-heif) is not installed.

    The extra is optional because its wheels bundle GPLv2/LGPLv3 libraries. Without this check such a file was reported as
    'corrupted', which hides the one-line fix (Pinned by: test_image.py::TestHeicWithoutTheOptionalExtra).
    """
    return not HEIF_AVAILABLE and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS
