"""Presentation MathML → LaTeX: the rules HTML and ODF share.

HTML reads its equations from a parsed HTML tree and ODF from an XML tree, so each walks its own tree and decides what the
text of a leaf is. What an element becomes once its child elements are rendered is the same for both and is decided here,
so an equation reads the same from HTML and from ODF.
"""

# Elements with a base and one or two scripts → the mark of the first script.
MATH_SCRIPTS = {"msup": "^", "msub": "_", "msubsup": "_", "munder": "_", "mover": "^", "munderover": "_"}
# Elements that are not part of the text of an equation.
MATH_IGNORED = frozenset(("annotation", "annotation-xml", "none", "mprescripts", "mphantom"))


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
        opening, separator, closing = fence or ("(", ",", ")")
        return f"{opening}{separator.join(parts)}{closing}"
    if name == "mtable":
        rows = " \\\\ ".join(parts)
        return f"\\begin{{matrix}}{rows}\\end{{matrix}}"
    if name in ("mtr", "mlabeledtr"):
        return " & ".join(parts)
    return None
