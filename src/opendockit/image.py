"""Image decoding, filtering and normalization.

Images inside documents come in every format (BMP and WMF from Hangul (HWP), iPhone HEIC, CMYK JPEG, transparent PNG, TIFF).
Unify them into the PNG/JPEG a vision model accepts, shrink the long side, and drop small images that carry no information.
"""

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
import math
import struct
import sys
from typing import Any
import zlib

from PIL import Image, ImageChops, ImageMath, ImageOps, JpegImagePlugin

from opendockit.models import quiet_warnings


try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:  # the optional `heic` extra is not installed: only HEIC/HEIF files go unread (AVIF is read by Pillow itself)
    HEIF_AVAILABLE = False

_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")

# The only Pillow readers a picture is offered to: raster formats decoded inside this process. Left to itself `Image.open`
# tries every plugin, and an EPS reached the PostScript plugin, which starts Ghostscript on the file. Deliberate: EPS, WMF/EMF
# (drawn through Windows GDI, and documented as placeholders) and the scientific/container stubs are not listed
# (Pinned by: test_audit_runtime.py::TestPictureFormatAllowList::test_eps_is_refused_without_reaching_the_postscript_plugin).
# A JPEG that is an MPO is read by the JPEG reader. A name Pillow has not registered is a KeyError, hence the HEIF condition.
_OPEN_FORMATS = ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP", "AVIF", *(("HEIF",) if HEIF_AVAILABLE else ()),
                 "ICO", "JPEG2000", "PCX", "PPM", "DIB")


# An aspect ratio more extreme than this with a short side under `_CONTENT_SHORT_SIDE` is a divider or a piece of border. With a
# short side at or above it, the image is content such as a product detail page or a long screen capture
# (Pinned by: test_core_image_and_assembly.py::TestTallPictures).
_MAX_ASPECT_RATIO = 20
_CONTENT_SHORT_SIDE = 256
# When a very tall picture is cut into tiles of readable width (`_tile_boxes`): the long side is more than this multiple of the short
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
# The warnings Pillow gives while reading and writing a picture: metadata it could not take as written (an EXIF or TIFF tag
# of the wrong count), palette transparency, and the decompression-bomb warning below the limit. The outcome is what counts
# (read, or `ImageUnreadable`); left on, a program that turns warnings into errors had a readable picture rejected as
# 'corrupted' (Pinned by: test_image.py::TestPillowWarnings). Narrow to Pillow's modules (`models.quiet_warnings`), and to
# where a picture is opened and loaded: nothing warns while it is downscaled and written.
_PILLOW_WARNINGS = ((UserWarning, r"PIL\."), (Image.DecompressionBombWarning, r"PIL\."))
# What a transparent grey level of a PNG with 1, 2 or 4 bits per pixel is multiplied by to be on the scale of the pixels
# as Pillow hands them out (0–255), by the raw mode of the file (`_key_on_the_scale_of_the_pixels`).
_KEY_SCALE = {"1": 255, "L;2": 85, "L;4": 17}
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Minimum share for salvaging a JPEG with a truncated end: the height actually drawn must be at least this much of the whole.
# A file with only a few bytes of picture data left is nearly a plain gray sheet with nothing to describe.
_MIN_TRUNCATED_JPEG_SHARE = 0.1


@dataclass(frozen=True, slots=True)
class TiffPages:
    """The result of splitting a multi-page TIFF. Empty `pages` means it was not split (one page, not a TIFF, or unreadable from the
    first page) — the original is used as is."""

    pages: list[bytes]
    # The number of the first page where splitting stopped (from 1) and why. Both False for the page-count limit (`MAX_TIFF_PAGES`),
    # unreadable for a corrupted or too-large page, too_many_pixels for the total of the pages: their pixels (`_MAX_TIFF_PIXELS`)
    # or the bytes of the pages kept (`split_tiff_pages`'s `max_bytes`).
    stopped_at: int | None = None
    unreadable: bool = False
    too_many_pixels: bool = False


@dataclass(frozen=True, slots=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    # Tiles of a very tall picture cut top → bottom (left → right if it is wide) (`_tile_boxes`). An empty tuple for an ordinary picture.
    tiles: tuple[bytes, ...] = ()


class ImageSkipped(Exception):
    """An image to drop (small, or a divider). Its placeholder is removed from the body."""


class ImageUnreadable(Exception):
    """An image that could not be decoded (WMF/EMF vectors, a broken file). Placeholder text is left in the body."""


class ImageOutOfMemory(ImageUnreadable):
    """A picture inside a document that could not be read in the memory there is (a `MemoryError` from `prepare_image`). The
    converter leaves it as placeholder text with a warning of its own."""


class ImageTooLarge(ImageUnreadable):
    """An image not read because it is over the decode pixel limit (`_MAX_DECODE_PIXELS`). An uploaded picture is reported as 'document too large'."""


def prepare_image(data: bytes, *, min_side: int, max_side: int, filter_decorations: bool = True) -> PreparedImage:
    """`filter_decorations=False` is for an image the user uploaded directly — it is not dropped even if small or thin."""
    try:
        image = _decoded(data, max_side)
    except (ImageUnreadable, MemoryError):
        # Running out of memory is not a broken picture: it goes up as it is, and the converter reports an uploaded picture as
        # too large or too complex and leaves a picture inside a document as placeholder text with a warning that says so.
        # Taken for an unreadable picture, an uploaded photo was 'corrupted' and a picture inside a document got the warning
        # about its format (Pinned by: test_image.py::TestOutOfMemoryWhileReadingAPicture).
        raise
    except Exception as exc:  # Pillow raises all sorts of exceptions on a corrupted file (SyntaxError, struct.error, ...).
        # A decoding failure is an "unreadable image" whatever its type (MemoryError aside). If it leaked, convert would die with
        # an exception outside ConversionError (Pinned by: test_image.py::test_broken_png_is_unreadable_not_a_crash).
        image = _truncated_jpeg(data, max_side)
        if image is None:
            raise ImageUnreadable(type(exc).__name__) from exc

    if filter_decorations and is_decoration(*image.size, min_side=min_side):
        raise ImageSkipped

    try:
        if image.mode.startswith("I;16"):
            # Pillow's downscaling does not support 16-bit grayscale and raised a ValueError. Reduce it to 8-bit grayscale
            # (Pinned by: test_image.py::TestPrepareImage::test_high_bit_depth_grayscale_is_converted_not_a_crash).
            # A transparent level is looked for before that, among the 16-bit values (`_at_level`).
            level = image.info.pop("transparency", None)
            transparent = _at_level(image, level) if isinstance(level, int) and 0 <= level <= 0xFFFF else None
            image = image.convert("I").point(lambda value: value / 256).convert("L")
            if transparent is not None:
                image.putalpha(ImageChops.invert(transparent))
        elif image.mode in ("I", "F"):
            image.info.pop("transparency", None)   # a level of the file, not of the stretched picture
            image = _stretch_to_8bit(image)
        image = _with_its_key_as_alpha(image)
        image = _blendable(image)

        # Any alpha band (RGBA, LA, PA, premultiplied RGBa as a HEIF may hold it) or a transparent palette entry
        # (`transparency`) keeps the picture transparent; the transparent grey level or colour of an L or RGB PNG is an alpha
        # band by now. Only RGBA, LA and P were looked at, so a PA TIFF became a JPEG with its transparent background turned
        # black (Pinned by: test_image.py::TestTransparencyOfEveryMode). The `A` of a Lab picture (bands L, A, B) is a colour
        # axis, not alpha: a Lab photo came out as a PNG several times the size of its JPEG
        # (Pinned by: test_image.py::TestTransparencyOfEveryMode::test_lab_picture_is_opaque).
        bands = image.getbands()
        has_alpha = (("A" in bands and image.mode != "LAB") or "a" in bands) or "transparency" in image.info
        as_it_is = _png_as_it_is(data, image, max_side)
        if as_it_is is not None:
            return PreparedImage(as_it_is, "image/png", *image.size)
        tiles = tuple(_encoded(image.crop(box), max_side, has_alpha)[0] for box in _tile_boxes(*image.size, max_side))
        data, mime_type, (shrunk_width, shrunk_height) = _encoded(image, max_side, has_alpha)
    except MemoryError:
        raise
    except Exception as exc:  # a failed downscale or re-encode is also an "unreadable image", like a failed decode (same reason)
        raise ImageUnreadable(type(exc).__name__) from exc
    return PreparedImage(data, mime_type, shrunk_width, shrunk_height, tiles)


def is_decoration(width: int, height: int, *, min_side: int) -> bool:
    """Whether a picture of this size is one `prepare_image` drops as decoration (`ImageSkipped`): a short side under
    `min_side` (an icon, a bullet), or an aspect ratio over `_MAX_ASPECT_RATIO` with a short side under `_CONTENT_SHORT_SIDE`
    (a divider, a piece of border). The converter asks it of the size in the header too (`header_size`)."""
    short, long = min(width, height), max(width, height)
    return short < min_side or (long / max(short, 1) > _MAX_ASPECT_RATIO and short < _CONTENT_SHORT_SIDE)


def header_size(data: bytes, max_side: int) -> tuple[int, int] | None:
    """The width and height `prepare_image` decodes the picture at, from its header alone: no pixel is decoded. None for a
    picture it cannot open (WMF/EMF, a HEIC without its extra, garbage, a format it does not read) or that is over the decode
    limit (`ImageTooLarge`) — those `prepare_image` cannot decode either. A JPEG over the limit has the size it is read reduced
    at (`_open_within_pixel_limit`); turning it by its EXIF orientation swaps the sides, which `is_decoration` does not mind.
    A file whose header reads but whose pixels are broken is a size here and unreadable for `prepare_image`."""
    try:
        with quiet_warnings(*_PILLOW_WARNINGS):
            opened = _open_within_pixel_limit(data, max_side)
    except Exception:  # noqa: BLE001 — Pillow raises all sorts of exceptions on a file it cannot open, as in `prepare_image`
        return None
    try:
        return opened.size
    finally:
        opened.close()

def _decoded(data: bytes, max_side: int) -> Image.Image:
    """The picture, decoded and with its orientation (EXIF) corrected.

    Deliberate: the opened picture itself is returned, turned in place, and it is not closed (closing drops its pixels; the
    file behind it is a `BytesIO`). `exif_transpose` otherwise returns a copy, and with the copy `_encoded` used to make
    there were three full-size pictures in memory at the peak. A 13000×13000 PNG, just under the pixel limit, peaked at
    2.1 GiB as RGBA (the whole of a worker's default limit) and 1.5 GiB as RGB; now at 1.5 GiB (Pillow's downscale of a
    transparent picture makes one premultiplied copy) and 0.9 GiB. The bytes that come out are the same
    (Pinned by: test_image.py::TestOnePictureInMemory)."""
    with quiet_warnings(*_PILLOW_WARNINGS):  # thread-safe (`models.quiet_warnings`)
        opened = _open_within_pixel_limit(data, max_side)
        _key_on_the_scale_of_the_pixels(opened)
        opened.seek(0)  # first frame only for an animated GIF/AVIF. A multi-page TIFF is split per page by the extractor first (split_tiff_pages)
        ImageOps.exif_transpose(opened, in_place=True)
        opened.load()
    return opened


def _key_on_the_scale_of_the_pixels(opened: Image.Image) -> None:
    """Rewrite the transparent colour of a PNG (`info["transparency"]`, the tRNS chunk) from the scale of the file to the
    scale of the pixels Pillow hands out. Called before the picture is loaded, while the raw mode of the file is known.

    Pillow keeps the key as the file writes it and compares its low byte with the 8-bit pixels: the key (300, 0, 0) of a
    16-bit RGB PNG made every pixel with red 44 transparent and none of those the file meant, and white (15) of a 4-bit grey
    PNG made nothing transparent. A 16-bit colour is cut to its high bytes, as the pixels are (Known accepted gap: pixels
    that differ from the key in the low bytes only are transparent too — Pillow does not hand those bytes out). A grey level
    of 1, 2 or 4 bits is multiplied up, unless it is on the 0–255 scale already (Pillow 12 and later do that for 1 bit).
    The level of a 16-bit grey PNG stays as it is: those pixels keep their 16 bits (`_at_level`)
    (Pinned by: test_image.py::TestTransparentColourOfAPng)."""
    key = opened.info.get("transparency")
    if opened.format != "PNG" or key is None or isinstance(key, bytes):
        return
    arguments = opened.tile[0][3] if opened.tile and len(opened.tile[0]) > 3 else None
    raw_mode = arguments if isinstance(arguments, str) else ""
    if isinstance(key, tuple):
        if raw_mode == "RGB;16B" or (not raw_mode and any(isinstance(value, int) and value > 255 for value in key)):
            opened.info["transparency"] = tuple(value >> 8 if isinstance(value, int) else value for value in key)
    elif isinstance(key, int) and raw_mode in _KEY_SCALE and 0 <= key * _KEY_SCALE[raw_mode] <= 255:
        opened.info["transparency"] = key * _KEY_SCALE[raw_mode]


def _at_level(image: Image.Image, level: int) -> Image.Image:
    """An `L` picture that is 255 where the 16-bit grey `image` is exactly `level`, 0 elsewhere.

    The transparent level of a 16-bit grey PNG is one of 65,536. Applied after the reduction to 8 bits (Pillow compares its
    low byte), level 300 made the pixels of 8-bit level 44 transparent instead of the ones at 300, 65535 none at all, and 0
    all of the levels 0 to 255 (Pinned by: test_image.py::TestTransparentColourOfAPng::test_level_of_a_16_bit_grey_picture).
    The two bytes of every pixel are compared as two 8-bit pictures: Pillow has no lookup for 16-bit pixels."""
    raw = image.tobytes()
    big_endian = image.mode == "I;16B" or (image.mode == "I;16N" and sys.byteorder == "big")
    low, high = (raw[1::2], raw[0::2]) if big_endian else (raw[0::2], raw[1::2])
    low_is = Image.frombytes("L", image.size, low).point([255 if value == level & 0xFF else 0 for value in range(256)])
    high_is = Image.frombytes("L", image.size, high).point([255 if value == level >> 8 else 0 for value in range(256)])
    return ImageChops.darker(low_is, high_is)


def _with_its_key_as_alpha(image: Image.Image) -> Image.Image:
    """An `L`, `1` or `RGB` picture with a transparent grey level or colour (`info["transparency"]`) as `LA` or `RGBA`, with
    exactly the pixels of that colour transparent; any other picture as it is. A key no pixel can have (out of range, or
    not what the mode takes) is dropped and the picture is opaque.

    Done before the downscale: shrunk first, the edge of a keyed background was a blend of the key colour and the picture —
    no longer the key, so it stayed, opaque: a magenta-keyed 4000×4000 PNG kept 9,106 magenta pixels around its subject
    (Pinned by: test_image.py::TestTransparentColourOfAPng::test_keyed_background_leaves_no_fringe_when_downscaled).
    A palette picture keeps its `transparency`: it becomes an alpha band when the picture is made `RGBA` before the
    downscale (`_blendable`), exactly the pixels of the transparent entries."""
    key = image.info.get("transparency")
    if key is None or image.mode not in ("1", "L", "RGB"):
        return image
    if image.mode == "RGB":
        valid = isinstance(key, tuple) and len(key) == 3 and all(isinstance(value, int) and 0 <= value <= 255 for value in key)
    else:   # a 1-bit picture is 0 and 255, and its key is one of them by now (`_key_on_the_scale_of_the_pixels`)
        valid = isinstance(key, int) and not isinstance(key, bool) and (0 <= key <= 255 if image.mode == "L" else key in (0, 255))
    if not valid:
        del image.info["transparency"]
        return image
    return (image.convert("L") if image.mode == "1" else image).convert("RGBA" if image.mode == "RGB" else "LA")


def _blendable(image: Image.Image) -> Image.Image:
    """A 1-bit picture as `L`, a palette picture as `RGB`, or `RGBA` when it has a transparent entry or an alpha band (`PA`);
    any other picture as it is. Done before the downscale and the tiles.

    Pillow shrinks a picture of mode `1`, `P` or `PA` by nearest neighbour whatever filter it is asked for: a row either was
    kept or vanished, so the 2 px rules of a bilevel fax page shrunk to 0.29 were there 58 times out of 100, and text lost
    its thin strokes (Pinned by: test_image.py::TestBilevelAndPalettePicturesShrunkWithBlending). As `L`, `RGB` or `RGBA`
    it is blended (LANCZOS); a transparent entry is an alpha band by then, so its colour is never blended into the edge.
    The written picture is the same mode as before (`_encoded` converts to RGB or RGBA)."""
    if image.mode == "1":
        return image.convert("L")
    if image.mode == "PA" or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA")
    if image.mode == "P":
        return image.convert("RGB")
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
    except MemoryError:
        raise
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

    `image` itself is shrunk (no copy is made first, see `_decoded`): it is a tile, or the whole picture at its last use.
    """
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    output = BytesIO()
    if has_alpha:
        image.convert("RGBA").save(output, format="PNG")
        return output.getvalue(), "image/png", image.size
    image.convert("RGB").save(output, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return output.getvalue(), "image/jpeg", image.size


def _png_as_it_is(data: bytes, image: Image.Image, max_side: int) -> bytes | None:
    """The file of an 8-bit RGBA PNG that needs no change, with only the chunks Pillow's own rewrite of it has (`IHDR`, `iCCP`
    when Pillow read the profile, the `IDAT` picture data, `IEND`); None when the picture has to be rewritten (`_encoded`).

    Deliberate: such a picture is handed on without being compressed again. Rewriting it was the PNG compression of
    `_encoded`, most of the conversion time of a picture-heavy document: a real HWP with 51 pictures took 550 ms with
    pictures decoded against 25 ms without, 420 ms of it in `Image.save`; a HWPX of 24 opaque RGBA photos of 1500×1000 took
    2.2 s and takes 0.36 s. The picture is still decoded (a broken one is unreadable as before, and its size decides what is
    dropped as decoration). The pixels are the ones the rewrite would hold; text, EXIF, XMP, time and colour-space chunks
    are left out as the rewrite left them out (Pinned by: test_image.py::TestPngPassedOnAsItIs).

    Only when nothing about the picture changes: colour type 6 at 8 bits (16 bits is reduced, a palette or grey picture is
    made RGBA), the long side within `max_side` (so no downscale and no tiles), no EXIF chunk and no text chunk that names
    EXIF or XMP (where Pillow reads an orientation from and turns the picture), no animation (`acTL`; the first frame is
    taken), no critical chunk this does not know, every chunk kept with its right CRC (a decoder stricter than Pillow refuses
    a picture data chunk with a wrong one), one `IHDR` and the `IDAT` chunks one after another (libpng refuses a picture
    with either out of place), picture data that is one zlib stream with nothing after it (bytes hidden behind the picture
    were dropped by the rewrite and must not be handed on) and that inflates to exactly the rows of the picture (a filter
    byte and 4 bytes a pixel a row, of each Adam7 pass when interlaced; more is hidden data or a zlib bomb, and inflating
    stops as soon as it goes past), and picture data at most half the raw pixels (a file written
    without or with little compression is compressed by the rewrite instead of handed on several times as large)."""
    width, height = image.size
    if image.mode != "RGBA" or max(width, height) > max_side or not data.startswith(_PNG_SIGNATURE):
        return None
    kept = [_PNG_SIGNATURE]
    position, picture_data, kind = len(_PNG_SIGNATURE), 0, b""
    rows = _png_rows_size(width, height, interlaced=data[28:29] == b"\x01")   # what the IDAT stream must inflate to
    stream = zlib.decompressobj()
    data_ended = False   # a chunk other than IDAT came after the picture data
    while kind != b"IEND":
        if position + 12 > len(data):
            return None
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            return None
        body = data[position + 8:end - 4]
        if position == len(_PNG_SIGNATURE) and (kind != b"IHDR" or length != 13 or body[:8] != struct.pack(">II", width, height)
                                                 or body[8:12] != b"\x08\x06\x00\x00" or body[12] > 1):
            return None
        if kind in (b"eXIf", b"acTL") or not (kind[0] & 0x20 or kind in (b"IHDR", b"PLTE", b"IDAT", b"IEND")):
            return None
        if (kind == b"IHDR" and position != len(_PNG_SIGNATURE)) or (kind == b"IDAT" and data_ended):
            return None
        if kind == b"IDAT":
            # The rows inflate to exactly `rows` bytes, thrown away a piece at a time: a stream holding more is refused as soon
            # as it goes past, so a few MB of zeros after the rows (8 GB inflated) are neither inflated whole nor handed on
            # (Pinned by: test_image.py::TestPngPassedOnAsItIs::test_picture_data_that_inflates_past_its_rows_is_written_anew).
            try:
                rows -= len(stream.decompress(body, min(rows + 1, 1 << 20)))
                while stream.unconsumed_tail and rows >= 0:
                    rows -= len(stream.decompress(stream.unconsumed_tail, min(rows + 1, 1 << 20)))
            except zlib.error:
                return None
            if stream.unused_data or rows < 0:
                return None
        elif picture_data:
            data_ended = True
        if kind in (b"tEXt", b"zTXt", b"iTXt") and any(name in body.split(b"\0", 1)[0].lower() for name in (b"exif", b"xmp")):
            return None
        if kind in (b"IHDR", b"IDAT", b"IEND") or (kind == b"iCCP" and "icc_profile" in image.info):
            if zlib.crc32(kind + body) != int.from_bytes(data[end - 4:end], "big"):
                return None
            kept.append(data[position:end])
            picture_data += length if kind == b"IDAT" else 0
        position = end
    if picture_data > width * height * 4 // 2 or not stream.eof or stream.unused_data or rows:
        return None
    return b"".join(kept)


# Where each of the seven Adam7 passes starts and how far apart its pixels are: (x0, y0, dx, dy)
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _png_rows_size(width: int, height: int, *, interlaced: bool) -> int:
    """The bytes the picture data of an 8-bit RGBA PNG inflates to: a filter byte and 4 bytes a pixel for every row, of
    each Adam7 pass that holds pixels when interlaced."""
    if not interlaced:
        return height * (1 + width * 4)
    size = 0
    for x0, y0, dx, dy in _ADAM7:
        pass_width, pass_height = -(-(width - x0) // dx), -(-(height - y0) // dy)
        if pass_width > 0 and pass_height > 0:
            size += pass_height * (1 + pass_width * 4)
    return size


def _tile_boxes(width: int, height: int, max_side: int) -> list[tuple[int, int, int, int]]:
    """Where the tiles of a very tall picture (`_TILE_ASPECT_RATIO`) are cut, in order along the long side (crop boxes). An empty list
    if the picture is not one to split.

    Shrinking the whole thing to a long side of `max_side` turned an 860×18000 product detail page into 98×2048 and the text
    was unreadable (Pinned by: test_core_image_and_assembly.py::TestTallPictures). A tile is twice the short side long (there
    are at most `_MAX_TILES`, so a longer picture gets longer tiles) and overlaps its neighbors by 1/16 so that a line of text
    on a boundary is whole in at least one tile. Deliberate: the downscaled whole picture (`data`) is kept — a describer that
    knows nothing about tiles sees it as before.

    Only the boxes: `prepare_image` cuts, encodes and lets go of one tile before it cuts the next. Each crop is a full-resolution
    copy, and cutting them all before the first was encoded held the whole picture over again (a 3000×40000 PNG peaked at
    1,124 MB instead of 710 MB). The bytes that come out are the same
    (Pinned by: test_image.py::TestOnePictureInMemory::test_tiles_are_cut_one_at_a_time).
    """
    short, long = min(width, height), max(width, height)
    if long <= _TILE_ASPECT_RATIO * short or long <= 2 * max_side:
        return []

    count = min(_MAX_TILES, -(-long // (2 * short)))
    length = -(-long // count)
    overlap = length // 16
    boxes = []

    for index in range(count):
        start, end = max(index * length - overlap, 0), min((index + 1) * length + overlap, long)
        boxes.append((0, start, width, end) if height > width else (start, 0, end, height))
    return boxes


def _open_within_pixel_limit(data: bytes, max_side: int) -> Image.Image:
    """Open so that at most `_MAX_DECODE_PIXELS` pixels are decoded. A JPEG over it is read reduced to 1/2–1/8 at decode time (draft).

    Normal photos over Pillow's decompression-bomb limit (the same value), such as panoramas and large scans, were rejected as a
    'corrupted image' (Pinned by: test_core_image_and_assembly.py::TestVeryLargePictures). A baseline JPEG is decoded reduced at
    the DCT stage, so it uses only the memory of the reduced size — the over-limit original is never decoded whole. Image.open
    rejects it in its limit check, so the JPEG decoder is opened directly (global settings such as `MAX_IMAGE_PIXELS` are not
    changed). If it is still over the limit after reduction, or the format cannot be read reduced (PNG, TIFF, a progressive
    JPEG, ...), raise `ImageTooLarge`.
    """
    try:
        opened = Image.open(BytesIO(data), formats=_OPEN_FORMATS)
    except Image.DecompressionBombError:
        opened = None
    if opened is not None and opened.width * opened.height <= _MAX_DECODE_PIXELS:
        return opened
    if opened is not None:
        opened.close()
    if not data.startswith(_JPEG_MAGIC):
        raise ImageTooLarge("image too large")

    jpeg = JpegImagePlugin.JpegImageFile(BytesIO(data))
    # Deliberate: a progressive JPEG over the limit is not read. Its decoder keeps the coefficients of the whole picture in
    # memory whatever the reduction, so the limit on the reduced size does not bound it: a 20000×20000 one (1.5 MB) took 946 MB
    # where a baseline one took 148 MB
    # (Pinned by: test_audit_runtime.py::TestProgressiveJpegOverThePixelLimit::test_progressive_jpeg_over_the_limit_is_too_large).
    if jpeg.info.get("progressive") or jpeg.info.get("progression"):
        jpeg.close()
        raise ImageTooLarge("image too large")
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


def split_tiff_pages(data: bytes, max_bytes: int | None = None) -> TiffPages:
    """Split a multi-page TIFF (a fax or a bundle of scans) into one single-page TIFF (lossless deflate) per page.

    Only the first page was decoded and the other pages vanished without a warning (Pinned by: test_image.py::TestMultiPageTiff).
    Each page has to be registered as its own picture for the describer (OCR) to see every page. `_MAX_DECODE_PIXELS` is kept
    per page, and on meeting a corrupted or too-large page only the pages before it are returned. The frames of an animated GIF
    are not pages, so they are not split (Deliberate). Pages from the one where the total page pixels go over `_MAX_TIFF_PIXELS`
    are not read (Pinned by: test_core_front_cut_and_tiff_pixels.py::TestMultiPageTiffPixelTotal).

    The pages kept count toward `max_bytes` (the document's `max_uncompressed_bytes`), as decompressed and prepared pictures do:
    a lossless page of a photographic scan takes many times its JPEG in the file, and a 48 MB TIFF of 28 colour pages held 691 MB
    of pages (1.7 GB at its peak), past any limit. The pages before the one that goes over are kept
    (Pinned by: test_image.py::TestMultiPageTiff::test_pages_kept_are_bounded_by_the_uncompressed_limit).
    """
    if not data.startswith(_TIFF_MAGIC):
        return TiffPages([])
    pages: list[bytes] = []
    pixels = kept = 0
    try:
        # EXIF warnings from a corrupted later page and the like (Pillow's UserWarning). The outcome (the page it stopped at) is
        # reported in the warning text. This used to ignore every warning — the thread-safe `models.quiet_warnings` applies to the
        # whole process for the duration of the block, so it was narrowed to Pillow warnings
        with quiet_warnings(*_PILLOW_WARNINGS):
            with Image.open(BytesIO(data), formats=_OPEN_FORMATS) as opened:
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
                    kept += output.tell()
                    if max_bytes is not None and kept > max_bytes and pages:
                        return TiffPages(pages, len(pages) + 1, too_many_pixels=True)
                    pages.append(output.getvalue())
                more = _has_frame(opened, MAX_TIFF_PAGES)
    except MemoryError:
        raise
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

    In a float picture the range is that of the finite values; NaN and minus infinity are black (0), plus infinity white
    (255). A NaN (the 'no data' of a GeoTIFF) made the range NaN and one +inf made the scale 0: either way the whole picture
    came out black (Pinned by: test_image.py::TestAuditedImageGaps::test_float_picture_with_nan_or_infinity_keeps_its_gradient).
    The masks and the range are made with Pillow's own operations, not a loop over the pixels in Python.
    """
    not_finite = _where(image, lambda value: (value != value) + (abs(value) == math.inf)) if image.mode == "F" else None
    if not_finite is not None and not_finite.getbbox() is None:
        not_finite = None
    if not_finite is None:
        low, high = image.getextrema()
    else:
        above = _where(image, lambda value: value == math.inf)
        if ImageChops.invert(not_finite).getbbox() is None:   # not one finite value
            return above
        # The finite extremes: the other pixels set first to the largest float, then to the smallest, then to the lowest finite
        # value, so that the stretch below sees finite numbers only. The picture is the decoder's own, changed in place.
        image.paste(_FLOAT32_MAX, mask=not_finite)
        low = image.getextrema()[0]
        image.paste(-_FLOAT32_MAX, mask=not_finite)
        high = image.getextrema()[1]
        image.paste(low, mask=not_finite)
    if high > low:
        scale = 255 / (high - low)
        image = image.convert("F").point(lambda value: value * scale - low * scale)
    else:
        level = min(max(low, 0), 255)
        image = image.convert("F").point(lambda value: value * 0 + level)

    image = image.convert("L")
    if not_finite is not None:
        image.paste(0, mask=not_finite)
        image.paste(255, mask=above)
    return image


# The largest finite 32-bit float: what the pixels that are not finite are set to while the finite extremes are looked for.
_FLOAT32_MAX = 3.4028234663852886e38


def _where(image: Image.Image, test: Callable[[Any], Any]) -> Image.Image:
    """An `L` mask of the float picture `image`, 255 where `test` holds for the pixel and 0 elsewhere (Pillow's `ImageMath`)."""
    found = ImageMath.lambda_eval(lambda names: test(names["value"]), value=image)
    return found.convert("L").point([0] + [255] * 255)


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
