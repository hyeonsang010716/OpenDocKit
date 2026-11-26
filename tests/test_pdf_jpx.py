"""JPEG 2000 pictures that stop the process when pdfium draws them (`_pdf_jpx.stops_the_renderer`)."""

import io
import subprocess
import sys
import zlib

import pytest

from opendockit import convert
from opendockit.extractors import _pdf_jpx
from opendockit.extractors._pdf_jpx import _OBJECT_HEADER, _holder, stops_the_renderer


def _picture(mode: str, jp2: bool = False, colour: int | None = None) -> bytes:
    """A 16 x 16 JPEG 2000 picture of the Pillow mode (L 1 component, LA 2, RGB 3, RGBA and CMYK 4), as a codestream or a JP2
    file; `colour` writes another enumerated colour space into its colour box."""
    from PIL import Image

    output = io.BytesIO()
    Image.new(mode, (16, 16), (0, 0, 0, 0) if mode == "CMYK" else "white").save(output, "JPEG2000", no_jp2=not jp2)
    data = output.getvalue()
    if colour is not None:
        at = data.index(b"colr") + 4
        assert data[at] == 1  # an enumerated colour space
        data = data[:at + 3] + colour.to_bytes(4, "big") + data[at + 7:]
    return data


def _box(kind: bytes, body: bytes) -> bytes:
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def _with_boxes(jp2: bytes, added: bytes = b"", colour_first: bytes = b"", second_colour: int | None = None) -> bytes:
    """A JP2 file with boxes added to its header box: `added` after the boxes Pillow wrote, `colour_first` before its
    colour box; `second_colour` rewrites the enumerated space of Pillow's own colour box when another comes first."""
    at = jp2.index(b"jp2h") - 4
    length = int.from_bytes(jp2[at:at + 4], "big")
    body = jp2[at + 8:at + length]
    if colour_first:
        colour = body.index(b"colr") - 4
        body = body[:colour] + colour_first + body[colour:]
        if second_colour is not None:
            own = body.index(b"colr", colour + len(colour_first)) + 4
            body = body[:own + 3] + second_colour.to_bytes(4, "big") + body[own + 7:]
    return jp2[:at] + _box(b"jp2h", body + added) + jp2[at + length:]


# A palette of two entries and three columns, the component map that applies it, and a colour box naming `space`
_PALETTE = _box(b"pclr", (2).to_bytes(2, "big") + bytes([3, 7, 7, 7, 0, 0, 0, 255, 255, 255]))
_MAP = _box(b"cmap", b"".join((0).to_bytes(2, "big") + bytes([1, column]) for column in range(3)))


def _colour(space: int) -> bytes:
    return _box(b"colr", bytes([1, 0, 0]) + space.to_bytes(4, "big"))


def _pdf(picture: bytes, space: str | None = None, *, mask: bool = False, flate: bool = False, in_form: bool = False,
         resources: str = "", objects: tuple[bytes, ...] = (), filter_name: str = "/JPXDecode") -> bytes:
    """A one-page PDF that draws `picture` (JPXDecode) with the colour space written as `space` (PDF text; `objects` are
    written as objects 6, 7, … for it to refer to), as an image mask, under FlateDecode, or from a form XObject."""
    numbered = [b""] * 5 + list(objects)
    data = zlib.compress(picture) if flate else picture
    filters = f"[/FlateDecode {filter_name}]" if flate else filter_name
    entries = (f"/ColorSpace {space} " if space else "") + ("/ImageMask true " if mask else "")
    numbered[4] = (f"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 /Filter {filters} {entries}"
                   f"/Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream"
    drawn = b"q 200 0 0 200 50 50 cm /Im0 Do Q"
    xobjects = "/Im0 5 0 R"
    if in_form:
        numbered.append(f"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /Im0 5 0 R >> >> "
                        f"/Length {len(drawn)} >>\nstream\n".encode() + drawn + b"\nendstream")
        xobjects, drawn = f"/Fm0 {len(numbered)} 0 R", b"q /Fm0 Do Q"
    numbered[3] = f"<< /Length {len(drawn)} >>\nstream\n".encode() + drawn + b"\nendstream"
    numbered[2] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /XObject << {xobjects} >> {resources} >> "
                   "/Contents 4 0 R >>").encode()
    numbered[1] = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    numbered[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    pdf, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(numbered, start=1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(pdf)
    pdf += f"xref\n0 {len(numbered) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    pdf += f"trailer\n<< /Size {len(numbered) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    return bytes(pdf)


def _stream(dictionary: str, body: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream"


# A DeviceN space of `n` colorants (object 6 is its tint transform) and an ICC profile of `n` components (object 6)
def _device_n(n: int) -> tuple[str, tuple[bytes, ...]]:
    domain = " ".join(["0 1"] * n)
    return (f"[/DeviceN [{' '.join(f'/C{index}' for index in range(n))}] /DeviceRGB 6 0 R]",
            (_stream(f"/FunctionType 4 /Domain [{domain}] /Range [0 1 0 1 0 1]", b"{ pop }"),))


def _icc(n: int) -> tuple[str, tuple[bytes, ...]]:
    return "[/ICCBased 6 0 R]", (_stream(f"/N {n}", b"\x00" * 128),)


_WHITE = "<< /WhitePoint [0.95 1 1.09] >>"

# Each case was drawn by pdfium 159 in a process of its own: True where that process was stopped
_CASES = {
    "DeviceN of 4 over 2 components": (lambda: _pdf(_picture("LA"), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 4 over 3 components": (lambda: _pdf(_picture("RGB"), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 2 over 2 components": (lambda: _pdf(_picture("LA"), _device_n(2)[0], objects=_device_n(2)[1]), False),
    "DeviceN of 4 over 4 components": (lambda: _pdf(_picture("RGBA"), _device_n(4)[0], objects=_device_n(4)[1]), False),
    "ICC of 3 over 1 component": (lambda: _pdf(_picture("L"), _icc(3)[0], objects=_icc(3)[1]), True),
    "ICC of 4 over 3 components": (lambda: _pdf(_picture("RGB"), _icc(4)[0], objects=_icc(4)[1]), True),
    "ICC of 3 over 3 components": (lambda: _pdf(_picture("RGB"), _icc(3)[0], objects=_icc(3)[1]), False),
    "Lab over 2 components": (lambda: _pdf(_picture("LA"), f"[/Lab {_WHITE}]"), True),
    "CalRGB over 1 component": (lambda: _pdf(_picture("L"), f"[/CalRGB {_WHITE}]"), True),
    "CalGray over 1 component": (lambda: _pdf(_picture("L"), f"[/CalGray {_WHITE}]"), False),
    "Indexed over 1 component": (lambda: _pdf(_picture("L"), "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"), False),
    "DeviceGray over 3 components": (lambda: _pdf(_picture("RGB"), "/DeviceGray"), False),
    # pdfium takes a picture of one or two components for gray and refuses it as RGB, or as CMYK
    "DeviceRGB over 1 component": (lambda: _pdf(_picture("L"), "/DeviceRGB"), False),
    "DeviceRGB over 2 components": (lambda: _pdf(_picture("LA"), "/DeviceRGB"), False),
    "DeviceCMYK over 2 components": (lambda: _pdf(_picture("LA"), "/DeviceCMYK"), False),
    "DeviceCMYK over 3 components": (lambda: _pdf(_picture("RGB"), "/DeviceCMYK"), True),
    "DeviceCMYK over 3 components named sRGB": (lambda: _pdf(_picture("RGB", jp2=True, colour=16), "/DeviceCMYK"), False),
    "DeviceCMYK over 4 components": (lambda: _pdf(_picture("CMYK"), "/DeviceCMYK"), False),
    # a default space in the resources makes DeviceRGB a space of three components that is not refused
    "DeviceRGB under DefaultRGB over 2 components": (
        lambda: _pdf(_picture("LA"), "/DeviceRGB", resources="/ColorSpace << /DefaultRGB [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 3", b"\x00" * 128),)), True),
    # no colour space: pdfium takes the one the picture names
    "no space, 2 components": (lambda: _pdf(_picture("LA")), False),
    "no space, 3 components": (lambda: _pdf(_picture("RGB")), False),
    "no space, JP2 CMYK over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=12)), True),
    "no space, JP2 CMYK over 1 component": (lambda: _pdf(_picture("L", jp2=True, colour=12)), False),
    "no space, JP2 sRGB over 1 component": (lambda: _pdf(_picture("L", jp2=True, colour=16)), False),
    "no space, JP2 gray over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=17)), False),
    # an image mask is read as a picture without a space, and stops pdfium when that is gray
    "mask of 1 component": (lambda: _pdf(_picture("L"), mask=True), True),
    "mask of 2 components": (lambda: _pdf(_picture("LA"), mask=True), True),
    "mask of 2 components with a DeviceN space": (lambda: _pdf(_picture("LA"), _device_n(4)[0], mask=True, objects=_device_n(4)[1]), True),
    "mask of JP2 gray over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=17), mask=True), True),
    "mask of JP2 CMYK over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=12), mask=True), True),
    "mask of 3 components": (lambda: _pdf(_picture("RGB"), mask=True), False),
    "mask of 4 components": (lambda: _pdf(_picture("RGBA"), mask=True), False),
    # openjpeg applies a palette when a component map comes with it, and pdfium does not ask it to for an Indexed space;
    # of two colour boxes the first counts
    "DeviceN of 3 over a palette of 3 columns": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), _device_n(3)[0], objects=_device_n(3)[1]), False),
    "DeviceN of 4 over a palette of 3 columns": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 3 over a palette without a map": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE), _device_n(3)[0], objects=_device_n(3)[1]), True),
    "Indexed over a palette": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"), False),
    "no space, sRGB box before a CMYK one, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_colour(16), second_colour=12)), False),
    "no space, CMYK box before an sRGB one, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_colour(12), second_colour=16)), True),
    "mask with a profile for colour box, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_box(b"colr", bytes([2, 0, 0]) + b"\x00" * 128)), mask=True),
        False),
    # wherever the picture is drawn from, and however its filter is written
    "drawn from a form": (lambda: _pdf(_picture("LA"), _device_n(4)[0], in_form=True, objects=_device_n(4)[1]), True),
    "under FlateDecode": (lambda: _pdf(_picture("LA"), _device_n(4)[0], flate=True, objects=_device_n(4)[1]), True),
    "name written with #hh": (lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="/J#50X#44ecode", objects=_device_n(4)[1]), True),
    "drawn from a form, safe": (lambda: _pdf(_picture("LA"), _device_n(2)[0], in_form=True, objects=_device_n(2)[1]), False),
}


class TestTheRendererStops:
    """pdfium asks the JPEG 2000 decoder for as many components as the colour space it takes for the picture has, and a
    check ends the process when that is more than the picture holds; an image mask it takes for gray is read past the end
    of a list, which libc++ ends. The PDF.js test file `jpx_numcomps.pdf` (10 KB, six pictures, one of them a DeviceN of
    four colorants over two components) ended every program that converted it in its own process."""

    @pytest.mark.parametrize("case", list(_CASES))
    def test_each_case_is_judged_as_pdfium_draws_it(self, case):
        build, stops = _CASES[case]

        assert stops_the_renderer(build()) is stops

    def test_a_document_that_stops_the_renderer_is_converted_without_its_pictures(self):
        """Converted in a process of its own: before, that process ended with SIGTRAP (SIGILL on x86)."""
        stopping = _pdf(_picture("LA"), _device_n(4)[0], objects=_device_n(4)[1])
        script = ("import sys; from opendockit import convert; result = convert(sys.stdin.buffer.read(), 'a.pdf'); "
                  "print(len(result.images)); print(*result.warnings, sep='\\n')")

        done = subprocess.run([sys.executable, "-c", script], input=stopping, capture_output=True, timeout=120, check=False)

        assert done.returncode == 0, done.stderr[-500:]
        lines = done.stdout.decode().splitlines()
        assert lines[0] == "0"
        assert "1 picture(s) of the PDF were not extracted: it holds a JPEG 2000 picture that stops the PDF renderer when drawn." in lines

    def test_a_picture_that_does_not_stop_it_is_extracted(self):
        result = convert(_pdf(_picture("LA"), _device_n(2)[0], objects=_device_n(2)[1]), "a.pdf")

        assert len(result.images) == 1 and not any("JPEG 2000" in warning for warning in result.warnings)


class TestWhatIsNotJudged:
    def test_a_file_without_the_name_is_not_opened(self, monkeypatch):
        """The search for the name decides almost every file; pdfminer opens only one that holds it."""
        monkeypatch.setattr(_pdf_jpx, "PDFDocument", lambda *args: pytest.fail("opened"))

        assert stops_the_renderer(_pdf(_picture("LA"), "/DeviceRGB").replace(b"/JPXDecode", b"/DCTDecode")) is False

    def test_a_name_inside_a_page_content_is_no_picture_pdfium_decodes(self):
        """pdfium does not decode a JPEG 2000 picture written inline in a content stream."""
        drawn = b"BI /W 1 /H 1 /BPC 8 /F /JPXDecode /CS /DeviceRGB ID x EI"
        data = _pdf(_picture("RGB"), "/DeviceRGB").replace(b"/JPXDecode", b"/DCTDecode")
        data = data.replace(b"<< /Length 32 >>\nstream\nq 200 0 0 200 50 50 cm /Im0 Do Q",
                            f"<< /Length {len(drawn)} >>\nstream\n".encode() + drawn)

        assert b"/JPXDecode" in data and stops_the_renderer(data) is False

    def test_what_cannot_be_read_counts_as_stopping_it(self):
        """A header that is not JPEG 2000, a colour space named in resources the guard does not look up, a file pdfminer
        cannot open, a name before any object, an object pdfminer cannot read."""
        assert stops_the_renderer(_pdf(b"\x00" * 64, "/DeviceRGB")) is True
        assert stops_the_renderer(_pdf(_picture("LA"), "/CS0", resources="/ColorSpace << /CS0 /DeviceGray >>")) is True
        assert stops_the_renderer(b"%PDF-1.7\n1 0 obj << /Filter /JPXDecode >> endobj") is True
        assert stops_the_renderer(b"/JPXDecode " + _pdf(_picture("RGB"), "/DeviceRGB")) is True
        data = _pdf(_picture("RGB"), "/DeviceRGB")
        assert stops_the_renderer(data.replace(b"%%EOF", b"99 0 obj\n<< /Filter /JPXDecode >>\nendobj\n%%EOF")) is True


class TestTheObjectOfAName:
    def test_the_header_is_found_far_before_the_name(self):
        data = b"12 0 obj\n<< " + b" " * 10_000 + b"/Filter /JPXDecode >>"

        assert _holder(data, data.index(b"/JPXDecode")) == 12

    def test_a_header_cut_by_the_start_of_the_stretch_is_not_read(self):
        data = b"x" * 5 + b"1234 0 obj"

        assert [match.group(1) for match in _OBJECT_HEADER.finditer(data, 7, len(data))] == []
        assert _holder(data, len(data)) == 1234
