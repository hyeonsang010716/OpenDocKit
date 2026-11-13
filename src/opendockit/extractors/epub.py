"""EPUB (2 and 3) → Markdown.

An EPUB is a ZIP of XHTML chapters. `META-INF/container.xml` names the package document (OPF); its manifest lists every file
and its spine gives the reading order. Each spine document is prepared (see `_Book._prepare`) and handed to the HTML
extractor, and the chapters are joined in spine order into one document section.

Elements of the package files are compared **by local name only**: EPUB 2 and 3 packages, and packages written without the
namespace, differ in nothing else that matters here.
"""

import base64
from dataclasses import dataclass
from io import BytesIO
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile
import zlib

from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError
from opendockit.extractors._shared import parse_part
from opendockit.extractors.html import extract_html
from opendockit.extractors.text import decode_text, decoding_warning
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, renumber_image_markers


_CONTAINER = "META-INF/container.xml"
_ENCRYPTION = "META-INF/encryption.xml"
_NCX_TYPE = "application/x-dtbncx+xml"
_CHAPTER_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_CHAPTER_EXTENSIONS = (".xhtml", ".html", ".htm")
# Sizes stored in the archive are not trusted: every part is read through a cap (the converter has already bounded the sum of
# the declared sizes, `converter._guard_zip`). A part over its cap is treated like a missing one.
_MAX_PACKAGE_BYTES = 16 * 1024 * 1024  # container.xml, the package document, encryption.xml
_MAX_CHAPTER_BYTES = 32 * 1024 * 1024
_MAX_PICTURE_BYTES = 20 * 1024 * 1024
# Pictures are handed to the HTML extractor inside the chapter (`_Book._inline`), and a picture referenced a thousand times is
# held a thousand times, so the sum is capped per book, counting every reference.
_MAX_INLINED_BYTES = 64 * 1024 * 1024
_MAX_SPINE_ITEMS = 5_000
_MAX_METADATA_CHARS = 500
_MAX_CREATORS = 20
_PICTURE_TYPE = re.compile(r"image/[A-Za-z0-9.+-]+\Z")
# Link targets that still mean something outside the book. Everything else points into the package.
_EXTERNAL_LINK = re.compile(r"\s*(?:https?:|mailto:)", re.IGNORECASE)
_DECLARED_CHARSET = re.compile(rb"<\?xml\b[^>]*?\bencoding\s*=\s*[\"']([\w.:+-]+)|<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([\w.:+-]+)",
                               re.IGNORECASE)
_CHARSET_PRESCAN_BYTES = 4096
_XML_DECLARATION = re.compile(r"\A\s*<\?xml\b[^>]*\?>")
_SPACE_ENTITIES = frozenset({"nbsp", "ensp", "emsp", "thinsp"})
_UNREAD_TAGS = frozenset({"script", "style"})
# Entities stay unexpanded (an entity defined in the chapter cannot multiply itself) and nothing is fetched.
_CHAPTER_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True)
# libxml2's HTML parser keeps at most 2,048 nested elements and drops what lies deeper. A chapter that reaches that depth
# (no real book does) is reported (Pinned by: test_epub.py::TestChapterMarkup::test_chapter_nested_deeper_than_the_parser_reads_is_reported).
_HTML_PARSER_DEPTH = 2_000
_TOO_DEEP_WARNING = "A chapter of the EPUB is nested too deeply to be read completely, so some of its text may be missing."
_CHAPTER_HEAD = b'<!DOCTYPE html><html><head><meta charset="utf-8"></head>'


@dataclass(frozen=True, slots=True)
class _Item:
    """One manifest entry: its name inside the archive (None when the href does not stay inside the package)."""

    path: str | None
    media_type: str
    properties: frozenset[str]


def extract_epub(data: bytes, options: ConvertOptions) -> Extraction:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError("The EPUB archive structure is corrupted.") from exc
    with archive:
        return _Book(archive, options).extract()


class _Book:
    def __init__(self, archive: zipfile.ZipFile, options: ConvertOptions) -> None:
        self._archive = archive
        self._names = frozenset(archive.namelist())
        self._options = options
        self._extraction = Extraction()
        self._manifest: dict[str, _Item] = {}
        self._media_types: dict[str, str] = {}
        self._encrypted: frozenset[str] = frozenset()
        self._cover_pictures: set[str] = set()
        self._cover_pages: set[str] = set()
        self._inlined = 0
        self._picture_sizes: dict[str, int] = {}   # picture name → its size once read (0: missing, unreadable or over the cap)
        self._uninlined = 0

    def extract(self) -> Extraction:
        package_path = self._package_path()
        package = self._parse(package_path) if package_path else None
        if package is None:
            raise CorruptedDocumentError("The package document (OPF) of the EPUB is missing or corrupted.")
        base = posixpath.dirname(package_path)
        self._load_manifest(package, base)
        self._load_cover(package, base)
        self._extraction.metadata.update(_metadata(package))
        self._encrypted = self._encrypted_names()

        options = self._options
        chapters, unlisted, other = self._reading_order(package)
        # DRM. Font obfuscation also lists its files in encryption.xml, but fonts are never read here, so only an encrypted
        # content document (or package document) makes the book unreadable
        # (Pinned by: test_epub.py::TestEncryption).
        if package_path in self._encrypted or any(path in self._encrypted for path in chapters):
            raise EncryptedDocumentError("The EPUB is protected by DRM (its content is encrypted). Please upload a copy without DRM.")

        texts: list[str] = []
        unread = 0
        for path in chapters:
            data = self._read(path, _MAX_CHAPTER_BYTES)
            text = self._chapter(data, path) if data is not None else None
            if text is None:
                unread += 1
                continue
            if not text.strip():
                continue
            text = text.strip()
            # A generated title page (pandoc: one page holding only `<h1>title</h1>`) followed by a chapter that opens with
            # the same heading: the heading is written once. Its characters were counted and no longer exist, and the count
            # must never be larger than the real one
            # (Pinned by: test_epub.py::TestReadingOrder::test_title_page_repeating_the_first_heading_is_written_once).
            if texts and texts[-1].startswith("#") and "\n" not in texts[-1] and text.split("\n", 1)[0] == texts[-1]:
                self._extraction.chars -= solid_length(texts.pop())
                self._extraction.exceeded = options.max_chars is not None and self._extraction.chars > options.max_chars
            texts.append(text)
            # Pinned by: test_epub.py::TestLimits::test_chapters_after_the_character_limit_are_not_read
            if self._extraction.exceeded:
                break

        if unread and not texts and not self._extraction.exceeded:
            raise CorruptedDocumentError("None of the chapters of the EPUB could be read.")
        warnings = self._extraction.warnings
        if unlisted + unread:
            # Pinned by: test_epub.py::TestBrokenPackages
            warnings.append(f"{unlisted + unread} chapter(s) listed in the reading order of the EPUB were missing, too large or not "
                            "readable and were left out.")
        if other:
            warnings.append(f"{other} page(s) of the EPUB that are not XHTML (SVG or picture pages) were left out.")
        if self._uninlined:
            warnings.append(f"{self._uninlined} picture(s) were not extracted because the pictures of the EPUB exceed "
                            f"{_MAX_INLINED_BYTES // (1024 * 1024)} MB in total.")
        self._extraction.sections.append(Section(SectionKind.DOCUMENT, 1, "\n\n".join(texts)))
        return self._extraction

    # ---- Package ---------------------------------------------------------------------------------------------------

    def _read(self, name: str, limit: int) -> bytes | None:
        """Bytes of a part. None if it is missing, cannot be decompressed, or is larger than `limit` (the stored size is not
        trusted: at most `limit + 1` bytes are decompressed)."""
        if name not in self._names:
            return None
        if self._archive.getinfo(name).flag_bits & 0x1:  # ZIP-level password
            raise EncryptedDocumentError("The EPUB is password-protected. Please remove the password and upload it again.")
        try:
            with self._archive.open(name) as member:
                data = member.read(limit + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError):
            return None
        return data if len(data) <= limit else None

    def _parse(self, name: str) -> etree._Element | None:
        data = self._read(name, _MAX_PACKAGE_BYTES)
        return parse_part(data) if data else None

    def _package_path(self) -> str | None:
        """The package document container.xml points to. When container.xml is missing or broken, or points nowhere, the first
        `.opf` file of the archive by name (Pinned by: test_epub.py::TestBrokenPackages::
        test_package_is_found_by_its_extension_when_the_container_is_broken)."""
        container = self._parse(_CONTAINER)
        if container is not None:
            for node in container.iter(etree.Element):
                path = _member_name("", node.get("full-path"), decode=False) if _local(node) == "rootfile" else None
                if path in self._names:
                    return path
        return min((name for name in self._names if name.lower().endswith(".opf")), default=None)

    def _load_manifest(self, package: etree._Element, base: str) -> None:
        for node in _children(package, "manifest", "item"):
            identifier = node.get("id")
            if not identifier or identifier in self._manifest:
                continue
            path = self._existing(base, node.get("href"))
            media_type = (node.get("media-type") or "").split(";")[0].strip().lower()
            self._manifest[identifier] = _Item(path, media_type, frozenset((node.get("properties") or "").split()))
            if path:
                self._media_types.setdefault(path, media_type)

    def _load_cover(self, package: etree._Element, base: str) -> None:
        """What marks the cover: the manifest item with `properties="cover-image"` (EPUB 3), `<meta name="cover" content="id">`
        (EPUB 2) and the `<guide>` reference of type `cover` (the cover page itself)."""
        for item in self._manifest.values():
            if "cover-image" in item.properties and item.path:
                self._cover_pictures.add(item.path)
        for node in _children(package, "metadata", "meta"):
            item = self._manifest.get(node.get("content") or "") if node.get("name") == "cover" else None
            if item is not None and item.path:
                self._cover_pictures.add(item.path)
        for node in _children(package, "guide", "reference"):
            path = self._existing(base, node.get("href")) if (node.get("type") or "").strip().lower() == "cover" else None
            if path:
                self._cover_pages.add(path)

    def _existing(self, base: str, href: str | None) -> str | None:
        """The archive member a href (relative to the folder `base`, percent-encoded) names. A name that exists only in its
        undecoded spelling is accepted too. When neither exists, the decoded name is returned (the part is then reported as
        missing); None when the href leaves the package or is not a path."""
        path = _member_name(base, href)
        if path is None or path in self._names:
            return path
        raw = _member_name(base, href, decode=False)
        return raw if raw in self._names else path

    def _encrypted_names(self) -> frozenset[str]:
        """Names listed in META-INF/encryption.xml (`CipherReference URI`, relative to the root of the archive). An
        encryption.xml that cannot be parsed is ignored: nothing can be told from it, and a chapter that really is encrypted
        is not readable markup and comes out as an unread chapter."""
        encryption = self._parse(_ENCRYPTION)
        if encryption is None:
            return frozenset()
        names = (_member_name("", node.get("URI")) for node in encryption.iter(etree.Element) if _local(node) == "CipherReference")
        return frozenset(name for name in names if name)

    def _reading_order(self, package: etree._Element) -> tuple[list[str], int, int]:
        """(chapters to read in order, spine entries that name nothing readable, pages that are not XHTML).

        Left out without a warning: `linear="no"` items (pop-up notes, answer keys and enlarged pictures that the reading
        system shows only on request), the navigation document and the NCX (tables of contents that repeat the headings as
        links), and a document listed a second time (Pinned by: test_epub.py::TestReadingOrder).
        A package without a usable spine is read in manifest order with a warning
        (Pinned by: test_epub.py::TestBrokenPackages::test_package_without_a_spine_is_read_in_manifest_order).
        """
        chapters: list[str] = []
        seen: set[str] = set()
        unlisted = other = 0

        def add(item: _Item) -> None:
            nonlocal other
            if "nav" in item.properties or item.media_type == _NCX_TYPE or item.path in seen:
                return
            seen.add(item.path)
            if item.media_type in _CHAPTER_TYPES or item.path.lower().endswith(_CHAPTER_EXTENSIONS):
                chapters.append(item.path)
            else:
                other += 1

        references = 0
        for node in _children(package, "spine", "itemref"):
            references += 1
            if references > _MAX_SPINE_ITEMS:
                # Pinned by: test_epub.py::TestLimits::test_spine_longer_than_the_cap_is_cut_with_a_warning
                self._extraction.warnings.append(f"The EPUB lists more than {_MAX_SPINE_ITEMS:,} chapters; the rest were not read.")
                break
            if (node.get("linear") or "").strip().lower() == "no":
                continue
            item = self._manifest.get(node.get("idref") or "")
            if item is None or item.path is None:
                unlisted += 1
            else:
                add(item)
        if not references:
            for item in list(self._manifest.values())[:_MAX_SPINE_ITEMS]:
                if item.path and (item.media_type in _CHAPTER_TYPES or item.path.lower().endswith(_CHAPTER_EXTENSIONS)):
                    add(item)
            if chapters:
                self._extraction.warnings.append("The EPUB has no reading order (spine), so its chapters were read in the order "
                                                 "the package lists them.")
        return chapters, unlisted, other

    # ---- Chapters --------------------------------------------------------------------------------------------------

    def _chapter(self, data: bytes, path: str) -> str | None:
        """Markdown of one chapter ('' when it has nothing to show). None when the chapter cannot be parsed at all."""
        html = self._prepare(data, path)
        if not html:
            return html
        inner = extract_html(html, self._options)
        # The chapter's placeholders carry numbers local to the chapter (same as an imported chunk in docx.py); rewritten in one
        # pass so that a rewritten number is not rewritten again.
        markers = {image.id: self._extraction.add_image(image.data, kind=image.kind, alt=image.alt) for image in inner.images}
        text = inner.sections[0].text if inner.sections else ""
        text = renumber_image_markers(text, markers)
        self._extraction.warnings.extend(warning for warning in inner.warnings if warning not in self._extraction.warnings)
        # A chapter the HTML extractor stopped reading at the limit reports the count it stopped at.
        self._extraction.count(inner.chars if inner.exceeded else solid_length(text), self._options)
        return text

    def _prepare(self, data: bytes, path: str) -> bytes | None:
        """The chapter as the HTML the HTML extractor is given: UTF-8, the body only. None if it cannot be parsed, b'' if it
        is a page with nothing to show.

        - The chapter is parsed as XML first and written out as HTML, because XHTML is XML: an HTML parser reads a CDATA
          section as a comment (its text vanished) and does not know prefixed elements (`<h:h1>`, `<h:li>` came out as one
          run of text). A chapter that is not well-formed XML is read with the HTML parser instead, as reading systems do
          (Pinned by: test_epub.py::TestChapterMarkup).
        - `<head>` is left out: the HTML extractor turns `<title>` into a heading when the page has none, and chapter titles
          are generator leftovers such as 'ch001.xhtml' or the book title once per chapter.
        - `<aside>` becomes `<div>`: outside main/article the HTML extractor takes an aside for a site side menu and removes
          it, but in a book it is a footnote or a note box.
        - Links into the package lose their target (`_EXTERNAL_LINK` ones keep it): the file names they point to do not exist
          in the Markdown (Pinned by: test_epub.py::TestLinks).
        - Pictures of the package are put into `src` as `data:` URIs, the one form the HTML extractor takes pictures from
          without fetching anything (Pinned by: test_epub.py::TestPictures).
        """
        root = _parse_chapter(data, self._extraction.warnings)
        if root is None:
            return None
        elements = list(root.iter(etree.Element))
        body = None
        pictures: list[tuple[etree._Element, str]] = []
        for element in elements:
            name = _local(element)
            if name != element.tag:
                element.tag = name
            if name == "body" and body is None:
                body = element
            elif name == "aside":
                element.tag = "div"
            elif name == "a":
                if "href" in element.attrib and not _EXTERNAL_LINK.match(element.get("href", "")):
                    del element.attrib["href"]
            elif name == "img":
                source = self._existing(posixpath.dirname(path), element.get("src"))
                if source:
                    pictures.append((element, source))
        if body is None:
            body = root
            for element in elements:
                if element.tag == "head" and element.getparent() is not None:
                    element.getparent().remove(element)

        if not _has_text(body):
            # A page without any text: the cover (its only pictures are the cover picture, or the package names the page as
            # the cover) or a blank page. A page of other pictures is kept for its pictures.
            # Pinned by: test_epub.py::TestReadingOrder::test_cover_page_without_text_is_left_out
            sources = {source for _, source in pictures}
            if not sources or path in self._cover_pages or sources <= self._cover_pictures:
                return b""
        if self._options.extract_images:
            for element, source in pictures:
                self._inline(element, source)
        return _CHAPTER_HEAD + etree.tostring(body, method="html", encoding="utf-8", with_tail=False) + b"</html>"

    def _inline(self, image: etree._Element, name: str) -> None:
        """Put a picture of the package into the `src` of its `<img>` as a `data:` URI. Left alone (the alt text remains): SVG
        pictures (the HTML extractor does not take those out either), encrypted pictures, missing or oversized ones, and
        everything after the book's total (`_MAX_INLINED_BYTES`) is used up."""
        media_type = self._media_types.get(name, "")
        if "svg" in media_type or name.lower().endswith((".svg", ".svgz")) or name in self._encrypted:
            return
        # A picture is decompressed only when it can still be used: one found missing or oversized is not read again, and one
        # whose size is known is not read once it no longer fits in the book's total. 20,000 `<img>` on one oversized picture
        # (a 20 KB book) each inflated 20 MB — over a minute
        # (Pinned by: test_epub.py::TestPictures::test_unusable_picture_is_read_once_however_often_it_is_shown).
        size = self._picture_sizes.get(name)
        if size == 0 or name not in self._names:
            return
        if self._inlined + (size or 1) > _MAX_INLINED_BYTES:
            self._uninlined += 1
            return
        data = self._read(name, _MAX_PICTURE_BYTES)
        self._picture_sizes[name] = len(data) if data else 0
        if not data:
            return
        if self._inlined + len(data) > _MAX_INLINED_BYTES:
            # Pinned by: test_epub.py::TestPictures::test_pictures_over_the_total_cap_keep_only_their_alt_text
            self._uninlined += 1
            return
        self._inlined += len(data)
        # The type label only has to say 'image': the converter recognizes the picture by its content.
        label = media_type if _PICTURE_TYPE.match(media_type) else "image/png"
        image.set("src", f"data:{label};base64,{base64.b64encode(data).decode('ascii')}")


def _parse_chapter(data: bytes, warnings: list[str]) -> etree._Element | None:
    """The chapter as a tree: XML when it is well-formed, otherwise what the HTML parser makes of it (as reading systems do
    for `&nbsp;` without a DTD or an unclosed `<p>`). None when even that gives nothing."""
    try:
        return etree.fromstring(data, parser=_CHAPTER_PARSER)
    except etree.XMLSyntaxError:
        pass
    match = _DECLARED_CHARSET.search(data[:_CHARSET_PRESCAN_BYTES])
    declared = (match.group(1) or match.group(2)).decode("ascii", errors="replace") if match else None
    text, encoding = decode_text(data, declared, markup=True)
    try:
        root = etree.fromstring(_without_prolog(text), parser=etree.HTMLParser(no_network=True, remove_comments=True, remove_pis=True,
                                                                               huge_tree=True))
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None
    for warning in (decoding_warning(encoding), _TOO_DEEP_WARNING if _is_cut_by_depth(root) else None):
        if warning and warning not in warnings:
            warnings.append(warning)
    return root


def _without_prolog(text: str) -> str:
    """The chapter without its XML declaration and DOCTYPE. A string with an encoding declaration is refused by lxml, and the
    HTML parser shows the entity definitions of a DOCTYPE's internal subset (`<!DOCTYPE html [ … ]>`) as text."""
    text = _XML_DECLARATION.sub("", text, count=1)
    start = text.find("<!DOCTYPE", 0, _CHARSET_PRESCAN_BYTES)
    if start == -1:
        return text
    end = text.find(">", start)
    subset = text.find("[", start)
    if subset != -1 and subset < end:
        close = text.find("]", subset)
        end = text.find(">", close) if close != -1 else -1
    return text if end == -1 else text[:start] + text[end + 1:]


def _is_cut_by_depth(root: etree._Element) -> bool:
    """Whether the tree reaches the nesting depth at which the HTML parser stops adding content (2,048 levels)."""
    depth = 0
    for event, _ in etree.iterwalk(root, events=("start", "end")):
        depth += 1 if event == "start" else -1
        if depth >= _HTML_PARSER_DEPTH:
            return True
    return False


def _has_text(body: etree._Element) -> bool:
    for node in body.iter():
        if isinstance(node, etree._Entity):
            if node.name not in _SPACE_ENTITIES:
                return True
        elif isinstance(node.tag, str) and node.tag not in _UNREAD_TAGS and node.text and node.text.strip():
            return True
        if node is not body and node.tail and node.tail.strip():
            return True
    return False


def _metadata(package: etree._Element) -> dict[str, str]:
    """`dc:title` (the first one: the main title), every `dc:creator` and `dc:language`, under the keys the other extractors use."""
    metadata: dict[str, str] = {}
    creators: list[str] = []
    for parent in package:
        if not isinstance(parent.tag, str) or _local(parent) != "metadata":
            continue
        for node in parent.iter(etree.Element):
            name = _local(node)
            if name not in ("title", "creator", "language"):
                continue
            text = " ".join("".join(node.itertext()).split())[:_MAX_METADATA_CHARS]
            if not text:
                continue
            if name == "creator":
                if text not in creators and len(creators) < _MAX_CREATORS:
                    creators.append(text)
            else:
                metadata.setdefault(name, text)
    if creators:
        metadata["author"] = ", ".join(creators)
    return metadata


def _children(package: etree._Element, parent_name: str, name: str) -> list[etree._Element]:
    """The `name` elements directly inside the package's `parent_name` elements (manifest → item, spine → itemref …)."""
    return [node for parent in package if isinstance(parent.tag, str) and _local(parent) == parent_name
            for node in parent if isinstance(node.tag, str) and _local(node) == name]


def _local(node: etree._Element) -> str:
    tag = node.tag
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _member_name(base: str, href: str | None, *, decode: bool = True) -> str | None:
    """The archive name a reference written in the file `base`/… points to, without its fragment. None when it is not a path
    inside the package: another scheme or host (`http:`, `data:`), an absolute path, or one that climbs out with `..`
    (Pinned by: test_epub.py::TestBrokenPackages::test_references_that_leave_the_package_read_nothing)."""
    if not href or not href.strip():
        return None
    try:
        parts = urlsplit(href.strip())
    except ValueError:
        return None
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(base, unquote(parts.path) if decode else parts.path))
    if path in (".", "..") or path.startswith(("../", "/")) or "\x00" in path:
        return None
    return path

