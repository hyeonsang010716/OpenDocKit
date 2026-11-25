"""DOCX/PPTX processing that was not proportional to the input size (many w:t in one run, deeply nested fields, number definitions
with many levels, slides with many inherited placeholders) and DOCX footnote numbers (the order of first reference, not the XML id)."""

import time

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import _docx_with_body, _number_level, _number_list, _paragraph
from tests.timing import Fastest, allowed, fastest


_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _seconds(convert_once) -> float:
    started = time.perf_counter()
    convert_once()
    return time.perf_counter() - started


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _docx_with_notes(body: str, footnotes: dict[str, str], endnotes: dict[str, str] | None = None) -> bytes:
    """Document given a body and footnotes (id → text) / endnotes. Ids may start anywhere, as LibreOffice does."""
    def notes(kind: str, contents: dict[str, str]) -> str:
        items = "".join(f'<w:{kind} w:id="{note_id}"><w:p>{_run(text)}</w:p></w:{kind}>' for note_id, text in contents.items())
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:{kind}s xmlns:w="{_WORD_NS}">{items}</w:{kind}s>'

    kinds = {"footnote": footnotes, **({"endnote": endnotes} if endnotes else {})}
    relationships = "".join(f'<Relationship Id="rId{kind}" Type="{_RELATIONSHIPS_NS}/{kind}s" Target="{kind}s.xml"/>'
                            for kind in kinds)
    overrides = "".join(f'<Override PartName="/word/{kind}s.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                        f'wordprocessingml.{kind}s+xml"/>' for kind in kinds)
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships + "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace("</Types>", overrides + "</Types>"),
    }, add={f"word/{kind}s.xml": notes(kind, contents) for kind, contents in kinds.items()})


class TestDocxLongRunsAndDeepFields:
    """For every w:t in a run the whole run text was joined again to see whether the run holds only boxes (`_painted_box`), taking
    time quadratic in the number of w:t (312 s for a 38KB document). Nested complex fields scanned the whole field stack for every
    text piece and joined all the following pieces each time a field ended, taking time quadratic in the depth (49 s for 32,000
    levels)."""

    @pytest.mark.timing
    def test_run_with_many_text_pieces_takes_linear_time(self):
        body = "<w:p><w:r>" + "<w:t>□</w:t>" * 20_000 + "</w:r></w:p>"
        data = _docx_with_body(body)

        elapsed = _seconds(lambda: convert(data, "a.docx"))

        assert elapsed < 2.0

    def test_box_run_split_into_pieces_is_still_painted_when_highlighted(self):
        body = ('<w:p><w:r><w:rPr><w:highlight w:val="black"/></w:rPr><w:t>□</w:t><w:t> </w:t><w:t>☐</w:t></w:r>'
                '<w:r><w:rPr><w:highlight w:val="black"/></w:rPr><w:t>□</w:t><w:t>글</w:t></w:r></w:p>')

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "■ ☒□글"

    @pytest.mark.timing
    def test_deeply_nested_fields_take_linear_time(self):
        def nested(depth: int) -> bytes:
            begin = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>X</w:instrText></w:r>'
                     '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>a</w:t></w:r>')
            return _docx_with_body("<w:p>" + begin * depth + '<w:r><w:fldChar w:fldCharType="end"/></w:r>' * depth + "</w:p>")

        small, large = nested(2000), nested(8000)

        ratio = fastest(lambda: convert(large, "a.docx")) / max(fastest(lambda: convert(small, "a.docx")), 0.01)

        assert ratio < 8  # linear gives about 4 times for 4 times the input, quadratic about 16 times

    def test_field_nested_in_an_instruction_and_blank_equation_field_are_unchanged(self):
        """After the switch to the fast check, the result of a field inside an instruction is still left out, and an EQ field with no
        result text (enclosed character) still gives its instruction as text."""
        inner = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> NUMPAGES </w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + _run("4") + '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        outer = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> IF </w:instrText></w:r>' + inner
                 + '<w:r><w:instrText> &gt; 1 "다음" "" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                 + _run("다음") + '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        circled = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>eq \\o\\ac(○,21)</w:instrText></w:r>'
                   '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        body = "<w:p>" + _run("[") + outer + _run("] 제") + circled + _run("조") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "[다음] 제㉑조"


class TestDocxFootnoteNumbersFollowTheOrderOfReference:
    """The XML id was used as the footnote mark, so the first footnote of a document saved by LibreOffice (separators take ids 0 and
    1, so footnotes start at 2) was `[^2]`. As for HWP/HWPX, numbers are assigned from 1 in the order of first reference, and
    endnotes are numbered separately from `endnote1`. Referring to the same footnote twice gives the same number."""

    def test_footnotes_are_numbered_in_the_order_they_are_first_referenced(self):
        body = ("<w:p>" + _run("가") + '<w:r><w:footnoteReference w:id="5"/></w:r>' + _run("나")
                + '<w:r><w:footnoteReference w:id="2"/></w:r>' + _run("다") + '<w:r><w:footnoteReference w:id="5"/></w:r></w:p>')
        data = _docx_with_notes(body, {"2": "둘째 각주", "5": "첫째 각주"})

        result = convert(data, "a.docx")

        assert result.text == "가[^1]나[^2]다[^1]\n\n[^1]: 첫째 각주\n[^2]: 둘째 각주"

    def test_endnotes_have_their_own_sequence(self):
        body = ("<w:p>" + _run("가") + '<w:r><w:endnoteReference w:id="7"/></w:r>' + _run("나")
                + '<w:r><w:footnoteReference w:id="3"/></w:r></w:p>')
        data = _docx_with_notes(body, {"3": "각주"}, {"7": "미주"})

        result = convert(data, "a.docx")

        assert result.text == "가[^endnote1]나[^1]\n\n[^endnote1]: 미주\n[^1]: 각주"


def _deck_with_inherited_placeholders(count: int) -> bytes:
    """Presentation with `count` body placeholders on the blank layout (the 7th) (idx from 100; the blank layout's date, footer and
    slide number use 10–12. The later the idx, the higher the position), and on the slide only placeholders with the same idx
    and no position. Position and bullets are all inherited."""
    from io import BytesIO

    from pptx import Presentation

    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    buffer = BytesIO()
    presentation.save(buffer)

    def placeholder(index: int, body: str) -> str:
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="{index + 10}" name="p{index}"/><p:cNvSpPr/><p:nvPr><p:ph type="body" '
                f'idx="{index + 100}"/></p:nvPr></p:nvSpPr>{body}</p:sp>')

    layout = "".join(placeholder(index, f'<p:spPr><a:xfrm><a:off x="0" y="{(count - index) * 200_000}"/>'
                                        '<a:ext cx="100" cy="100"/></a:xfrm></p:spPr>') for index in range(count))
    slide = "".join(placeholder(index, f"<p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>t{index}</a:t></a:r></a:p></p:txBody>")
                    for index in range(count))
    return builder.zip_with(buffer.getvalue(), {
        "ppt/slides/slide1.xml": lambda xml: xml.replace("</p:spTree>", slide + "</p:spTree>"),
        "ppt/slideLayouts/slideLayout7.xml": lambda xml: xml.replace("</p:spTree>", layout + "</p:spTree>"),
    })


class TestPptxManyInheritedPlaceholders:
    """Every time a placeholder's position or bullet was inherited, python-pptx scanned the layout's placeholders from the start, so
    a slide with many inheriting placeholders took time quadratic in their number (89 s for 1,000). An idx → placeholder map is
    built once per layout/master. Kept per slide, every slide walked all placeholders of its layout again: 400 slides on a layout
    of 4,000 placeholders took 49 s."""

    @pytest.mark.timing
    def test_slide_with_many_inherited_placeholders_takes_linear_time(self):
        data = _deck_with_inherited_placeholders(600)

        elapsed = _seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=None)))

        assert elapsed < 3.0

    @pytest.mark.timing
    def test_many_slides_on_a_large_layout_take_linear_time(self):
        layout = "".join(_layout_placeholder(index, index) for index in range(3_000))
        data = _deck_on_layouts({7: layout}, [(7, _slide_placeholder(0, "x"))] * 300)

        elapsed = _seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=None)))

        assert elapsed < allowed(2.0)

    def test_inherited_positions_still_decide_the_reading_order(self):
        data = _deck_with_inherited_placeholders(3)

        result = convert(data, "a.pptx")

        assert result.text.split("\n\n")[1:] == ["- t2", "- t1", "- t0"]

    def test_each_slide_inherits_from_its_own_layout_and_keeps_its_own_positions(self):
        """Two layouts give the same placeholder indexes opposite positions. A slide on either inherits from its own layout, and a
        slide whose placeholder has its own position keeps it, without changing what the next slide on that layout inherits."""
        first_up = _layout_placeholder(0, 1) + _layout_placeholder(1, 2)
        second_up = _layout_placeholder(0, 2) + _layout_placeholder(1, 1)
        own_position = '<p:spPr><a:xfrm><a:off x="0" y="600000"/><a:ext cx="100" cy="100"/></a:xfrm></p:spPr>'
        slides = [(7, _slide_placeholder(0, "a0") + _slide_placeholder(1, "a1")),
                  (6, _slide_placeholder(0, "b0") + _slide_placeholder(1, "b1")),
                  (7, _slide_placeholder(0, "c0", own_position) + _slide_placeholder(1, "c1")),
                  (7, _slide_placeholder(0, "d0") + _slide_placeholder(1, "d1")),
                  (6, _slide_placeholder(0, "e0", own_position) + _slide_placeholder(1, "e1"))]
        data = _deck_on_layouts({7: first_up, 6: second_up}, slides)

        result = convert(data, "a.pptx")

        texts = [block for block in result.text.split("\n\n") if block.startswith("- ")]
        assert texts == ["- a0", "- a1", "- b1", "- b0", "- c1", "- c0", "- d0", "- d1", "- e1", "- e0"]


class TestPptxManyShapesWrappedInAlternateContent:
    """Each `mc:AlternateContent` on a slide was replaced by its shapes with `parent.index` + `parent.insert`, which walk the
    sibling list, so the unwrapping took time quadratic in the number of wrapped shapes: 40,000 of them took 19.6 s against
    3.5 s for the same shapes unwrapped, before any character limit was checked. The shapes are moved in front of the wrapper
    instead."""

    @staticmethod
    def _shape_tree(count: int) -> object:
        from lxml import etree

        wrapper = ('<mc:AlternateContent><mc:Choice Requires="a14"><p:sp><p:nvSpPr><p:cNvPr id="{0}" name="t{0}"/></p:nvSpPr>'
                   '<p:txBody><a:bodyPr/><a:p><a:r><a:t>s{0}</a:t></a:r></a:p></p:txBody></p:sp></mc:Choice>'
                   '<mc:Fallback><p:sp><p:nvSpPr><p:cNvPr id="{0}" name="f{0}"/></p:nvSpPr></p:sp></mc:Fallback>'
                   '</mc:AlternateContent>')
        return etree.fromstring(
            '<p:spTree xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            + "".join(wrapper.format(index) for index in range(count)) + "</p:spTree>")

    @pytest.mark.timing
    def test_unwrapping_many_wrapped_shapes_takes_linear_time(self):
        from opendockit.extractors.pptx import _unwrap_alternate_content

        def seconds(count: int) -> float:
            timer = Fastest(6)  # tens of milliseconds: the fastest of three was passed once on a macOS runner
            for _ in timer:
                tree = self._shape_tree(count)
                with timer:
                    _unwrap_alternate_content(tree, None)
            return timer.seconds

        ratio = seconds(12_000) / max(seconds(3_000), 0.005)

        assert ratio < 8  # linear gives about 4 times for 4 times the input, quadratic about 16 times

    def test_wrapped_shapes_keep_their_order_and_choice(self):
        from opendockit.extractors.pptx import _unwrap_alternate_content

        tree = self._shape_tree(3)

        _unwrap_alternate_content(tree, None)

        assert [shape.find(".//{*}cNvPr").get("name") for shape in tree] == ["t0", "t1", "t2"]


def _layout_placeholder(index: int, rank: int) -> str:
    """Body placeholder of a layout with idx `100 + index`, placed `rank` steps of 200,000 EMU down the slide."""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{index + 10}" name="p{index}"/><p:cNvSpPr/><p:nvPr><p:ph type="body" '
            f'idx="{index + 100}"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="{rank * 200_000}"/>'
            '<a:ext cx="100" cy="100"/></a:xfrm></p:spPr></p:sp>')


def _slide_placeholder(index: int, text: str, properties: str = "<p:spPr/>") -> str:
    """Slide placeholder with idx `100 + index` holding `text`; without its own frame it inherits the layout's position."""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{index + 10}" name="p{index}"/><p:cNvSpPr/><p:nvPr><p:ph type="body" '
            f'idx="{index + 100}"/></p:nvPr></p:nvSpPr>{properties}<p:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
            '</p:txBody></p:sp>')


def _deck_on_layouts(layouts: dict[int, str], slides: list[tuple[int, str]]) -> bytes:
    """Presentation whose slides are each (layout file number, shapes) — 7 is the blank layout, 6 'Title Only' — with the given
    shapes added to the layout files."""
    from io import BytesIO

    from pptx import Presentation

    presentation = Presentation()
    for layout, _shapes in slides:
        presentation.slides.add_slide(presentation.slide_layouts[layout - 1])
    buffer = BytesIO()
    presentation.save(buffer)

    def append(shapes: str):
        return lambda xml: xml.replace("</p:spTree>", shapes + "</p:spTree>")

    edits = {f"ppt/slides/slide{number}.xml": append(shapes) for number, (_layout, shapes) in enumerate(slides, 1)}
    edits.update({f"ppt/slideLayouts/slideLayout{layout}.xml": append(shapes) for layout, shapes in layouts.items()})
    return builder.zip_with(buffer.getvalue(), edits)


def _numbering_with_levels(levels: str, lists: int) -> str:
    """Numbering definition 0 with the given w:lvl elements, and `lists` w:num (numId from 1) pointing at it, each with a level
    override that holds its own level 0 definition."""
    override = '<w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:lvlOverride>'
    return (f'<w:abstractNum w:abstractNumId="0">{levels}</w:abstractNum>'
            + "".join(_number_list(num_id, 0, override) for num_id in range(1, lists + 1)))


class TestDocxNumberingLevelsPastEight:
    """The levels of a number definition (w:abstractNum/w:lvl) were all kept, whatever their index, and every w:num with a level
    override (w:lvlOverride/w:lvl) copied them: 100,000 levels and 5,000 w:num (312 KB) took 44 s. A list level is clamped to 0–8
    before it is looked up, so only those levels are kept now; the paragraph style a level points at is read for any level."""

    @pytest.mark.timing
    def test_many_levels_and_overriding_lists_take_linear_time(self):
        levels = "".join(f'<w:lvl w:ilvl="{index}"/>' for index in range(40_000))
        data = _docx_with_body(_paragraph("x", 1), numbering=_numbering_with_levels(levels, 4_000))

        elapsed = _seconds(lambda: convert(data, "a.docx"))

        assert elapsed < allowed(2.0)

    def test_only_levels_zero_to_eight_are_kept(self):
        from io import BytesIO

        from docx import Document

        from opendockit.extractors._docx_parts import _load_numbering

        levels = "".join(f'<w:lvl w:ilvl="{index}"/>' for index in (-1, 0, 8, 9, 500))
        data = _docx_with_body(_paragraph("x", 1), numbering=_numbering_with_levels(levels, 2))

        lists = _load_numbering(Document(BytesIO(data)))

        assert {num_id: sorted(number_list.levels) for num_id, number_list in lists.items()} == {"1": [0, 8], "2": [0, 8]}
        assert all(sorted(number_list.indents) == [0, 8] for number_list in lists.values())

    def test_levels_past_eight_still_read_as_level_eight(self):
        """A paragraph at level 12, or in a style a level-12 definition points at, is numbered with level 8 as before."""
        levels = (_number_level(0, "%1.") + _number_level(8, "(%9)") + _number_level(9, "nine %10")
                  + _number_level(12, "twelve %13", extra='<w:pStyle w:val="Deep"/>'))
        styles = ('<w:style w:type="paragraph" w:styleId="Deep"><w:name w:val="Deep"/>'
                  '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>')
        body = _paragraph("top", 1) + _paragraph("deep", 1, 12) + _paragraph("styled", style="Deep") + _paragraph("nine", 1, 9)
        data = _docx_with_body(body, numbering=f'<w:abstractNum w:abstractNumId="0">{levels}</w:abstractNum>' + _number_list(1),
                               styles=styles)

        result = convert(data, "a.docx")

        deepest = "  " * 8
        assert result.text == f"1. top\n{deepest}- (1) deep\n{deepest}- (2) styled\n{deepest}- (3) nine"
