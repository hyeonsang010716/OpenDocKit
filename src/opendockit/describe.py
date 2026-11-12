"""Image → text (vision model). Describes and OCRs images with LangChain `ChatOpenAI` and fills the result into their place in the body.

Conversion (`convert`) is synchronous and offline; this is the only module that uses the network. A description failure is
not a conversion failure — that image keeps its placeholder text and only a warning is added.
"""

import asyncio
import base64
from collections import Counter
from dataclasses import replace
import re
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from opendockit.converter import image_length_bound, render
from opendockit.markdown import clean_text
from opendockit.models import ConversionResult, DocumentImage, ImageKind, image_marker_pattern


# The default vision model — a project decision (the GPT-5.6 tier). When changing it, check it again against the live OpenAI API
# (it passed after the switch to terra on 2026-10-07). History: the previous default, gpt-6-luna, was chosen with 4 runs on a dense
# 40-line A4 scan and 8 runs on smudged text — gpt-4o-mini made up numbers (0 of the 40 lines matched the original), and
# gpt-4.1-mini and gpt-4o made up characters they could not read (docs/design.md).
# The SKIP and text-check defenses below were observed with gpt-4o-mini, but they stay regardless of the model.
DEFAULT_MODEL = "gpt-5.6-terra"

_COMMON_RULES = """\
- Transcribe all the text in the image exactly as written, leaving nothing out. Do not translate or summarize it.
- Write a table as a Markdown table, a chart as its title, axes, legend and values in a Markdown table, and a diagram or flowchart \
as a list of its components and how they are connected.
- For a value that is unreadable or uncertain, do not guess; write "(illegible)".
- Do not make up information that is not visible in the image (year, unit, source, company name, etc.).
- Write only the content, with no preamble ("This image shows…"), no closing remark and no code block. Write Markdown in the same \
language as the text in the image (or, if it has no text, the language of the surrounding document context)."""

FIGURE_PROMPT = f"""\
Turn an image from a document into text that can be used for search and question answering.
- On the first line, summarize what the image is in one sentence (e.g. "A bar chart comparing sales by region").
- For a photo, describe concisely only the subjects and situation that matter in the document context.
{_COMMON_RULES}
- If there is even one readable letter or number, never answer SKIP; transcribe that text.
- Only when the image is an icon, decorative pattern, background or blank image with no text at all and no information, output \
the single word SKIP and nothing else."""

PAGE_PROMPT = f"""\
This is a (scanned) document page whose text exists only as an image. Transcribe the page content in reading order, leaving nothing out.
- Write headings with #, lists with -, and tables as Markdown tables. Leave out page numbers and running headers.
- Write a picture or figure on the page in its place as "[Figure] one-line description".
{_COMMON_RULES}"""

STANDALONE_PROMPT = f"""\
Turn an image uploaded by the user into text that can be used for search and question answering.
- On the first line, summarize what the image is in one sentence.
- For an image that is mainly text, such as a document, screen or sign, transcribe all of the text; for a photo, describe the \
subjects, situation and notable features.
{_COMMON_RULES}"""

# The check prompt asked once more for a picture that got a "no information" answer. It makes the model look only at the text,
# with no description, which reduces the wobble.
TEXT_CHECK_PROMPT = """\
Transcribe every letter and number visible in the image exactly as written. Write only the transcribed text, with no translation, \
no description and no preamble.
If there is no text at all, output the single word NONE and nothing else."""

# The note attached for a long picture cut into tiles (`DocumentImage.tiles`). Neighboring tiles overlap a little.
_TILES_NOTE = ("The image is long, so it is sent as {count} tile(s) cut in order along its long side (top → bottom or left → right). "
               "Neighboring tiles overlap slightly at their edges. Treat them as one image and transcribe them continuously, "
               "without writing the overlapping part twice.")
_PROMPTS = {ImageKind.FIGURE: FIGURE_PROMPT, ImageKind.PAGE: PAGE_PROMPT, ImageKind.STANDALONE: STANDALONE_PROMPT}
# Answers taken to mean a picture with no information: "SKIP" and its decorated forms ("(SKIP)", "`SKIP`", "**SKIP**"),
# "(illegible)" / "(판독 불가)", and short answers that only claim emptiness such as "빈 이미지" (empty image), "내용 없음" (no content)
# or "blank". The real gpt-4o-mini answered like this for patterns with no text, and sometimes for pictures of text too.
# Such an answer does not remove the picture right away; the text check is asked once more (OpenAIImageDescriber.describe).
_NO_CONTENT = re.compile(
    r"(?:\W|skip|none|판독\s*불가|illegible|unreadable|빈\s*(?:이미지|그림|화면)|내용\s*(?:이\s*)?없음|비어\s*있음"
    r"|blank(?:\s*image)?|empty(?:\s*image)?)*",
    re.IGNORECASE,
)
# The model words its claim of emptiness differently every time ("비어 있는 이미지.\n\nSKIP" (an empty image), "이 이미지는 비어 있는
# 도식입니다" (this image is an empty diagram)). A short answer containing such an expression gets the text check. A long description
# (one describing an empty form, say) has content, so it is left alone.
_EMPTY_CLAIM = re.compile(r"비어\s*있|빈\s*(?:이미지|그림|화면|도식)|내용\s*(?:이\s*)?없|판독\s*불가|illegible|unreadable|blank|empty|skip",
                          re.IGNORECASE)
_EMPTY_CLAIM_MAX_CHARS = 80
_MISSING_EXTRA = 'OpenAIImageDescriber needs the optional vision dependencies: pip install "opendockit[openai]"'


class ImageDescriber(Protocol):
    """Turn one image into text. Return "SKIP" for decoration with no information."""

    async def describe(self, image: DocumentImage) -> str: ...


class OpenAIImageDescriber:
    """Describe images with LangChain `ChatOpenAI` (a vision model).

    Pass an already-built LangChain chat model as `model` and its configuration (key, retries, timeout) is used as is (this
    assumes a server running on one event loop). Without one, a new `ChatOpenAI` is created from `model_name`; it reads
    `OPENAI_API_KEY` from the environment and needs the optional extra `pip install "opendockit[openai]"`.
    `detail="high"` is needed for OCR of small text and tables. Use "low" to cut cost (large text and rough descriptions only).

    Deliberate: a model created here is made **separately for each event loop** and given its own HTTP client. The default
    async client of langchain_openai is a process-global cache bound to the loop that first used it, so workers and scripts
    that call asyncio.run several times failed with "Event loop is closed" from the second call on
    (Pinned by: test_image.py::test_openai_describer_uses_separate_http_client_per_event_loop).
    """

    def __init__(self, model: Any = None, *, model_name: str = DEFAULT_MODEL, detail: str = "high",
                 timeout: float = 120.0, max_retries: int = 2) -> None:
        self._model = model
        self._model_name = model_name
        self._timeout = timeout
        self._max_retries = max_retries
        self._detail = detail
        self._per_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = WeakKeyDictionary()

    def _chat_model(self) -> Any:
        if self._model is not None:
            return self._model
        loop = asyncio.get_running_loop()
        model = self._per_loop.get(loop)
        if model is None:
            try:
                import httpx
                from langchain_openai import ChatOpenAI
            except ImportError as exc:
                raise ImportError(_MISSING_EXTRA) from exc

            model = ChatOpenAI(model=self._model_name, timeout=self._timeout, max_retries=self._max_retries,
                               http_async_client=httpx.AsyncClient(timeout=self._timeout))
            self._per_loop[loop] = model
        return model

    def check(self) -> None:
        """Build the chat model now, so a missing `opendockit[openai]` extra or API key raises here instead of later.

        `describe_images` treats every failed call as a warning on that image, which hides a setup problem behind
        "could not be described". Call this once at startup (inside a running event loop) to fail fast; the CLI does
        (Pinned by: test_cli.py::TestDescribeSetup). A model passed in by the caller is not checked.
        """
        self._chat_model()

    async def aclose(self) -> None:
        """Close the HTTP client created here for the current event loop. Call it before ending the loop (a CLI with a single asyncio.run, etc.).

        The client of a closed loop cannot be closed without that loop, so without this call the connections stay open until
        the loop is gone. A model passed in belongs to the caller and is not closed
        (Pinned by: test_image.py::test_openai_describer_closes_the_http_client_of_the_current_event_loop).
        """
        model = self._per_loop.pop(asyncio.get_running_loop(), None)
        if model is not None:
            await model.http_async_client.aclose()

    async def describe(self, image: DocumentImage) -> str:
        """When a picture is judged "no information" (SKIP, illegible, an empty-claim answer), ask once more with a text-only check.

        Deliberate: the real gpt-4o-mini answered "(판독 불가)" (illegible) about a third of the time even for pictures of sharp
        text. Trusting the first answer and removing the picture as decoration loses the content. SKIP is returned only when
        both passes say there is no text (Pinned by: test_image.py::TestNoContentSecondOpinion).
        The first answer is replaced by the text-check answer only when the first answer did not read the picture (a
        no-information answer, or an "it is empty" claim sharing no word with the text the check found). If they share words,
        the first answer is a description that did read the picture, so it is kept, and the found text is appended only when
        it contains something the first answer lacks (`_merge_second_look`).
        A blank answer ("", whitespace, code fences only) is not a 'no text' verdict; it is no answer. Decoration (SKIP) is only
        for when both passes **answered** that there is no text; if the text check is blank, or the first answer is blank and the
        text check says NONE, a blank answer is returned so that `describe_images` records a failure (calling it again asks
        again). Previously a blank answer became SKIP through `is_no_content("")` and the picture was removed without a warning
        (Pinned by: test_image.py::TestNoContentSecondOpinion::test_empty_answer_in_either_pass_is_a_failure_not_a_decoration).
        """
        prompt = _PROMPTS[image.kind]
        if image.kind is ImageKind.FIGURE and image.context:
            prompt += f"\n\nDocument context for reference (the body text right before the image):\n{image.context}"
        if image.alt:
            prompt += f"\n\nAlt text provided by the author: {image.alt}"
        answer = await self._ask(prompt, image)
        if image.kind is not ImageKind.FIGURE or not needs_second_look(answer):
            return answer

        text = await self._ask(TEXT_CHECK_PROMPT, image)
        if _is_blank(text) or (is_no_content(text) and _is_blank(answer)):
            return ""
        return "SKIP" if is_no_content(text) else _merge_second_look(answer, text)

    async def _ask(self, prompt: str, image: DocumentImage) -> str:
        try:
            from langchain_core.messages import HumanMessage
        except ImportError as exc:
            raise ImportError(_MISSING_EXTRA) from exc

        # A very tall picture is sent as its tiles, cut in order, instead of the downscaled whole — shrunk whole, the text cannot
        # be read (`DocumentImage.tiles`).
        pictures = image.tiles or (image.data,)
        if image.tiles:
            prompt += f"\n\n{_TILES_NOTE.format(count=len(image.tiles))}"
        message = HumanMessage(content=[
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{base64.b64encode(picture).decode()}",
                                                  "detail": self._detail}} for picture in pictures),
        ])
        response = await self._chat_model().ainvoke([message])
        return response.text


async def describe_images(result: ConversionResult, describer: ImageDescriber, *,
                          max_concurrency: int = 8) -> ConversionResult:
    """Describe `result.images` and return a new result with the descriptions put in their place in the body (the original is not changed).

    Images already described or judged decorative are not asked about again. An image whose call failed is marked `failed`
    and gets a warning; calling this function again with the returned result retries only the failed images
    (Pinned by: test_image.py::test_describing_again_retries_only_failed_images_and_clears_warning).

    The character count is added up as each description is filled in; once it goes over the limit (`options.max_chars`), the
    remaining calls are cancelled and the result comes back as `exceeded`. Nothing is done to a result that is already over
    (Pinned by: test_image.py::test_description_stops_once_the_character_limit_is_exceeded).
    """
    finished: dict[int, DocumentImage] = {}
    limit = result.options.max_chars
    headroom = None if limit is None else limit - _floor_length(result, finished)
    if headroom is not None and headroom < 0:
        return result
    semaphore = asyncio.Semaphore(max(max_concurrency, 1))

    async def run(image: DocumentImage) -> DocumentImage:
        if image.description or image.decorative:
            return image
        async with semaphore:
            try:
                text = _answer_text(await describer.describe(image))
            except Exception:  # noqa: BLE001 — description is best-effort. Leave the placeholder text and report it with a warning.
                return replace(image, failed=True)
            except asyncio.CancelledError:
                # A CancelledError the describer (a library inside it) raised by itself is a failure of this picture. Propagated
                # as is, it killed the whole aconvert although the outside was not cancelled, losing the descriptions of the
                # other pictures too. Only a real cancellation (cancel arrived at this task) is propagated
                # (Pinned by: test_image.py::TestAuditedImageGaps::test_describer_that_raises_cancelled_error_itself_fails_only_that_image,
                # test_cancelling_aconvert_still_cancels_it).
                current = asyncio.current_task()
                if current is None or current.cancelling():
                    raise
                return replace(image, failed=True)

        text = clean_text(_strip_code_fence(text))
        if not text or (image.kind is not ImageKind.FIGURE and is_no_content(text)):
            # A blank answer ("", None) is not an answer (a truncated response, empty content) — treated as decoration like SKIP,
            # the picture vanished without a warning. A SKIP-like answer for a scanned page or an uploaded picture is the same:
            # keeping the text makes 'SKIP' the body, and removing it makes the whole document vanish. Report a failure; calling
            # again retries
            # (Pinned by: test_image.py::TestDescribeImages::test_completely_empty_answer_for_a_figure_is_a_failure_not_a_decoration,
            # test_skip_answer_for_an_uploaded_image_is_a_failure, test_skip_answer_for_a_scanned_page_is_a_failure,
            # TestStandaloneImage::test_empty_answer_for_uploaded_image_is_reported_as_failed).
            return replace(image, failed=True)
        if image.kind is ImageKind.FIGURE and is_no_content(text):
            return replace(image, decorative=True, failed=False)
        return replace(image, description=text, failed=False)

    # When the placeholder of one picture appears several times, its description goes in that many times. The bound on the
    # increase has to be multiplied by that count for the stopping point to be the same even when rendering is skipped
    # (Pinned by: test_image.py::TestResourceBounds::test_description_stops_at_the_same_image_when_a_placeholder_appears_more_than_once).
    pattern = image_marker_pattern(result.marker_key)
    occurrences = Counter(int(found) for section in result.sections for found in pattern.findall(section.template))
    tasks = [asyncio.ensure_future(run(image)) for image in result.images if _is_pending(image)]
    try:
        for next_done in asyncio.as_completed(tasks):
            image = await next_done
            finished[image.id] = image
            if limit is None or headroom is None:
                continue

            # Rendering the body takes time proportional to the document length, so doing it per picture is quadratic in the number
            # of pictures (25 seconds for 3,332 pictures). Only subtract the most characters one picture can add, and render for
            # an exact decision only when the headroom runs out. The stopping point is the same as when rendering every time
            # (Pinned by: test_image.py::TestResourceBounds::test_description_stops_at_exactly_the_image_that_crosses_the_limit).
            headroom -= image_length_bound(image, result.fallbacks) * max(occurrences[image.id], 1)
            if headroom < 0:
                headroom = limit - _floor_length(result, finished)
                if headroom < 0:
                    break
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return render(replace(result, images=[finished.get(image.id, image) for image in result.images]))


def _answer_text(answer: Any) -> str:
    """Turn the describer's answer into text. For a list of content parts, like a LangChain chat model's `content`, join only the text parts.

    An answer that is not text fails only that picture (exception → `failed`). Previously an AttributeError leaked out of
    `aconvert` and every other picture was lost too
    (Pinned by: test_image.py::TestDescribeImages::test_describer_answer_that_is_not_text_fails_only_that_image).
    """
    if answer is None or isinstance(answer, str):
        return answer or ""
    if isinstance(answer, list):
        parts = []
        for part in answer:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif not (isinstance(part, dict) and "type" in part):
                raise TypeError("unknown answer part")
        return "".join(parts)
    raise TypeError("answer is not text")


def _is_pending(image: DocumentImage) -> bool:
    return not (image.description or image.decorative)


def _floor_length(result: ConversionResult, finished: dict[int, DocumentImage]) -> int:
    """The character count assuming every picture not yet described is removed as decoration. If this is over the limit, the result surely is.

    Deliberate: the decision is not made on the body length at this moment. When a picture is judged decorative, the `[Image]`
    in its place is removed and the count goes down, so a result that looks over the limit midway can be under it when all is
    done. The body with every remaining picture removed is actually rendered, and the work stops only if that is still over
    (Pinned by: test_image.py::test_placeholders_of_images_that_turn_out_decorative_do_not_count_against_the_limit).
    Alt text is removed as well: a picture with alt text keeps '[Image: …]' even when decorative, but a short description can
    be shorter than that, so the removed form is the floor
    (Pinned by: test_image.py::TestDecorativePictureWithAltText::test_limit_is_exact_when_a_picture_with_alt_text_gets_any_answer).
    """
    images = [finished.get(image.id) or (replace(image, decorative=True, alt=None)
                                         if _is_pending(image) and image.kind is ImageKind.FIGURE else image)
              for image in result.images]
    return render(replace(result, images=images)).char_count


def needs_second_look(text: str) -> bool:
    stripped = _strip_code_fence(text or "").strip()
    return is_no_content(stripped) or (len(stripped) <= _EMPTY_CLAIM_MAX_CHARS and bool(_EMPTY_CLAIM.search(stripped)))


def _merge_second_look(first: str, text: str) -> str:
    """Merge the first answer with the text the text check transcribed (`text`, which is not a no-information answer).

    Replacing the first answer wholesale turned a short description that missed just one value, such as "분기별 매출 막대 차트:
    1분기 120, …, 3분기 (판독 불가)" (quarterly sales bar chart: Q1 120, …, Q3 (illegible)), into "매출 120 135" (sales 120 135)
    and the summary was lost (Pinned by: test_image.py::TestNoContentSecondOpinion::
    test_substantive_first_answer_is_not_replaced_by_the_text_check, test_text_check_adds_what_a_substantive_first_answer_missed).
    Known accepted gap: when the text check says NONE, the result is SKIP whatever the first answer was (except a blank answer —
    a failure); both are taken to have said there is no text.
    """
    first_words = _words(first)
    found = _words(text)
    if is_no_content(first) or not first_words & found:
        return text
    if found <= first_words:
        return first
    return f"{first}\n\n{text}"


def _words(text: str) -> set[str]:
    """The words (lowercased) without the 'it is empty' claim expressions. Used to compare the first answer with the text-check
    answer to see whether the first answer read the picture."""
    return set(re.findall(r"\w+", _EMPTY_CLAIM.sub(" ", _strip_code_fence(text)).lower()))


def _is_blank(text: str) -> bool:
    """Whether there is no answer (empty text, whitespace, code-fence lines only). Different from an answer saying 'no text' (`is_no_content`)."""
    return not _strip_code_fence(text or "").strip()


def is_no_content(text: str) -> bool:
    return bool(_NO_CONTENT.fullmatch(_strip_code_fence(text or "").strip()))


def _strip_code_fence(text: str) -> str:
    """Remove the ``` lines the model put in against the instructions (whether they wrap everything or a part in the middle). The
    description goes into a quote block, so a code block inside it is noise to the LLM (the real gpt-4o-mini wrapped only the
    body after the summary)."""
    return "\n".join(line for line in text.split("\n") if not line.strip().startswith("```"))
