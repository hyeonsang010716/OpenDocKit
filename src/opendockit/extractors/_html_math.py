"""MathML and equations kept as pictures or scripts → LaTeX."""

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4.element import PageElement, PreformattedString

from opendockit.extractors._html_base import _BLOCK_TAGS, _HEADINGS
from opendockit.extractors._html_dom import _decompose_all, _text
from opendockit.extractors._mathml import MATH_IGNORED, base_child, literal, mathml_structure, text_leaf


# MathML (`_put_math`): annotation encodings of the LaTeX the document wrote, the opening of Wikipedia's wrapper, script
# elements → marks, elements that are not text, and the maximum depth unfolded LaTeX-style (recursion).
_TEX_ENCODINGS = {"application/x-tex", "tex", "latex", "application/x-latex", "text/x-tex"}
_DISPLAY_STYLE = re.compile(r"\{\\displaystyle(?![A-Za-z])\s*")
_MAX_MATH_DEPTH = 50
# Page blocks that cannot appear inside an equation (`_maths_holding_page_blocks`).
_PAGE_BLOCKS_IN_MATH = [*_BLOCK_TAGS, *_HEADINGS, "table", "ul", "ol", "li", "pre"]


def _put_math(soup: BeautifulSoup) -> None:
    """Replaces MathML (`<math>`) with the same `$LaTeX$` notation as Office and Hangul (HWP) equations
    (`_omml.render_omml`). Joining only the characters read `r²π` as `r2π` and the fraction 3/4 as `34`
    (Pinned by: test_core_html.py::TestMathMl).

    The LaTeX the document wrote is used first: `<annotation encoding="application/x-tex">` (LaTeXML, MathJax) or, without it,
    `alttext` (Wikipedia's `{\\displaystyle …}` is stripped from either, `_unwrapped`). The whole element is replaced so that
    presentation MathML and the annotation text do not run together and give the same expression twice. With neither, the
    presentation MathML is unfolded LaTeX-style (`_mathml_latex`). The replacement method is the same as `_put_form_values`
    (in-place span, linear time).
    """
    for math in _maths_holding_page_blocks(soup):
        math.unwrap()
    # Equations inside an equation that was replaced: nothing of them is used. `clear()` only detaches the direct children, so
    # a `<math>` further down still had a parent and was converted again, each one walking everything below it
    # (Pinned by: test_audit_html_text.py::TestEquationsNestedInsideEquations)
    replaced: set[int] = set()
    for math in soup.find_all("math"):
        if id(math) in replaced:
            continue  # already replaced together with the outer equation
        replaced.update(id(inner) for inner in math.find_all("math"))
        latex = _math_source(math)
        if latex is None:
            latex = " ".join(_mathml_latex(math, 0).split())

        math.clear()
        math.attrs = {}
        math.name = "span"
        if latex:
            math.append(f"${latex}$")  # surrounding spacing stays as in the source (so no space appears before a comma)


# The classes MathJax 2 gives the element that shows a formula, or the display block around it, right before its
# `<script type="math/tex">`, as MathJax spells them (a page's own `mathjax` container is not one): the frames and display blocks
# of its HTML-CSS, SVG, CommonHTML, PreviewHTML and NativeMML outputs and the preview shown while it typesets. For a frame without
# the id MathJax gives it (`_mathjax_frame`).
_MATHJAX_FRAMES = frozenset({"MathJax", "MathJax_Display", "MathJax_SVG", "MathJax_SVG_Display", "MathJax_CHTML",
                             "MathJax_CHTML_Display", "mjx-chtml", "MJXc-display", "MathJax_PHTML", "MathJax_PHTML_Display",
                             "MJXp-display", "MathJax_MathML", "MathJax_Preview"})


def _put_tex_scripts(soup: BeautifulSoup) -> None:
    """Replaces the TeX of `<script type="math/tex">` (also `; mode=display`) with the same `$LaTeX$` notation, before scripts
    are removed. It is the input form of MathJax 2 and what kramdown (Jekyll, GitHub Pages) writes for `$…$` and `$$…$$`: in a
    page MathJax has not rendered, the formula is nowhere else, and it vanished with the scripts, with no warning
    (Pinned by: test_core_html.py::TestMathMl::test_tex_in_a_mathjax_script_is_the_formula). A formula that opens a fragment
    is taken out of the head the parser put it in (`_formulas_out_of_the_head`).

    A page saved after MathJax 2 ran has the rendered formula right before the script (`_mathjax_frame`). When it holds MathML
    (the assistive copy), it is replaced by that MathML, from which the formula is read; when it holds only the drawn glyphs,
    it is replaced by the TeX, which reads better than the glyphs one by one. Either way the script is dropped and the formula
    is read once: the glyphs beside the MathML were read as well unless the page marked them hidden (Pinned by:
    test_core_html.py::TestMathMl::test_mathjax_frame_is_found_by_its_id).

    The scripts of frames are removed together at the end (`_decompose_all`): removed one by one, each looked up its place among
    its siblings, and a paragraph of 20,000 formulas took 14 seconds where 10,000 took 3 (Pinned by:
    test_core_html.py::TestMathMl::test_many_mathjax_formulas_take_linear_time).
    """
    scripts = [script for script in soup.find_all("script", type=True)
               if str(script["type"]).strip().lower().startswith("math/tex")]
    _formulas_out_of_the_head(soup, scripts)
    framed: list[Tag] = []
    for script in scripts:
        tex = " ".join(_tex_without_comments(script.get_text()).split())
        frame = _mathjax_frame(script)
        math = None
        if frame is not None:
            framed.append(script)
            math = frame.find("math")
            if math is not None:
                math.extract()  # out of the frame, which then holds it alone
        target = script if frame is None else frame
        target.clear()
        target.attrs = {}
        # A display block MathJax writes as a `<div>` stays a block: as a span its formula ran into the text after it (Pinned
        # by: test_core_html.py::TestMathMl::test_mathjax_display_block_stays_a_block)
        target.name = "div" if target.name == "div" else "span"
        if math is not None:
            target.append(math)
        elif tex:
            target.append(f"${tex}$")
    _decompose_all(framed)


# What stands only in the head of a page (`_formulas_out_of_the_head`).
_HEAD_ONLY = frozenset({"title", "meta", "link", "base", "style"})


def _formulas_out_of_the_head(soup: BeautifulSoup, scripts: list[Tag]) -> None:
    """Moves the formula scripts among `scripts` that the parser put in the head to the start of the body. A fragment that opens
    with a `<script type="math/tex">` has it put in the head, as everything before the first text or block, and the head is
    removed: the formula was lost, where with a `<body>` written it was read (Pinned by:
    test_core_html.py::TestMathMl::test_a_formula_that_opens_a_fragment_is_kept). Only the scripts that come after nothing
    that stands only in a head (`_HEAD_ONLY`): after a title or a meta element the head is the page's own."""
    head = soup.head
    if head is None or not scripts:
        return
    formulas = {id(script) for script in scripts}
    moved: list[PageElement] = []
    for child in head.contents:
        if isinstance(child, Tag) and child.name in _HEAD_ONLY:
            break
        # with the white space after a formula, which keeps it apart from the text after it (`$E$ is`, not `$E$is`)
        if id(child) in formulas or (moved and type(child) is NavigableString and not child.strip()):
            moved.append(child)
    if not moved:
        return
    body = soup.body
    if body is None:
        body = soup.new_tag("body")
        head.insert_after(body)
    _decompose_all(moved, detach_only=True)
    for node in reversed(moved):
        body.insert(0, node)


def _mathjax_frame(script: Tag) -> Tag | None:
    """The element MathJax 2 shows the formula of `script` in, or the display block around it, right before the script (white
    space and comments between); None when there is none. MathJax gives the frame the id of the script followed by `-Frame` and
    puts a displayed formula in a block of its own, so that element is the frame when it has that id or holds only the element
    that has it; otherwise when its class is one MathJax gives its frames (`_MATHJAX_FRAMES`). The display block of the
    CommonHTML output (`mjx-chtml MJXc-display`) was not among those classes, and a displayed formula came out twice, as its
    MathML and as its TeX (Pinned by: test_core_html.py::TestMathMl::test_mathjax_frame_is_found_by_its_id). Only that element:
    any earlier sibling whose class held the letters 'mathjax' was taken for the frame, and a container of paragraphs was
    replaced by the formula (Pinned by: test_core_html.py::TestMathMl::test_only_the_mathjax_frame_right_before_the_script_is_replaced)."""
    before = script.previous_sibling
    while isinstance(before, NavigableString) and (isinstance(before, Comment) or not before.strip()):
        before = before.previous_sibling
    if not isinstance(before, Tag):
        return None
    name = str(script.get("id") or "").strip()
    if name:
        frame = f"{name}-Frame"
        if before.get("id") == frame:
            return before
        inside = [child for child in before.contents if isinstance(child, Tag) or (not isinstance(child, Comment) and child.strip())]
        if len(inside) == 1 and isinstance(inside[0], Tag) and inside[0].get("id") == frame:
            return before
    return before if any(str(kind) in _MATHJAX_FRAMES for kind in before.get("class") or ()) else None


def _tex_without_comments(tex: str) -> str:
    """`tex` without its comments: from a `%` that is not escaped (`\\%` is a percent sign) to the end of its line. kramdown wraps
    a displayed formula in `% <![CDATA[` and `%]]>`; once the lines of the TeX were joined, the `%` of the wrapper made all of
    the formula a comment, and a comment inside a formula took the lines after it along (`x % note` and `+ y` as `x % note + y`)
    (Pinned by: test_core_html.py::TestMathMl::test_comments_in_the_tex_of_a_script_are_left_out)."""
    # An escaped character (`\%` among them, and the `\\` of a line break) is matched first and kept
    return _TEX_COMMENT.sub(lambda match: match.group(1) or "", tex) if "%" in tex else tex


_TEX_COMMENT = re.compile(r"(\\.)|%[^\n]*", re.DOTALL)


def _maths_holding_page_blocks(soup: BeautifulSoup) -> list[Tag]:
    """`<math>` elements holding page blocks such as paragraphs, tables and lists (except inside annotation-xml). That is the
    body after an unclosed `<math>` having landed inside it, not an equation — it is unwrapped and read as ordinary text.
    Turning it into an equation collected only the text of MathML elements, so `<math>abc<p>after</p>` became `$after$` (abc
    lost, the whole body after it a one-line equation) (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml). Each block
    walks upwards and stops at an element already visited, so it is linear time."""
    holding: list[Tag] = []
    seen: set[int] = set()

    for block in soup.find_all(_PAGE_BLOCKS_IN_MATH):
        node = block.parent
        while isinstance(node, Tag) and id(node) not in seen and node.name != "annotation-xml":
            seen.add(id(node))
            if node.name == "math":
                holding.append(node)
            node = node.parent
    return holding


def _math_source(math: Tag) -> str | None:
    """The LaTeX of the equation as the document wrote it. None if there is none."""
    for annotation in math.find_all("annotation"):
        if str(annotation.get("encoding", "")).strip().lower() in _TEX_ENCODINGS:
            text = " ".join(_text(annotation).split())
            if text:
                return _unwrapped(text)

    text = " ".join(str(math.get("alttext", "")).split())
    return _unwrapped(text) if text else None


def _unwrapped(latex: str) -> str:
    """`latex` without the `{\\displaystyle …}` group Wikipedia wraps every formula in, in its alttext and in its TeX
    annotation alike (the annotation came out as `${\\displaystyle E=mc^{2}}$`). Only a group that holds the whole formula is
    taken off: in `{\\displaystyle a}+{\\displaystyle b}` the first group closes before the end, and the text stays as it is
    (Pinned by: test_core_html.py::TestMathMl::test_display_style_wrapper_of_wikipedia_is_taken_off)."""
    opening = _DISPLAY_STYLE.match(latex)
    if opening is None:
        return latex
    depth = 0
    index = 0
    while index < len(latex):
        character = latex[index]
        if character == "\\":
            index += 2  # an escaped character (`\\{`, `\\}`) opens and closes nothing
            continue
        depth += {"{": 1, "}": -1}.get(character, 0)
        index += 1
        if depth == 0:
            break
    # The group must close on the last character: before it, the group holds only part of the formula
    return latex[opening.end():-1].strip() if depth == 0 and index == len(latex) else latex


def _mathml_latex(node: object, depth: int) -> str:
    """Presentation MathML as one LaTeX-style line. The aim is not complete LaTeX but making the structure (scripts,
    fractions, radicals, upper/lower limits) readable (the same as `_omml.omml_to_latex`). Unknown elements have their
    children joined in order. Deeper than `_MAX_MATH_DEPTH`, only the characters are joined."""
    if isinstance(node, NavigableString):
        return "" if isinstance(node, PreformattedString) else literal(str(node).strip())
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name in MATH_IGNORED:
        return ""
    if depth >= _MAX_MATH_DEPTH:
        return literal("".join(_text(node).split()))

    children = [child for child in node.children if isinstance(child, Tag)]
    parts = [_mathml_latex(child, depth + 1) for child in children]

    if name == "semantics":
        return parts[0] if parts else ""  # the first child is presentation, the rest are annotations
    if name == "mspace":
        return " "
    fence = (node.get("open", "("), str(node.get("separators", ",")).strip()[:1], node.get("close", ")")) if name == "mfenced" else None
    elements = [base_child(child, _tag_name, _child_tags) for child in children]
    structure = mathml_structure(name, parts, fence, elements)  # scripts, fractions, radicals, fences, matrices (`_mathml`)
    if structure is not None:
        return structure
    if not children:
        if name == "mtext":
            return text_leaf(_text(node))
        return literal(" ".join(_text(node).split()))  # mi·mn·mo·ms
    # Unknown elements have their child elements and the text between them joined in order. Joining only the elements lost the
    # `-` of `<math>- <span>…</span></math>`
    # (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml::test_text_beside_elements_inside_math_is_kept).
    tag_parts = iter(parts)
    return "".join(next(tag_parts) if isinstance(child, Tag) else _mathml_latex(child, depth + 1) for child in node.children)


def _tag_name(tag: Tag) -> str:
    return tag.name


def _child_tags(tag: Tag) -> list[Tag]:
    return [child for child in tag.children if isinstance(child, Tag)]
