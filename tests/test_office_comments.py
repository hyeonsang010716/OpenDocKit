"""DOCX/PPTX comments (review remarks): they were dropped wholesale without a warning. They are put at the end of the document
(slide) as `> Comment(author, on "…"): text` lines."""

from io import BytesIO
import time

from pptx import Presentation
import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import _WORD_NAMESPACES, _docx_with_body


_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _comment(comment_id: str, author: str, text: str, para_id: str = "") -> str:
    paragraph_id = f' w14:paraId="{para_id}"' if para_id else ""
    return (f'<w:comment w:id="{comment_id}" w:author="{author}" w:initials="x">'
            f'<w:p{paragraph_id}><w:r><w:annotationRef/></w:r>{_run(text)}</w:p></w:comment>')


def _docx_with_comments(body: str, comments: str, extended: str = "") -> bytes:
    parts = {"word/comments.xml": f'<w:comments {_WORD_NAMESPACES} xmlns:w14="{_W14}">{comments}</w:comments>'}
    relationships = f'<Relationship Id="rIdC" Type="{_RELATIONSHIPS_NS}/comments" Target="comments.xml"/>'
    types = ('<Override PartName="/word/comments.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>')
    if extended:
        parts["word/commentsExtended.xml"] = f'<w15:commentsEx xmlns:w15="{_W15}">{extended}</w15:commentsEx>'
        relationships += ('<Relationship Id="rIdE" Type="http://schemas.microsoft.com/office/2011/relationships/'
                          'commentsExtended" Target="commentsExtended.xml"/>')
        types += ('<Override PartName="/word/commentsExtended.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"/>')
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships + "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace("</Types>", types + "</Types>"),
    }, add=parts)


def _commented(comment_id: str, before: str, target: str, after: str) -> str:
    return (f'<w:p>{_run(before)}<w:commentRangeStart w:id="{comment_id}"/>{_run(target)}'
            f'<w:commentRangeEnd w:id="{comment_id}"/><w:r><w:commentReference w:id="{comment_id}"/></w:r>'
            f"{_run(after)}</w:p>")


class TestDocxComments:
    def test_comments_are_written_after_the_body_with_their_target_text(self):
        body = _commented("1", "지표명: ", "울산CLX 투입용 바이오 원료", " 활용") + "<w:p>" + _run("둘째 문단") + "</w:p>"
        comments = _comment("1", "검토자", "지표명에 투입처를 적을 필요가 없어 보임")

        result = convert(_docx_with_comments(body, comments), "a.docx")

        assert result.text == ("지표명: 울산CLX 투입용 바이오 원료 활용\n\n둘째 문단\n\n"
                               '> Comment(검토자, on "울산CLX 투입용 바이오 원료"): 지표명에 투입처를 적을 필요가 없어 보임')
        assert result.warnings == []

    def test_target_spanning_paragraphs_is_joined_and_long_target_is_shortened(self):
        long_text = "가" * 80
        body = ('<w:p><w:commentRangeStart w:id="1"/>' + _run("첫 문단") + "</w:p><w:p>" + _run("둘째")
                + '<w:commentRangeEnd w:id="1"/></w:p>' + _commented("2", "", long_text, ""))
        comments = _comment("1", "갑", "두 문단에 단 메모") + _comment("2", "을", "긴 구간")

        result = convert(_docx_with_comments(body, comments), "a.docx")

        assert '> Comment(갑, on "첫 문단 둘째"): 두 문단에 단 메모' in result.text
        assert f'> Comment(을, on "{"가" * 40}…"): 긴 구간' in result.text

    def test_moved_away_and_hidden_text_is_not_part_of_the_range(self):
        """The original position of moved text (w:moveFrom) and a hidden run (w:vanish) are not in the body, yet they were quoted as
        the commented range ('MOVED-AWAYkept'). A range mark inside such content still opens the range."""
        body = ('<w:p><w:commentRangeStart w:id="1"/><w:moveFrom w:id="7" w:author="a">' + _run("옮겨 간 문장")
                + '</w:moveFrom><w:r><w:rPr><w:vanish/></w:rPr><w:t>숨김</w:t></w:r>' + _run("남은 문장")
                + '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p>'
                + '<w:p><w:del w:id="8" w:author="a"><w:commentRangeStart w:id="2"/><w:r><w:delText>지움</w:delText></w:r></w:del>'
                + _run("새 값") + '<w:commentRangeEnd w:id="2"/></w:p>')
        comments = _comment("1", "갑", "확인") + _comment("2", "을", "값 확인")

        result = convert(_docx_with_comments(body, comments), "a.docx")

        assert result.text == ('남은 문장\n\n새 값\n\n> Comment(갑, on "남은 문장"): 확인\n> Comment(을, on "새 값"): 값 확인')

    def test_reply_is_marked_and_follows_its_comment(self):
        body = _commented("1", "", "표지", "")
        comments = _comment("1", "갑", "지표명과 맞출 것", "0A") + _comment("2", "을", "반영했습니다", "0B")
        extended = ('<w15:commentEx w15:paraId="0A" w15:done="0"/>'
                    '<w15:commentEx w15:paraId="0B" w15:paraIdParent="0A" w15:done="0"/>')

        result = convert(_docx_with_comments(body, comments, extended), "a.docx")

        assert result.text.endswith('> Comment(갑, on "표지"): 지표명과 맞출 것\n> Reply(을): 반영했습니다')

    def test_document_without_comments_is_unchanged(self):
        result = convert(_docx_with_body("<w:p>" + _run("본문") + "</w:p>"), "a.docx")

        assert result.text == "본문"

    def test_empty_comment_and_comment_without_an_author_or_target(self):
        comments = _comment("1", "", "작성자 없는 메모") + '<w:comment w:id="2" w:author="갑"><w:p/></w:comment>'

        result = convert(_docx_with_comments("<w:p>" + _run("본문") + "</w:p>", comments), "a.docx")

        assert result.text == "본문\n\n> Comment: 작성자 없는 메모"

    @pytest.mark.timing
    def test_many_open_ranges_over_empty_runs_take_linear_time(self):
        """An empty text (`<w:t/>`) does not grow the span's character count, so a span without an end mark rescanned every open
        span for each empty text (quadratic, 8 s for 8,000). Empty pieces are not appended. Twice the input should take a little
        over twice the time."""
        def elapsed(count: int) -> tuple[float, str]:
            body = ("<w:p>" + "".join(f'<w:commentRangeStart w:id="{index}"/>' for index in range(count))
                    + "<w:r><w:t/></w:r>" * count + "</w:p>")
            data = _docx_with_comments(body, "".join(_comment(str(index), "갑", f"메모{index}") for index in range(count)))
            started = time.perf_counter()
            text = convert(data, "a.docx").text
            return time.perf_counter() - started, text

        small, _ = elapsed(2_000)
        large, text = elapsed(4_000)

        assert large < 3 * small + 0.1
        assert text.endswith("> Comment(갑): 메모3999")

    def test_comments_count_toward_the_character_limit(self):
        comments = _comment("1", "갑", "메모" * 50)
        data = _docx_with_comments("<w:p>" + _run("본문") + "</w:p>", comments)

        result = convert(data, "a.docx", ConvertOptions(max_chars=60))

        assert result.exceeded


_P188 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _deck(texts: list[str]) -> bytes:
    presentation = Presentation()
    for text in texts:
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _with_legacy_comments(data: bytes, comments: list[tuple[str, str]]) -> bytes:
    """Legacy-format comments on slide 1 (ppt/comments/comment1.xml; authors in ppt/commentAuthors.xml)."""
    authors = sorted({author for author, _ in comments})
    author_xml = "".join(f'<p:cmAuthor id="{index}" name="{name}" initials="x" lastIdx="1" clrIdx="0"/>'
                         for index, name in enumerate(authors))
    comment_xml = "".join(f'<p:cm authorId="{authors.index(author)}" dt="2024-01-01T00:00:00" idx="{index}">'
                          f'<p:pos x="10" y="10"/><p:text>{text}</p:text></p:cm>'
                          for index, (author, text) in enumerate(comments, start=1))
    return builder.zip_with(data, edit={
        "ppt/slides/_rels/slide1.xml.rels": lambda xml: xml.replace("</Relationships>", (
            f'<Relationship Id="rIdCm" Type="{_RELATIONSHIPS_NS}/comments" Target="../comments/comment1.xml"/>'
            "</Relationships>")),
        "ppt/_rels/presentation.xml.rels": lambda xml: xml.replace("</Relationships>", (
            f'<Relationship Id="rIdCa" Type="{_RELATIONSHIPS_NS}/commentAuthors" Target="commentAuthors.xml"/>'
            "</Relationships>")),
    }, add={
        "ppt/comments/comment1.xml": f'<p:cmLst xmlns:p="{_PRESENTATION_NS}">{comment_xml}</p:cmLst>',
        "ppt/commentAuthors.xml": f'<p:cmAuthorLst xmlns:p="{_PRESENTATION_NS}">{author_xml}</p:cmAuthorLst>',
    })


def _with_modern_comments(data: bytes) -> bytes:
    """Modern-format comments on slide 1 (including one reply; authors in ppt/authors.xml)."""
    def body(text: str) -> str:
        return f"<p188:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p188:txBody>"

    comment = (f'<p188:cm id="{{C1}}" authorId="{{A1}}" created="2024-01-01T00:00:00">{body("수치 출처 확인")}'
               f'<p188:replyLst><p188:reply id="{{R1}}" authorId="{{A2}}" created="2024-01-02T00:00:00">'
               f'{body("환경부 자료입니다")}</p188:reply></p188:replyLst></p188:cm>')
    return builder.zip_with(data, edit={
        "ppt/slides/_rels/slide1.xml.rels": lambda xml: xml.replace("</Relationships>", (
            '<Relationship Id="rIdMc" Type="http://schemas.microsoft.com/office/2018/10/relationships/comments" '
            'Target="../comments/modernComment_100_1.xml"/></Relationships>')),
        "ppt/_rels/presentation.xml.rels": lambda xml: xml.replace("</Relationships>", (
            '<Relationship Id="rIdAu" Type="http://schemas.microsoft.com/office/2018/10/relationships/authors" '
            'Target="authors.xml"/></Relationships>')),
    }, add={
        "ppt/comments/modernComment_100_1.xml": f'<p188:cmLst xmlns:p188="{_P188}" xmlns:a="{_DRAWING_NS}">{comment}</p188:cmLst>',
        "ppt/authors.xml": (f'<p188:authorLst xmlns:p188="{_P188}"><p188:author id="{{A1}}" name="검토자" initials="x" '
                            'userId="u1" providerId="None"/><p188:author id="{A2}" name="작성자" initials="y" userId="u2" '
                            'providerId="None"/></p188:authorLst>'),
    })


class TestPptxComments:
    def test_legacy_comments_are_written_at_the_end_of_their_slide(self):
        data = _with_legacy_comments(_deck(["매출 현황", "둘째"]), [("검토자", "수치 출처 확인"), ("작성자", "반영함")])

        result = convert(data, "a.pptx")

        assert result.sections[0].text.endswith("> Comment(검토자): 수치 출처 확인\n> Comment(작성자): 반영함")
        assert "Comment" not in result.sections[1].text

    def test_modern_comments_and_replies_are_written_at_the_end_of_their_slide(self):
        result = convert(_with_modern_comments(_deck(["매출 현황"])), "a.pptx")

        assert result.sections[0].text.endswith("> Comment(검토자): 수치 출처 확인\n> Reply(작성자): 환경부 자료입니다")

    def test_deck_without_comments_is_unchanged(self):
        result = convert(_deck(["매출 현황"]), "a.pptx")

        assert "Comment" not in result.text
