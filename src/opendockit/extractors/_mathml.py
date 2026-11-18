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


def mathml_structure(name: str, parts: list[str], fence: tuple[str, str, str] | None = None) -> str | None:
    """The LaTeX of the element `name` from the LaTeX of its child elements (`parts`): scripts, fractions, radicals, fences
    and matrices. None for an element without a rule here (the caller joins its children). `fence` is the (open, separator,
    close) of an `mfenced`."""
    if name in MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = parts[0], parts[1:]
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
