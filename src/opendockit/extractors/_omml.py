"""Office equations (OMML) → LaTeX. DOCX and PPTX use the same XML and come out in the same notation (` $LaTeX$ `)."""

import re

from lxml import etree


_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# The 'off' values of an OOXML on/off value (ST_OnOff)
_OFF_VALUES = frozenset(("0", "off", "false"))


# ---- Office equations (OMML) -------------------------------------------------------------------------------------

# Equation accent character (m:chr of m:acc) → LaTeX command. A character not listed here is appended after the letter as is.
_ACCENTS = {"\u0302": "hat", "\u0303": "tilde", "\u0304": "bar", "\u0305": "bar", "\u0307": "dot", "\u0308": "ddot",
            "\u20d7": "vec"}


def render_omml(equation: etree._Element) -> str:
    """One m:oMath → ` $LaTeX$ ` to insert in the paragraph text. DOCX and PPTX use the same notation."""
    return f" ${omml_to_latex(equation).strip()}$ "


def _m(tag: str) -> str:
    return f"{{{_M}}}{tag}"


def _group(text: str) -> str:
    return text if len(text) == 1 else f"{{{text}}}"


# Text ending in a one-character script (`x^k`, `a_n`). When a letter follows directly, the script is braced (`_join_math`).
_BARE_SCRIPT_END = re.compile(r"[_^]([^{}\\\s])$")
# Grouping character (m:chr of m:groupChr) → LaTeX command. A character not listed here goes above/below with `\overset` / `\underset`.
_GROUP_BRACES = {"\u23df": "underbrace", "\u23de": "overbrace"}
# The m:d delimiters that LaTeX reads as grouping characters.
_ESCAPED_DELIMITERS = {"{": "\\{", "}": "\\}"}
# Equation font style (m:rPr/m:scr) → LaTeX command. Only double-struck (ℝ), script (ℒ) and fraktur, whose meaning differs, are
# written; sans-serif, monospace etc. are left as is.
_MATH_ALPHABETS = {"double-struck": "mathbb", "script": "mathcal", "fraktur": "mathfrak"}
_MATH_ALPHABET_LETTERS = re.compile(r"[A-Za-z0-9]+")
# The removed side of tracked changes (deleted text, the original place of moved text). It wraps m:r inside equations too. Left out
# as in body paragraphs (`docx._paragraph_text`).
_TRACKED_REMOVALS = (f"{{{_W}}}del", f"{{{_W}}}moveFrom")


def _math_content(element: etree._Element) -> list[etree._Element]:
    """Content children of an equation element (properties m:*Pr / w:rPr and comments / processing instructions are left out)."""
    return [child for child in element if isinstance(child.tag, str) and not child.tag.endswith("Pr")]


def _join_math(element: etree._Element) -> str:
    """Join the child equations in order. When a letter or digit directly follows a one-character script, the script is wrapped in
    `{}` (`x^{k}a^{n-k}`), and a function (m:func) name is spaced from a preceding letter, digit or closing bracket (`a_n cos x`;
    after an operator it stays `α+cos β`). Joined as is, the script and the following letter read as one lump, like `x^ka^{n-k}` and
    `a_ncos x` (Pinned by: test_office_equations.py::TestOmmlScriptsFollowedByText).
    The script is looked for in the last three characters only (the pattern ends at the end of the text, or before a line break
    there): searched from the start for every child, an equation of 80,000 runs took 10 seconds
    (Pinned by: test_audit_core.py::TestEquationWithManyRuns)."""
    text = ""
    for child in element:
        piece = omml_to_latex(child)
        if piece and text and child.tag == _m("func") and (text[-1].isalnum() or text[-1] in ")]}"):
            text += " "
        elif piece[:1].isalnum() and (bare := _BARE_SCRIPT_END.search(text, max(len(text) - 3, 0))):
            text = f"{text[:bare.start(1)]}{{{bare.group(1)}}}"
        text += piece
    return text


# A base that already carries a script on the same side. Script element → the script elements that clash when put on top of it
_CLASHING_SCRIPTS = {"sSup": ("sSup", "sSubSup"), "sSub": ("sSub", "sSubSup"), "sSubSup": ("sSup", "sSub", "sSubSup")}


def _script_base(element: etree._Element, kind: str) -> str:
    """Base of a script (m:e). If the last element of the base is an expression carrying a script on the same side, it is braced —
    `(x²)³` became `x^2^3` (a LaTeX error, confusable with `x^{2^3}`) (Pinned by:
    test_office_equations.py::TestOmmlScriptOnAScriptedBase). A superscript on top of a subscript (`a_i^2`) is left as is."""
    base = element.find(_m("e"))
    if base is None:
        return ""
    text = omml_to_latex(base)
    last = next((child for child in reversed(base) if isinstance(child.tag, str) and not child.tag.endswith("Pr")), None)
    clashes = last is not None and any(last.tag == _m(name) for name in _CLASHING_SCRIPTS[kind])
    return f"{{{text}}}" if clashes else text


def omml_to_latex(element: etree._Element) -> str:
    """Carry an Office equation (OMML; Word and PowerPoint use the same XML) over as a one-line LaTeX-style string.

    The aim is not a complete LaTeX conversion but to let an LLM read the structure (fractions, scripts, radicals, delimiters, sums).
    For unknown elements the child text is joined in order.
    """
    def part(name: str) -> str:
        child = element.find(_m(name))
        return omml_to_latex(child) if child is not None else ""

    def prop(container: str, name: str, default: str) -> str:
        node = element.find(f"{_m(container)}/{_m(name)}")
        return node.get(_m("val"), default) if node is not None else default

    tag = element.tag
    if tag == _m("t"):
        return element.text or ""
    if not isinstance(tag, str) or tag.endswith("Pr") or tag in _TRACKED_REMOVALS:
        # Reading the removed side of tracked changes made an equation edited from 0.3 to 0.5 into `E=A×0.30.5` (Pinned by:
        # test_office_equations.py::TestOmmlTrackedChanges). The inserted side (w:ins, w:moveTo) is read as is by `_join_math` below.
        return ""
    if tag == _m("r"):
        # Dropping double-struck / script letters (m:scr) turned the set of reals ℝ into an ordinary variable R. Only letters and
        # digits are wrapped in the command (Pinned by: test_office_equations.py::TestOmmlMathAlphabets).
        text = _join_math(element)
        command = _MATH_ALPHABETS.get(prop("rPr", "scr", "roman"))
        return _MATH_ALPHABET_LETTERS.sub(lambda match: f"\\{command}{{{match.group()}}}", text) if command else text
    if tag == _m("f"):
        # A fraction without a bar (noBar) is stacked text such as a binomial coefficient (inside parentheses m:d turns it into
        # `\binom`). Linear and skewed fractions (lin, skw) have the `a/b` shape. Emitting them all as `\frac` made a binomial
        # coefficient read as a division (Pinned by: test_office_equations.py::TestOmmlFractionsWithoutABar).
        kind = prop("fPr", "type", "bar")
        if kind == "noBar":
            return f"{{{part('num')} \\atop {part('den')}}}"
        if kind in ("lin", "skw"):
            return f"{_group(part('num'))}/{_group(part('den'))}"
        return f"\\frac{{{part('num')}}}{{{part('den')}}}"
    if tag == _m("sSup"):
        return f"{_script_base(element, 'sSup')}^{_group(part('sup'))}"
    if tag == _m("sSub"):
        return f"{_script_base(element, 'sSub')}_{_group(part('sub'))}"
    if tag == _m("sSubSup"):
        return f"{_script_base(element, 'sSubSup')}_{_group(part('sub'))}^{_group(part('sup'))}"
    if tag == _m("sPre"):
        return f"{{}}_{_group(part('sub'))}^{_group(part('sup'))}{part('e')}"
    if tag == _m("rad"):
        degree = part("deg")
        return f"\\sqrt[{degree}]{{{part('e')}}}" if degree else f"\\sqrt{{{part('e')}}}"
    if tag == _m("d"):
        return _delimited(element, prop("dPr", "begChr", "("), prop("dPr", "endChr", ")"), prop("dPr", "sepChr", "|"))
    if tag == _m("nary"):
        operator = prop("naryPr", "chr", "∫")
        sub, sup = part("sub"), part("sup")
        limits = (f"_{_group(sub)}" if sub else "") + (f"^{_group(sup)}" if sup else "")
        return f"{operator}{limits} {part('e')}"
    if tag == _m("groupChr"):
        # The grouping character (default: bottom brace ⏟) is a property, so it was dropped and what the note under the group
        # referred to was lost (Pinned by: test_office_equations.py::TestOmmlGroupCharactersAndBraces::test_group_character_is_kept).
        symbol, position = prop("groupChrPr", "chr", "\u23df"), prop("groupChrPr", "pos", "bot")
        if symbol in _GROUP_BRACES:
            return f"\\{_GROUP_BRACES[symbol]}{{{part('e')}}}"
        return f"\\{'overset' if position == 'top' else 'underset'}{{{symbol}}}{{{part('e')}}}"
    if tag == _m("func"):
        return f"{part('fName')} {part('e')}"
    if tag == _m("limLow"):
        return f"{part('e')}_{_group(part('lim'))}"
    if tag == _m("limUpp"):
        return f"{part('e')}^{_group(part('lim'))}"
    if tag == _m("acc"):
        # The accent (the overbar of x̄ etc.) was dropped, leaving only x. The default character is ^
        # (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_equation_array_rows_and_accents_are_kept).
        accent = prop("accPr", "chr", "\u0302")
        return f"\\{_ACCENTS[accent]}{{{part('e')}}}" if accent in _ACCENTS else f"{part('e')}{accent}"
    if tag == _m("bar"):
        return f"\\{'overline' if prop('barPr', 'pos', 'bot') == 'top' else 'underline'}{{{part('e')}}}"
    if tag == _m("phant"):
        # A phantom only takes up space. The text of a hidden one (m:show 0) was joined too, giving `hiddenx` (Pinned by:
        # test_office_equations.py::TestOmmlPhantom). Removing the width/height (zeroWid etc.) of a visible phantom (the default)
        # does not affect the text.
        return "" if prop("phantPr", "show", "on") in _OFF_VALUES else part("e")
    if tag == _m("eqArr"):
        # A multi-line equation. Joining the rows with no separator gave `a+b=1a-b=3`. The same row separator as a matrix is used.
        return " \\\\ ".join(omml_to_latex(child) for child in element.iterchildren(_m("e")))
    if tag == _m("m"):
        rows = [" & ".join(omml_to_latex(cell) for cell in row.iterchildren(_m("e")))
                for row in element.iterchildren(_m("mr"))]
        return "\\begin{matrix}" + " \\\\ ".join(rows) + "\\end{matrix}"
    return _join_math(element)


def _delimited(element: etree._Element, begin: str, end: str, separator: str) -> str:
    """Delimiters (m:d). A single bar-less fraction inside parentheses is a binomial coefficient (`\\binom`, the parentheses are
    absorbed); a single equation array inside a delimiter that has only an opening `{` is a system of equations (`cases`). Brace
    delimiters are written `\\{` / `\\}` — left as is they are LaTeX grouping and the braces of `{a,b}` vanish
    (Pinned by: test_office_equations.py::TestOmmlFractionsWithoutABar, TestOmmlGroupCharactersAndBraces)."""
    parts = list(element.iterchildren(_m("e")))
    only = _math_content(parts[0]) if len(parts) == 1 else []
    if len(only) == 1 and only[0].tag == _m("f") and (begin, end) == ("(", ")"):
        kind = only[0].find(f"{_m('fPr')}/{_m('type')}")
        if kind is not None and kind.get(_m("val")) == "noBar":
            numerator, denominator = (only[0].find(_m(name)) for name in ("num", "den"))
            return (f"\\binom{{{omml_to_latex(numerator) if numerator is not None else ''}}}"
                    f"{{{omml_to_latex(denominator) if denominator is not None else ''}}}")
    if len(only) == 1 and only[0].tag == _m("eqArr") and begin == "{" and not end:
        return f"\\begin{{cases}}{omml_to_latex(only[0])}\\end{{cases}}"
    opening, closing = _ESCAPED_DELIMITERS.get(begin, begin), _ESCAPED_DELIMITERS.get(end, end)
    return opening + separator.join(omml_to_latex(part) for part in parts) + closing
