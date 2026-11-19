"""Presentation MathML → LaTeX: the rules HTML and ODF share.

HTML reads its equations from a parsed HTML tree and ODF from an XML tree, so each walks its own tree and decides what the
text of a leaf is. What an element becomes once its child elements are rendered is the same for both and is decided here,
so an equation reads the same from HTML and from ODF.
"""

from collections.abc import Callable

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


# The marks of the scripts each script element writes on its base (`_script_base`).
_SIDES = {"msup": "^", "mover": "^", "msub": "_", "munder": "_", "msubsup": "_^", "munderover": "_^"}
# Elements that only group, space or style what they hold (`base_child`): one that holds one element is that element, and one
# that holds several is a group.
_GROUPS = frozenset(("mrow", "mstyle", "mpadded", "mphantom", "menclose", "merror"))
# The wrappers `base_child` looks through, at most. Past them the base is taken for a group and braced.
_MAX_WRAPPERS = 8
# A child element: its name and the names of its own child elements (`base_child`).
Child = tuple[str, list[str]]


def base_child[Node](node: Node, name_of: Callable[[Node], str], elements_of: Callable[[Node], list[Node]]) -> Child:
    """The child element `node` as `mathml_structure` takes it: its name and the names of its child elements, looking through
    an element that only groups or styles one element (`_GROUPS`) and the annotations of a `semantics`, which stand for what
    they hold. A group in an `mstyle`, `mpadded` or `semantics` stood as the base of a script unbraced (`a+b^2`), and a group
    of one group could not be told from a group of several (Pinned by: test_core_html.py::TestMathMl::
    test_script_on_a_scripted_or_grouped_base_keeps_its_group). `name_of` and `elements_of` read the tree of the caller."""
    name, inner = name_of(node), elements_of(node)
    for _ in range(_MAX_WRAPPERS):
        if not inner or (name != "semantics" and (name not in _GROUPS or len(inner) != 1)):
            break
        node = inner[0]
        name, inner = name_of(node), elements_of(node)
    else:
        if name in _GROUPS or name == "semantics":
            return "mrow", ["", ""]  # still a wrapper: what it holds is not known here, and it is braced as a group
    return name, [name_of(element) for element in inner]


def _script_base(sides: str, base: str, child: Child | None) -> str:
    """The base of a script element that writes the scripts `sides` (`^`, `_` or both) on it (`base` is its LaTeX, `child`
    the base element, `base_child`): braced when it ends in a script on one of those sides, or is a group of more than one
    element, so that the script applies to all of it. `(x²)³` came out as `x^2^3` (a LaTeX error, confusable with `x^{2^3}`),
    `f′²` as `f^′^2` and `(a+b)²` as `a+b^2`; Office equations already brace the first (Pinned by:
    test_core_html.py::TestMathMl::test_script_on_a_scripted_or_grouped_base_keeps_its_group). Which scripts the base ends in
    is read from its LaTeX (`_last_scripts`): told from the name of the base element, a limit (`munder`, `mover`) or a
    superscript on a subscript (`a_i^2`) under another script was missed. A superscript on a subscript is left as it is."""
    if not base or (child is not None and child[0] in _GROUPS and len(child[1]) > 1):
        return f"{{{base}}}"  # an empty base too: the script went to what came before it (`x^2` and `_i` as `x^2_i`)
    return f"{{{base}}}" if any(mark in sides for mark in _last_scripts(base)) else base


def _last_scripts(latex: str) -> str:
    """The marks (`^`, `_`) of the scripts on the last item of `latex` outside braces: `^_` for `a^2_i`, `` for `{x^2}` and
    `\\frac{a}{b}`. Linear in the length of `latex`."""
    if "^" not in latex and "_" not in latex:
        return ""
    marks = ""
    depth = 0
    argument = False  # the next item is the argument of a script
    index = 0
    while index < len(latex):
        character = latex[index]
        step = 1
        if character == "\\":  # a command (`\frac`) or an escaped character (`\{`) is one item
            step = 2
            while index + step < len(latex) and latex[index + 1].isalpha() and latex[index + step].isalpha():
                step += 1
        if character == "}":
            depth -= 1
        elif depth == 0 and character in "^_":
            marks += character
            argument = True
        elif depth == 0 and not character.isspace():
            if not argument:
                marks = ""  # a new item, without scripts yet
            argument = False
        if character == "{":
            depth += 1
        index += step
    return marks


def _multiscripts(parts: list[str], children: list[Child]) -> str:
    """An `mmultiscripts`: the base, then pairs of (subscript, superscript) after it, and after an `mprescripts` the pairs
    before it; an empty script (`none`) is left out, and so is the missing superscript of an odd count. Joined as other
    elements, an isotope `¹⁴₆C` came out as `C614`. Pairs follow each other over an empty group, as a tensor is written
    (`R_{i}^{j}{}_{k}`): put side by side they gave a double subscript, and the last script of an odd count was dropped. The
    base is braced as the base of a script when scripts follow it (`_script_base`) (Pinned by: test_core_html.py::TestMathMl::
    test_scripts_of_an_mmultiscripts_follow_each_other)."""
    names = [element for element, _ in children]
    split = names.index("mprescripts") if "mprescripts" in names else len(parts)

    def scripts(pairs: list[str]) -> list[str]:
        if len(pairs) % 2:
            pairs = [*pairs, ""]
        written = ((f"_{{{lower}}}" if lower else "") + (f"^{{{upper}}}" if upper else "")
                   for lower, upper in zip(pairs[::2], pairs[1::2], strict=True))
        return [pair for pair in written if pair]

    before, after = scripts(parts[split + 1:]), scripts(parts[1:split])
    # The base takes the first pair after it; the next pairs stand on empty groups
    base = _script_base(_last_scripts(after[0]), parts[0], children[0]) if after else parts[0]
    return ("{}" + "{}".join(before) if before else "") + base + "{}".join(after)


def mathml_structure(name: str, parts: list[str], fence: tuple[str, str, str] | None = None,
                     children: list[Child] | None = None) -> str | None:
    """The LaTeX of the element `name` from the LaTeX of its child elements (`parts`): scripts, fractions, radicals, fences
    and matrices. None for an element without a rule here (the caller joins its children). `fence` is the (open, separator,
    close) of an `mfenced`; `children` are the child elements the parts come from (`base_child`)."""
    if name == "mmultiscripts" and parts and children is not None and len(children) == len(parts):
        return _multiscripts(parts, children)
    if name in MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = _script_base(_SIDES[name], parts[0], children[0] if children else None), parts[1:]
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
