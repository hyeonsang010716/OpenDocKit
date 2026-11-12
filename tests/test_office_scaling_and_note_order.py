"""DOCX/PPTX processing that was not proportional to the input size (many w:t in one run, deeply nested fields, slides with many
inherited placeholders) and DOCX footnote numbers (the order of first reference, not the XML id)."""

import time

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import _docx_with_body


_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _seconds(convert_once) -> float:
    started = time.perf_counter()
    convert_once()
    return time.perf_counter() - started


def _best_seconds(convert_once, runs: int = 3) -> float:
    """Fastest of several measurements — used when looking at a ratio. With a single measurement, a brief OS stall in between
    (hundreds of ms) doubled the ratio, and the test failed on rare occasions during the full unit test run (Deliberate — it does
    not depend on other tests: confirmed unrelated to threads, child processes and a large heap). The fastest value filters out
    transient stalls and still shows processing that itself became slow (about 16 times if quadratic)."""
    return min(_seconds(convert_once) for _ in range(runs))


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

        ratio = _best_seconds(lambda: convert(large, "a.docx")) / max(_best_seconds(lambda: convert(small, "a.docx")), 0.01)

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
    built once per slide/layout."""

    @pytest.mark.timing
    def test_slide_with_many_inherited_placeholders_takes_linear_time(self):
        data = _deck_with_inherited_placeholders(600)

        elapsed = _seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=None)))

        assert elapsed < 3.0

    def test_inherited_positions_still_decide_the_reading_order(self):
        data = _deck_with_inherited_placeholders(3)

        result = convert(data, "a.pptx")

        assert result.text.split("\n\n")[1:] == ["- t2", "- t1", "- t0"]
