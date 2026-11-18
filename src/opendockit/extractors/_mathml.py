"""Presentation MathML → LaTeX: the rules HTML and ODF share.

HTML reads its equations from a parsed HTML tree and ODF from an XML tree, so each walks its own tree and decides what the
text of a leaf is. What an element becomes once its child elements are rendered is the same for both and is decided here,
so an equation reads the same from HTML and from ODF.
"""

from opendockit.extractors._omml import _literal


# Elements with a base and one or two scripts → the mark of the first script.
MATH_SCRIPTS = {"msup": "^", "msub": "_", "msubsup": "_", "munder": "_", "mover": "^", "munderover": "_"}
# Elements that are not part of the text of an equation.
MATH_IGNORED = frozenset(("annotation", "annotation-xml", "none", "mprescripts", "mphantom"))


def literal(text: str) -> str:
    """The text of a leaf (mi, mn, mo, mtext, ms, the text between elements, the fences of an mfenced) with the characters
    LaTeX or the Markdown `$…$` would read as syntax escaped, as in the text of an Office equation (`_omml._literal`): a `$`
    in `<mtext>$</mtext><mn>5</mn>` gave `$$5×n$`, which opened display math and swallowed the text after it, and a brace
    typed in an `<mo>` vanished as grouping (Pinned by: test_core_html.py::TestMathMl::test_literal_text_is_escaped,
    test_odf.py::TestObjects::test_literal_text_of_a_formula_is_escaped)."""
    return _literal(text)


def text_leaf(text: str) -> str:
    """The LaTeX of an `<mtext>` with the text `text`. Words are `\\text{…}` with the spaces at its ends kept, as a Hangul
    equation writes its text (`_equation`): the leaf was stripped like a variable, so `1<mtext> if </mtext>x` read `1ifx`
    (Pinned by: test_core_html.py::TestMathMl::test_words_of_an_mtext_keep_their_spaces). Text without a letter stays
    literal text, its end spaces kept as spaces, and text of spaces only is one space, as an `<mspace>` is."""
    words = " ".join(text.split())
    if not words:
        return " " if text else ""
    lead, trail = " " if text[:1].isspace() else "", " " if text[-1:].isspace() else ""
    if not any(char.isalpha() for char in words):
        return f"{lead}{literal(words)}{trail}"
    return f"\\text{{{lead}{literal(words)}{trail}}}"


def braced(text: str) -> str:
    """`text` as the argument of a script: one character stands as it is, more are wrapped in braces."""
    return text if len(text) == 1 else f"{{{text}}}"


# Scripts whose base is itself a script on the same side (`_script_base`), as `_omml._CLASHING_SCRIPTS` for Office.
_CLASHING_SCRIPTS = {"msup": ("msup", "msubsup"), "msub": ("msub", "msubsup"), "msubsup": ("msup", "msub", "msubsup")}
# A child element: its name and the names of its own child elements.
Child = tuple[str, list[str]]


def _script_base(name: str, base: str, child: Child | None) -> str:
    """The base of the script element `name` (`base` is its LaTeX, `child` the base element): braced when it is itself a script
    on the same side, or a group (`mrow`) of more than one element, so that the script applies to all of it. `(x²)³` came out as
    `x^2^3` (a LaTeX error, confusable with `x^{2^3}`), `f′²` as `f^′^2` and `(a+b)²` as `a+b^2`; Office equations already brace
    the first (Pinned by: test_core_html.py::TestMathMl::test_script_on_a_scripted_or_grouped_base_keeps_its_group). A
    superscript on a subscript (`a_i^2`) is left as it is. A group of one element is looked through."""
    if child is None:
        return base
    element, inner = child
    if element == "mrow" and len(inner) == 1:
        element, inner = inner[0], []
        if element == "mrow":  # a group inside a group (MathJax wraps its groups): what it holds is not known here
            return f"{{{base}}}"
    if element in _CLASHING_SCRIPTS.get(name, ()) or (element == "mrow" and len(inner) > 1):
        return f"{{{base}}}"
    return base


def _multiscripts(parts: list[str], children: list[Child]) -> str:
    """An `mmultiscripts`: the base, then pairs of (subscript, superscript) after it, and after an `mprescripts` the pairs
    before it; an empty script (`none`) is left out. Joined as other elements, an isotope `¹⁴₆C` came out as `C614`."""
    names = [element for element, _ in children]
    split = names.index("mprescripts") if "mprescripts" in names else len(parts)

    def scripts(pairs: list[str]) -> str:
        return "".join((f"_{{{lower}}}" if lower else "") + (f"^{{{upper}}}" if upper else "")
                       for lower, upper in zip(pairs[::2], pairs[1::2], strict=False))

    before = scripts(parts[split + 1:])
    return (f"{{}}{before}" if before else "") + parts[0] + scripts(parts[1:split])


def mathml_structure(name: str, parts: list[str], fence: tuple[str, str, str] | None = None,
                     children: list[Child] | None = None) -> str | None:
    """The LaTeX of the element `name` from the LaTeX of its child elements (`parts`): scripts, fractions, radicals, fences
    and matrices. None for an element without a rule here (the caller joins its children). `fence` is the (open, separator,
    close) of an `mfenced`; `children` are the child elements the parts come from (name and the names of their children)."""
    if name == "mmultiscripts" and parts and children is not None and len(children) == len(parts):
        return _multiscripts(parts, children)
    if name in MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = _script_base(name, parts[0], children[0] if children else None), parts[1:]
        if name in ("msup", "msub"):
            return f"{base}{MATH_SCRIPTS[name]}{braced(scripts[0])}"
        if name == "msubsup" and len(scripts) >= 2:
            return f"{base}_{braced(scripts[0])}^{braced(scripts[1])}"
        # Upper/lower limits (∑, lim, underbars etc.) are always wrapped in braces — so they do not read as glued to the
        # following character, as in `∑_{i=1}^ni`.
        if name == "munderover" and len(scripts) >= 2:
            return f"{base}_{{{scripts[0]}}}^{{{scripts[1]}}}"
        return f"{base}{MATH_SCRIPTS[name]}{{{scripts[0]}}}"
    if name == "mfrac" and len(parts) >= 2:
        return f"\\frac{{{parts[0]}}}{{{parts[1]}}}"
    if name == "msqrt":
        return f"\\sqrt{{{''.join(parts)}}}"
    if name == "mroot" and len(parts) >= 2:
        return f"\\sqrt[{parts[1]}]{{{parts[0]}}}"
    if name == "mfenced":
        opening, separator, closing = (literal(mark) for mark in fence or ("(", ",", ")"))
        return f"{opening}{separator.join(parts)}{closing}"
    if name == "mtable":
        rows = " \\\\ ".join(parts)
        return f"\\begin{{matrix}}{rows}\\end{{matrix}}"
    if name in ("mtr", "mlabeledtr"):
        return " & ".join(parts)
    return None
