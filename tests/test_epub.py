"""EPUB: the package (container.xml → OPF manifest and spine) decides what is read and in which order, each chapter goes through
the HTML extractor, and the package's own pictures are extracted in place.

Every book is built in memory with `_epub`: chapter files under `OEBPS/`, a package document at `OEBPS/content.opf`.
"""

from io import BytesIO
import zipfile

import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, DocumentFormat, EncryptedDocumentError, SectionKind, convert
from opendockit.extractors import epub
from tests import builder


_CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="{path}" media-type="application/oebps-package+xml"/></rootfiles></container>')


def _page(body: str, *, title: str = "ch001.xhtml") -> str:
    """One XHTML chapter the way EPUB generators write it: XML declaration, XHTML namespace, a `<title>` in the head."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" href="../style.css"/></head>'
            f"<body>{body}</body></html>")


def _epub(chapters: dict[str, str | bytes | None], *, files: dict[str, bytes] | None = None, metadata: str = "",
          manifest: str = "", spine: str | None = None, guide: str = "", item_attributes: dict[str, str] | None = None,
          container: str | None = _CONTAINER, package_path: str = "OEBPS/content.opf", stored: bool = False) -> bytes:
    """An EPUB whose chapters (href relative to the package document → content; None = listed but not in the archive) get the
    ids c1, c2 … and, unless `spine` (the inside of `<spine>`) is given, are read in that order. `manifest`, `metadata` and
    `guide` are extra XML for those elements; `item_attributes` adds attributes to a chapter's manifest item; `files` are
    further archive members by full name."""
    base = package_path.rpartition("/")[0]
    items = "".join(f'<item id="c{number}" href="{href}" media-type="application/xhtml+xml" {(item_attributes or {}).get(href, "")}/>'
                    for number, href in enumerate(chapters, start=1))
    if spine is None:
        spine = "".join(f'<itemref idref="c{number}"/>' for number in range(1, len(chapters) + 1))
    package = ('<?xml version="1.0" encoding="UTF-8"?><package version="3.0" xmlns="http://www.idpf.org/2007/opf">'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{metadata}</metadata>'
               f"<manifest>{items}{manifest}</manifest><spine>{spine}</spine>{guide}</package>")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        if container is not None:
            archive.writestr("META-INF/container.xml", container.format(path=package_path))
        archive.writestr(package_path, package)
        for href, content in chapters.items():
            if content is not None:
                archive.writestr(f"{base}/{href}" if base else href, content)
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _with_password_flag(data: bytes, name: str) -> bytes:
    """Set the 'encrypted' bit of one member in the central directory, as a password-protected ZIP has it."""
    patched = bytearray(data)
    position = patched.find(b"PK\x01\x02")
    while position != -1:
        length = int.from_bytes(patched[position + 28:position + 30], "little")
        if bytes(patched[position + 46:position + 46 + length]) == name.encode():
            patched[position + 8] |= 0x01
        position = patched.find(b"PK\x01\x02", position + 46)
    return bytes(patched)


class TestStructure:
    def test_chapters_are_joined_in_spine_order_not_manifest_order(self) -> None:
        """The manifest lists 맺음말 first, the spine puts it last. The spine is the reading order."""
        data = _epub({"end.xhtml": _page("<h1>맺음말</h1><p>감사합니다.</p>"), "start.xhtml": _page("<h1>머리말</h1><p>시작합니다.</p>")},
                     spine='<itemref idref="c2"/><itemref idref="c1"/>')

        result = convert(data, "book.epub")

        assert result.format is DocumentFormat.EPUB
        assert [(section.kind, section.index) for section in result.sections] == [(SectionKind.DOCUMENT, 1)]
        assert result.text == "# 머리말\n\n시작합니다.\n\n# 맺음말\n\n감사합니다."

    def test_headings_lists_tables_and_code_come_through_the_html_extractor(self) -> None:
        """One chapter with the block structures of a technical book."""
        body = ("<h1>릴리스 노트</h1><h2>추가</h2><ul><li>JSON 출력</li><li>재시도<ul><li>지수 백오프</li></ul></li></ul>"
                "<table><thead><tr><th>이슈</th><th>수정</th></tr></thead><tbody><tr><td>#812</td><td>시간대 유지</td></tr></tbody></table>"
                "<pre><code>client.upload(path)\n    retries = 3</code></pre>")

        result = convert(_epub({"ch1.xhtml": _page(body)}), "book.epub")

        assert "# 릴리스 노트\n\n## 추가" in result.text
        assert "- JSON 출력\n- 재시도\n  - 지수 백오프" in result.text
        assert "| 이슈 | 수정 |\n| --- | --- |\n| #812 | 시간대 유지 |" in result.text
        assert "```\nclient.upload(path)\n    retries = 3\n```" in result.text

    def test_title_authors_and_language_come_from_the_package_metadata(self) -> None:
        """Two `dc:creator` elements (EPUB lists each author separately) are joined; the first `dc:title` is the main title."""
        metadata = ('<dc:title>파이썬 입문</dc:title><dc:title>부제: 처음 시작하기</dc:title><dc:creator>김하늘</dc:creator>'
                    '<dc:creator>Lee Minho</dc:creator><dc:language>ko</dc:language><dc:identifier>urn:isbn:1</dc:identifier>')

        result = convert(_epub({"ch1.xhtml": _page("<p>본문</p>")}, metadata=metadata), "book.epub")

        assert result.metadata["title"] == "파이썬 입문"
        assert result.metadata["author"] == "김하늘, Lee Minho"
        assert result.metadata["language"] == "ko"

    def test_package_at_the_archive_root_resolves_chapters_in_subfolders(self) -> None:
        """The package document sits at the root (no folder), the chapter in `text/`."""
        data = _epub({"text/ch1.xhtml": _page("<p>루트 패키지</p>")}, package_path="content.opf")

        assert convert(data, "book.epub").text == "루트 패키지"


class TestReadingOrder:
    def test_navigation_document_and_ncx_are_not_read(self) -> None:
        """The EPUB 3 navigation document is in the spine (some generators put it there) and the NCX is referenced from it
        too. Both only repeat the chapter headings as links to file names."""
        nav = _page('<nav epub:type="toc"><h1>차례</h1><ol><li><a href="ch1.xhtml">첫 장</a></li></ol></nav><p>목차 설명</p>')
        ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint><navLabel><text>NCX 항목</text></navLabel></navPoint></navMap></ncx>'
        data = _epub({"nav.xhtml": nav, "ch1.xhtml": _page("<h1>첫 장</h1><p>본문</p>")}, files={"OEBPS/toc.ncx": ncx.encode()},
                     item_attributes={"nav.xhtml": 'properties="nav"'},
                     manifest='<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                     spine='<itemref idref="c1"/><itemref idref="ncx"/><itemref idref="c2"/>')

        result = convert(data, "book.epub")

        assert result.text == "# 첫 장\n\n본문"
        assert result.warnings == []

    def test_non_linear_spine_items_are_left_out(self) -> None:
        """`linear="no"` marks content outside the reading flow (here an answer key shown as a pop-up)."""
        data = _epub({"ch1.xhtml": _page("<p>문제</p>"), "answers.xhtml": _page("<p>정답 팝업</p>")},
                     spine='<itemref idref="c1" linear="yes"/><itemref idref="c2" linear="no"/>')

        assert convert(data, "book.epub").text == "문제"

    def test_document_listed_twice_in_the_spine_is_read_once(self) -> None:
        """An invalid but real spine: the same idref twice, and a second manifest item with the same href."""
        data = _epub({"ch1.xhtml": _page("<p>한 번만</p>")}, manifest='<item id="again" href="ch1.xhtml" media-type="application/xhtml+xml"/>',
                     spine='<itemref idref="c1"/><itemref idref="c1"/><itemref idref="again"/>')

        assert convert(data, "book.epub").text == "한 번만"

    @pytest.mark.parametrize("marking", ["cover-image", "meta", "guide"])
    def test_cover_page_without_text_is_left_out(self, marking: str) -> None:
        """A first page holding nothing but the cover picture, marked the EPUB 3 way (`properties="cover-image"`), the EPUB 2
        way (`<meta name="cover">`) or by the guide. The cover is not content of the book."""
        cover = _page('<div><img src="images/cover.png" alt="표지"/></div>')
        data = _epub({"cover.xhtml": cover, "ch1.xhtml": _page("<p>본문</p>")}, files={"OEBPS/images/cover.png": builder.png_bytes()},
                     manifest=f'<item id="cover-img" href="images/cover.png" media-type="image/png" '
                              f'{"properties=" + chr(34) + "cover-image" + chr(34) if marking == "cover-image" else ""}/>',
                     metadata='<meta name="cover" content="cover-img"/>' if marking == "meta" else "",
                     guide='<guide><reference type="cover" href="cover.xhtml"/></guide>' if marking == "guide" else "")

        result = convert(data, "book.epub")

        assert result.text == "본문"
        assert result.images == []

    def test_page_of_pictures_that_is_not_the_cover_is_kept(self) -> None:
        """A full-page illustration between chapters has no text either, but it is content."""
        data = _epub({"plate.xhtml": _page('<img src="plate.png" alt="삽화"/>'), "ch1.xhtml": _page("<p>본문</p>")},
                     files={"OEBPS/plate.png": builder.png_bytes()})

        result = convert(data, "book.epub")

        assert [image.alt for image in result.images] == ["삽화"]
        assert result.text.endswith("본문")

    def test_blank_page_leaves_no_gap_and_no_warning(self) -> None:
        data = _epub({"blank.xhtml": _page("<p>&#160;</p>"), "ch1.xhtml": _page("<p>본문</p>")})

        result = convert(data, "book.epub")

        assert result.text == "본문"
        assert result.warnings == []

    def test_title_page_repeating_the_first_heading_is_written_once(self) -> None:
        """pandoc writes a title page holding only `<h1>title</h1>` and then the document, which opens with the same h1."""
        data = _epub({"title_page.xhtml": _page('<section epub:type="titlepage"><h1>릴리스 노트 2.4</h1></section>'),
                      "ch001.xhtml": _page("<h1>릴리스 노트 2.4</h1><p>본문</p>")})

        assert convert(data, "book.epub").text == "# 릴리스 노트 2.4\n\n본문"

    def test_title_page_with_its_own_content_is_kept(self) -> None:
        """A title page that says more than the heading (the author) is not a repetition."""
        data = _epub({"title_page.xhtml": _page("<h1>제목</h1><p>김하늘 지음</p>"), "ch001.xhtml": _page("<h1>제목</h1><p>본문</p>")})

        assert convert(data, "book.epub").text == "# 제목\n\n김하늘 지음\n\n# 제목\n\n본문"

    def test_chapter_title_element_does_not_become_a_heading(self) -> None:
        """A chapter without a heading whose `<title>` is the generator's file name. The HTML extractor would promote it."""
        data = _epub({"ch1.xhtml": _page("<p>머리글 없는 장</p>", title="ch001.xhtml")})

        assert convert(data, "book.epub").text == "머리글 없는 장"


class TestChapterMarkup:
    def test_self_closed_elements_do_not_swallow_the_rest_of_the_chapter(self) -> None:
        """XHTML closes empty elements with `/>` (`<script src=…/>`, `<a id=…/>`, `<div/>`), which are opening tags in HTML.
        The text after them must survive the trip through the HTML extractor."""
        body = '<p>앞 문단<a id="anchor"/></p><script type="text/javascript" src="reader.js"/><div class="pagebreak"/><p>뒤 문단</p>'

        assert convert(_epub({"ch1.xhtml": _page(body)}), "book.epub").text == "앞 문단\n\n뒤 문단"

    def test_cdata_sections_and_prefixed_xhtml_elements_are_read_as_xml(self) -> None:
        """Two things only XML knows: a CDATA section (an HTML parser takes it for a comment and drops the text) and XHTML
        elements written with a namespace prefix (`<h:h1>`), which an HTML parser does not recognize as headings or lists."""
        prefixed = ('<?xml version="1.0"?><h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:head><h:title>t</h:title></h:head>'
                    "<h:body><h:h1>제목</h:h1><h:ul><h:li>항목</h:li></h:ul></h:body></h:html>")
        data = _epub({"ch1.xhtml": _page("<p>식: <![CDATA[a < b && c]]> 끝</p>"), "ch2.xhtml": prefixed})

        assert convert(data, "book.epub").text == "식: a < b && c 끝\n\n# 제목\n\n- 항목"

    def test_chapter_that_is_not_well_formed_xml_is_read_as_html(self) -> None:
        """`&nbsp;` without a DTD and an unclosed `<p>` are XML errors; reading systems show such chapters anyway."""
        chapter = "<html><head><title>t</title></head><body><h2>깨진 장</h2><p>첫&nbsp;문단<p>둘째 문단<br>끝</body></html>"

        result = convert(_epub({"ch1.xhtml": chapter, "ch2.xhtml": _page("<p>다음 장</p>")}), "book.epub")

        assert result.text == "## 깨진 장\n\n첫 문단\n\n둘째 문단\n끝\n\n다음 장"
        assert result.warnings == []

    @pytest.mark.parametrize("well_formed", [True, False])
    def test_chapter_in_a_legacy_korean_encoding_is_decoded(self, well_formed: bool) -> None:
        """An EPUB 2 chapter saved as EUC-KR and declared only in the XML declaration, once well-formed and once with an unclosed
        tag (the HTML fallback must honor the declaration too)."""
        closing = "</p>" if well_formed else ""
        chapter = ('<?xml version="1.0" encoding="EUC-KR"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>장</title></head>'
                   f"<body><p>한글 인코딩 본문{closing}</body></html>").encode("euc-kr")

        assert convert(_epub({"ch1.xhtml": chapter}), "book.epub").text == "한글 인코딩 본문"

    def test_footnote_aside_is_kept(self) -> None:
        """EPUB 3 footnotes are `<aside epub:type="footnote">`. The HTML extractor removes an aside outside main/article as a
        site side menu."""
        body = '<p>본문<a epub:type="noteref" href="#n1">1</a></p><aside epub:type="footnote" id="n1"><p>각주 내용입니다.</p></aside>'

        assert "각주 내용입니다." in convert(_epub({"ch1.xhtml": _page(body)}), "book.epub").text

    def test_entities_defined_in_a_chapter_are_not_expanded(self) -> None:
        """A 'billion laughs' chapter: ten levels of entities that each repeat the previous one ten times."""
        levels = "".join(f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' for n in range(1, 10))
        chapter = (f'<?xml version="1.0"?><!DOCTYPE html [<!ENTITY l0 "ha">{levels}]>'
                   '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>시작 &l9; 끝</p></body></html>')

        result = convert(_epub({"ch1.xhtml": chapter}), "book.epub")

        assert result.text == "시작 &l9; 끝"

    def test_chapter_nested_deeper_than_the_parser_reads_is_reported(self) -> None:
        """100,000 nested `<div>`s in one chapter: the parser stops at 2,048 levels, so the innermost text cannot be vouched
        for. The book is still converted, the next chapter is read, and the gap is reported."""
        deep = _page("<div>" * 100_000 + "깊은 곳" + "</div>" * 100_000)

        result = convert(_epub({"deep.xhtml": deep, "ch2.xhtml": _page("<p>다음 장</p>")}), "book.epub")

        assert "다음 장" in result.text
        assert any("nested too deeply" in warning for warning in result.warnings)

    def test_moderately_deep_chapter_keeps_its_text(self) -> None:
        """300 levels is past the XML parser's limit (256) but nothing a browser would refuse."""
        deep = _page("<div>" * 300 + "깊은 곳" + "</div>" * 300)

        result = convert(_epub({"deep.xhtml": deep}), "book.epub")

        assert result.text == "깊은 곳"
        assert result.warnings == []


class TestLinks:
    def test_links_between_chapters_keep_their_text_and_lose_the_file_name(self) -> None:
        """Cross references `chapter2.xhtml#sec`, `#local` and `../notes.xhtml` point at files of the package, which do not
        exist in the Markdown. The external link's text stays whatever the link option does with its address."""
        body = ('<p><a href="chapter2.xhtml#sec">2장 참고</a>와 <a href="#local">아래 절</a>, <a href="../notes.xhtml">미주</a>, '
                '<a href="https://example.com/guide">외부 안내</a></p>')

        text = convert(_epub({"ch1.xhtml": _page(body), "chapter2.xhtml": _page('<h2 id="sec">2장</h2>')}), "book.epub").text

        assert "2장 참고와 아래 절, 미주," in text
        assert "외부 안내" in text
        assert "xhtml" not in text and "#local" not in text and "#sec" not in text


class TestPictures:
    def test_picture_of_the_package_is_extracted_where_it_stands(self) -> None:
        """The chapter is in `text/`, the picture in `images/` under a percent-encoded Korean name with a space: the `src` is
        relative to the chapter and decoded before the lookup."""
        body = '<p>그림 앞</p><p><img src="../images/%EA%B7%B8%EB%A6%BC%201.png" alt="매출 차트"/></p><p>그림 뒤</p>'
        picture = builder.png_bytes(seed=3)
        data = _epub({"text/ch1.xhtml": _page(body)}, files={"OEBPS/images/그림 1.png": picture},
                     manifest='<item id="p1" href="images/%EA%B7%B8%EB%A6%BC%201.png" media-type="image/png"/>')

        result = convert(data, "book.epub")

        assert [(image.alt, image.width, image.height) for image in result.images] == [("매출 차트", 200, 120)]
        before, _, after = result.text.partition("매출 차트")
        assert "그림 앞" in before and "그림 뒤" in after

    def test_pictures_of_later_chapters_are_numbered_after_the_earlier_ones(self) -> None:
        """Each chapter is converted on its own and numbers its pictures from 1; in the book they must stay distinct and in order."""
        data = _epub({"ch1.xhtml": _page('<p>하나</p><img src="a.png" alt="첫째"/>'), "ch2.xhtml": _page('<p>둘</p><img src="b.png" alt="둘째"/>')},
                     files={"OEBPS/a.png": builder.png_bytes(seed=1), "OEBPS/b.png": builder.png_bytes(seed=2)})

        result = convert(data, "book.epub")

        assert [(image.id, image.alt) for image in result.images] == [(1, "첫째"), (2, "둘째")]
        assert result.text.index("하나") < result.text.index("첫째") < result.text.index("둘") < result.text.index("둘째")

    def test_pictures_are_not_read_when_image_extraction_is_off(self) -> None:
        data = _epub({"ch1.xhtml": _page('<p>본문</p><img src="a.png" alt="도표"/>')}, files={"OEBPS/a.png": builder.png_bytes()})

        result = convert(data, "book.epub", ConvertOptions(extract_images=False))

        assert result.images == []
        assert "[Image: 도표]" in result.text

    def test_remote_missing_and_svg_pictures_keep_only_their_alt_text(self) -> None:
        """Nothing is fetched for an `https` picture; a `src` naming no file of the package and an SVG picture are not pictures
        that can be extracted either."""
        body = ('<p>본문</p><img src="https://example.com/remote.png" alt="원격"/><img src="gone.png" alt="없음"/>'
                '<img src="diagram.svg" alt="벡터"/>')
        data = _epub({"ch1.xhtml": _page(body)}, files={"OEBPS/diagram.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>'})

        result = convert(data, "book.epub")

        assert result.images == []
        assert all(alt in result.text for alt in ("[Image: 원격]", "[Image: 없음]", "[Image: 벡터]"))

    def test_pictures_over_the_total_cap_keep_only_their_alt_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three pictures with a cap that holds the first two: the third keeps its alt text and the book says so."""
        pictures = [builder.png_bytes(seed=seed) for seed in (1, 2, 3)]
        monkeypatch.setattr(epub, "_MAX_INLINED_BYTES", len(pictures[0]) + len(pictures[1]))
        body = "<p>본문</p>" + "".join(f'<p><img src="p{number}.png" alt="그림 설명 {number}"/></p>' for number in range(3))
        data = _epub({"ch1.xhtml": _page(body)}, files={f"OEBPS/p{number}.png": picture for number, picture in enumerate(pictures)})

        result = convert(data, "book.epub")

        assert [image.alt for image in result.images] == ["그림 설명 0", "그림 설명 1"]
        assert "[Image: 그림 설명 2]" in result.text
        assert any("1 picture(s) were not extracted" in warning for warning in result.warnings)

    def test_unusable_picture_is_read_once_however_often_it_is_shown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One picture over the per-picture cap shown 500 times, and one that fits shown 500 times with a total cap that holds
        two copies. Each `<img>` used to decompress the picture again up to the cap (20 MB each for a 20 KB book). The oversized
        one is read once; the other is read until the total is used up and not after."""
        small, large = builder.png_bytes(seed=1), builder.png_bytes(seed=2) + b"\0" * 4096
        monkeypatch.setattr(epub, "_MAX_PICTURE_BYTES", len(large) - 1)
        monkeypatch.setattr(epub, "_MAX_INLINED_BYTES", 2 * len(small))
        reads: list[str] = []
        read = epub._Book._read
        monkeypatch.setattr(epub._Book, "_read", lambda self, name, limit: (reads.append(name), read(self, name, limit))[1])
        body = "<p>본문</p>" + '<img src="large.png" alt="큰"/>' * 500 + '<img src="small.png" alt="작은"/>' * 500
        data = _epub({"ch1.xhtml": _page(body)}, files={"OEBPS/large.png": large, "OEBPS/small.png": small})

        result = convert(data, "book.epub")

        assert reads.count("OEBPS/large.png") == 1 and reads.count("OEBPS/small.png") == 2
        assert len(result.images) == 1   # the two copies are one picture to the converter
        assert any("498 picture(s) were not extracted" in warning for warning in result.warnings)

    def test_text_shaped_like_a_picture_placeholder_stays_text(self) -> None:
        """A chapter whose text contains `⟦IMG:1⟧` and `⟦IMG:dead:1⟧`, and a real picture after it. The chapter's placeholders
        are renumbered for the book; matched by shape alone, the author's text vanished and the picture appeared three times."""
        body = '<p>문자 그대로 ⟦IMG:1⟧ 그리고 ⟦IMG:dead:1⟧ 입니다.</p><p><img src="a.png" alt="도표"/></p>'
        data = _epub({"ch1.xhtml": _page(body)}, files={"OEBPS/a.png": builder.png_bytes()})

        result = convert(data, "book.epub")

        assert len(result.images) == 1
        assert "문자 그대로 ⟦IMG:1⟧ 그리고 ⟦IMG:dead:1⟧ 입니다." in result.text
        assert result.text.count("도표") == 1

    def test_picture_larger_than_the_per_picture_cap_is_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        picture = builder.png_bytes()
        monkeypatch.setattr(epub, "_MAX_PICTURE_BYTES", len(picture) - 1)
        data = _epub({"ch1.xhtml": _page('<p>본문</p><img src="a.png" alt="큰 그림"/>')}, files={"OEBPS/a.png": picture})

        result = convert(data, "book.epub")

        assert result.images == []
        assert "[Image: 큰 그림]" in result.text


class TestEncryption:
    _ENCRYPTION = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                   '<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algorithm}"/>'
                   '<enc:CipherData><enc:CipherReference URI="{uri}"/></enc:CipherData></enc:EncryptedData></encryption>')

    def test_drm_encrypted_chapter_is_reported_as_encrypted(self) -> None:
        """encryption.xml lists the chapter itself (Adobe ADEPT / Readium LCP style): its bytes are ciphertext."""
        encryption = self._ENCRYPTION.format(algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc", uri="OEBPS/text/ch%201.xhtml")
        data = _epub({"text/ch%201.xhtml": None}, files={"OEBPS/text/ch 1.xhtml": b"\x8f\x02\xa1" * 200, "META-INF/encryption.xml": encryption.encode()})

        with pytest.raises(EncryptedDocumentError) as raised:
            convert(data, "book.epub")

        assert "DRM" in str(raised.value)

    def test_font_obfuscation_alone_does_not_make_the_book_encrypted(self) -> None:
        """Most commercial fonts are embedded obfuscated, which puts an encryption.xml into an otherwise open book."""
        encryption = self._ENCRYPTION.format(algorithm="http://www.idpf.org/2008/embedding", uri="OEBPS/fonts/NanumGothic.otf")
        data = _epub({"ch1.xhtml": _page("<p>열린 본문</p>")},
                     files={"OEBPS/fonts/NanumGothic.otf": b"\x00" * 64, "META-INF/encryption.xml": encryption.encode()})

        result = convert(data, "book.epub")

        assert result.text == "열린 본문"
        assert result.warnings == []

    def test_encryption_file_that_cannot_be_parsed_does_not_reject_an_open_book(self) -> None:
        data = _epub({"ch1.xhtml": _page("<p>열린 본문</p>")}, files={"META-INF/encryption.xml": b"<encryption><enc:EncryptedData"})

        assert convert(data, "book.epub").text == "열린 본문"

    def test_encrypted_picture_is_not_extracted_but_the_text_is_read(self) -> None:
        encryption = self._ENCRYPTION.format(algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc", uri="OEBPS/a.png")
        data = _epub({"ch1.xhtml": _page('<p>본문</p><img src="a.png" alt="잠긴 그림"/>')},
                     files={"OEBPS/a.png": b"\x8f\x02\xa1" * 200, "META-INF/encryption.xml": encryption.encode()})

        result = convert(data, "book.epub")

        assert result.images == []
        assert "본문" in result.text and "[Image: 잠긴 그림]" in result.text

    def test_zip_password_on_a_chapter_is_reported_as_encrypted(self) -> None:
        data = _with_password_flag(_epub({"ch1.xhtml": _page("<p>본문</p>")}), "OEBPS/ch1.xhtml")

        with pytest.raises(EncryptedDocumentError):
            convert(data, "book.epub")


class TestBrokenPackages:
    def test_spine_entries_without_a_file_are_skipped_with_a_warning(self) -> None:
        """Three broken references — a manifest item whose file is not in the archive, an idref that is in no manifest, a
        chapter whose stored bytes fail the CRC check — and one good chapter between them."""
        data = _epub({"gone.xhtml": None, "ch1.xhtml": _page("<p>남은 장</p>"), "bad.xhtml": "CRC-MARKER-AAAA"},
                     spine='<itemref idref="c1"/><itemref idref="nowhere"/><itemref idref="c2"/><itemref idref="c3"/>', stored=True)
        data = data.replace(b"CRC-MARKER-AAAA", b"CRC-MARKER-BBBB")

        result = convert(data, "book.epub")

        assert result.text == "남은 장"
        assert [warning for warning in result.warnings if "3 chapter(s)" in warning]

    def test_package_is_found_by_its_extension_when_the_container_is_broken(self) -> None:
        for container in (None, "<container><rootfiles", _CONTAINER.replace("{path}", "missing/package.opf")):
            data = _epub({"ch1.xhtml": _page("<p>본문</p>")}, container=container,
                         files={} if container else {"META-INF/container.xml": b""})

            assert convert(data, "book.epub").text == "본문"

    def test_archive_without_a_package_document_is_corrupted(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", _CONTAINER.format(path="OEBPS/content.opf"))
            archive.writestr("OEBPS/ch1.xhtml", _page("<p>secret words</p>"))

        with pytest.raises(CorruptedDocumentError) as raised:
            convert(buffer.getvalue(), "book.epub")

        assert "secret" not in str(raised.value)

    def test_package_without_a_spine_is_read_in_manifest_order(self) -> None:
        data = _epub({"a.xhtml": _page("<p>가</p>"), "b.xhtml": _page("<p>나</p>")}, spine="",
                     manifest='<item id="css" href="style.css" media-type="text/css"/>', files={"OEBPS/style.css": b"p{}"})

        result = convert(data, "book.epub")

        assert result.text == "가\n\n나"
        assert any("no reading order" in warning for warning in result.warnings)

    def test_references_that_leave_the_package_read_nothing(self) -> None:
        """The archive holds a member literally named `../outside.xhtml`; the manifest and a picture point above the package
        root and at an absolute path. None of them is opened."""
        data = _epub({"../outside.xhtml": None, "ch1.xhtml": _page('<p>안쪽</p><img src="../../../etc/passwd" alt="밖"/><img src="/etc/hosts" alt="절대"/>'),
                      "/etc/passwd": None, "../../etc/passwd": None},
                     files={"../outside.xhtml": _page("<p>바깥 비밀</p>").encode()}, package_path="content.opf")

        result = convert(data, "book.epub")

        assert "안쪽" in result.text and "비밀" not in result.text
        assert result.images == []
        assert [warning for warning in result.warnings if "3 chapter(s)" in warning]

    def test_book_whose_chapters_are_all_unreadable_is_corrupted(self) -> None:
        data = _epub({"gone1.xhtml": None, "gone2.xhtml": None})

        with pytest.raises(CorruptedDocumentError):
            convert(data, "book.epub")

    def test_pages_that_are_not_xhtml_are_left_out_with_a_warning(self) -> None:
        """A spine item that is an SVG page (allowed by EPUB 3) next to a normal chapter."""
        data = _epub({"ch1.xhtml": _page("<p>본문</p>")}, files={"OEBPS/page.svg": '<svg xmlns="http://www.w3.org/2000/svg"><text>벡터</text></svg>'.encode()},
                     manifest='<item id="svg" href="page.svg" media-type="image/svg+xml"/>', spine='<itemref idref="svg"/><itemref idref="c1"/>')

        result = convert(data, "book.epub")

        assert result.text == "본문"
        assert any("1 page(s)" in warning and "not XHTML" in warning for warning in result.warnings)


class TestLimits:
    def test_chapters_after_the_character_limit_are_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fifty chapters of 1,000 characters against a limit of 2,500: the third chapter crosses it and the other 47 are never
        handed to the HTML extractor."""
        converted: list[int] = []
        original = epub.extract_html

        def counting(data: bytes, options: ConvertOptions):
            converted.append(len(data))
            return original(data, options)

        monkeypatch.setattr(epub, "extract_html", counting)
        data = _epub({f"ch{number}.xhtml": _page(f"<p>{'가' * 1000}</p>") for number in range(50)})

        result = convert(data, "book.epub", ConvertOptions(max_chars=2_500))

        assert result.exceeded
        assert len(converted) == 3

    def test_book_exactly_at_the_limit_is_not_reported_as_exceeded(self) -> None:
        """Two chapters of 5 characters with the limit at 10, and a repeated title page whose heading is counted once."""
        data = _epub({"title.xhtml": _page("<h1>가나다</h1>"), "ch1.xhtml": _page("<h1>가나다</h1><p>라마바사</p>")})
        length = len(convert(data, "book.epub").text)

        result = convert(data, "book.epub", ConvertOptions(max_chars=length))

        assert not result.exceeded
        assert result.text == "# 가나다\n\n라마바사"

    def test_spine_longer_than_the_cap_is_cut_with_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(epub, "_MAX_SPINE_ITEMS", 3)
        data = _epub({f"ch{number}.xhtml": _page(f"<p>{number}장</p>") for number in range(1, 6)})

        result = convert(data, "book.epub")

        assert result.text == "1장\n\n2장\n\n3장"
        assert any("more than 3 chapters" in warning for warning in result.warnings)

    def test_chapter_larger_than_the_part_cap_is_skipped_with_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cap is applied to the bytes actually decompressed, not to the size the archive declares."""
        monkeypatch.setattr(epub, "_MAX_CHAPTER_BYTES", 2_000)
        data = _epub({"big.xhtml": _page(f"<p>{'x' * 5_000}</p>"), "ch1.xhtml": _page("<p>작은 장</p>")})

        result = convert(data, "book.epub")

        assert result.text == "작은 장"
        assert any("1 chapter(s)" in warning for warning in result.warnings)

    def test_spine_repeating_one_chapter_a_hundred_thousand_times_reads_it_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        converted: list[int] = []
        original = epub.extract_html
        monkeypatch.setattr(epub, "extract_html", lambda data, options: converted.append(1) or original(data, options))
        data = _epub({"ch1.xhtml": _page("<p>본문</p>")}, spine='<itemref idref="c1"/>' * 100_000)

        result = convert(data, "book.epub")

        assert result.text == "본문"
        assert len(converted) == 1
