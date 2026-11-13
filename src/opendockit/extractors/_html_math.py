"""MathML and equations kept as pictures or scripts → LaTeX."""

import re

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import PreformattedString

from opendockit.extractors._html_base import _BLOCK_TAGS, _HEADINGS


# MathML (`_put_math`): annotation encodings of the LaTeX the document wrote, the wrapper of Wikipedia's alttext, script
# elements → marks, elements that are not text, and the maximum depth unfolded LaTeX-style (recursion).
_TEX_ENCODINGS = {"application/x-tex", "tex", "latex", "application/x-latex", "text/x-tex"}
_DISPLAY_STYLE = re.compile(r"\{\\displaystyle\s*(.*)\}", re.DOTALL)
_MATH_SCRIPTS = {"msup": "^", "msub": "_", "msubsup": "_", "munder": "_", "mover": "^", "munderover": "_"}
_MATH_IGNORED = {"annotation", "annotation-xml", "none", "mprescripts", "mphantom"}
_MAX_MATH_DEPTH = 50
# Page blocks that cannot appear inside an equation (`_maths_holding_page_blocks`).
_PAGE_BLOCKS_IN_MATH = [*_BLOCK_TAGS, *_HEADINGS, "table", "ul", "ol", "li", "pre"]


def _put_math(soup: BeautifulSoup) -> None:
    """Replaces MathML (`<math>`) with the same `$LaTeX$` notation as Office and Hangul (HWP) equations
    (`_omml.render_omml`). Joining only the characters read `r²π` as `r2π` and the fraction 3/4 as `34`
    (Pinned by: test_core_html.py::TestMathMl).

    The LaTeX the document wrote is used first: `<annotation encoding="application/x-tex">` (LaTeXML, MathJax) or, without it,
    `alttext` (Wikipedia's `{\\displaystyle …}` is stripped). The whole element is replaced so that presentation MathML and the
    annotation text do not run together and give the same expression twice. With neither, the presentation MathML is unfolded
    LaTeX-style (`_mathml_latex`). The replacement method is the same as `_put_form_values` (in-place span, linear time).
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
            text = " ".join(annotation.get_text().split())
            if text:
                return text

    text = " ".join(str(math.get("alttext", "")).split())
    if not text:
        return None
    wrapped = _DISPLAY_STYLE.fullmatch(text)
    return wrapped.group(1).strip() if wrapped else text


def _mathml_latex(node: object, depth: int) -> str:
    """Presentation MathML as one LaTeX-style line. The aim is not complete LaTeX but making the structure (scripts,
    fractions, radicals, upper/lower limits) readable (the same as `_omml.omml_to_latex`). Unknown elements have their
    children joined in order. Deeper than `_MAX_MATH_DEPTH`, only the characters are joined."""
    if isinstance(node, NavigableString):
        return "" if isinstance(node, PreformattedString) else str(node).strip()
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name in _MATH_IGNORED:
        return ""
    if depth >= _MAX_MATH_DEPTH:
        return "".join(node.get_text().split())

    children = [child for child in node.children if isinstance(child, Tag)]
    parts = [_mathml_latex(child, depth + 1) for child in children]

    if name == "semantics":
        return parts[0] if parts else ""  # the first child is presentation, the rest are annotations
    if name == "mspace":
        return " "
    if name in _MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = parts[0], parts[1:]
        if name in ("msup", "msub"):
            return f"{base}{_MATH_SCRIPTS[name]}{_math_group(scripts[0])}"
        if name == "msubsup" and len(scripts) >= 2:
            return f"{base}_{_math_group(scripts[0])}^{_math_group(scripts[1])}"
        # Upper/lower limits (∑, lim, underbars etc.) are always wrapped in braces — so they do not read as glued to the
        # following character, as in `∑_{i=1}^ni`.
        if name == "munderover" and len(scripts) >= 2:
            return f"{base}_{{{scripts[0]}}}^{{{scripts[1]}}}"
        return f"{base}{_MATH_SCRIPTS[name]}{{{scripts[0]}}}"
    if name == "mfrac" and len(parts) >= 2:
        return f"\\frac{{{parts[0]}}}{{{parts[1]}}}"
    if name == "msqrt":
        return f"\\sqrt{{{''.join(parts)}}}"
    if name == "mroot" and len(parts) >= 2:
        return f"\\sqrt[{parts[1]}]{{{parts[0]}}}"
    if name == "mfenced":
        separator = str(node.get("separators", ",")).strip()[:1]
        return f"{node.get('open', '(')}{separator.join(parts)}{node.get('close', ')')}"
    if name == "mtable":
        rows = " \\\\ ".join(parts)
        return f"\\begin{{matrix}}{rows}\\end{{matrix}}"
    if name in ("mtr", "mlabeledtr"):
        return " & ".join(parts)
    if not children:
        return " ".join(node.get_text().split())  # mi·mn·mo·mtext·ms
    # Unknown elements have their child elements and the text between them joined in order. Joining only the elements lost the
    # `-` of `<math>- <span>…</span></math>`
    # (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml::test_text_beside_elements_inside_math_is_kept).
    tag_parts = iter(parts)
    return "".join(next(tag_parts) if isinstance(child, Tag) else _mathml_latex(child, depth + 1) for child in node.children)


def _math_group(text: str) -> str:
    return text if len(text) == 1 else f"{{{text}}}"
