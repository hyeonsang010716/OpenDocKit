"""Hancom equation script (HWP equation record, HWPX hp:script) → LaTeX.

The rules follow Hancom's public document "Hangul Document File Structure - Equation" (revision 1.3).

- Terms are separated by whitespace (space, line break, `~`, `` ` ``). The numerator of `10a^3 over b^2` is `10a^3`, and the
  subscript of `lim_N->inf` is the whole `N->inf`. So `over` and scripts take whitespace-separated terms, not words.
- Commands are case-insensitive (`OVER`, `over`). Only Greek letters, arrows and `deg`/`DEG` change meaning with case
  (`alpha` α vs `Alpha` Α, `larrow` ← vs `LARROW` ⇐).
- Basic functions are read as functions even when written without a space (`logx` → log x), and a word made entirely of
  command words is split into them (`costheta` → cos θ; `_split_glued_words`).
- `root n of x` is the n-th root, `root x` (no `of`) the square root.

Returns None when the script cannot be converted, and the caller keeps the original script. Deliberate: unknown commands
(LADDER, LONGDIV, ...), unbalanced braces, excessive nesting or length, and even defects in the converter itself all come back
as None. A single equation must not get the whole document rejected as 'corrupted' (Pinned by: test_equation.py::TestFallback).
"""

import re
from typing import Final


# Keeps `{{{{…` in a crafted file from blowing up the recursion or burning time. Real equations are far below both limits.
_MAX_SCRIPT_CHARS: Final = 10_000
_MAX_DEPTH: Final = 64

_TOKEN = re.compile(
    r'"[^"]*"?'                     # quoted text (a group of words longer than 9 characters)
    r"|[\s~`]+"                     # term separators
    r"|[{}^_&#]"
    r"|[A-Za-z]+"
    r"|[0-9]+(?:\.[0-9]+)?"
    r"|[가-힣ㄱ-ㅎㅏ-ㅣ]+"
    r"|<->|->|<-|!=|==|<<<|>>>|<<|>>|<=|>=|\+-|-\+"
    r"|.",
    re.DOTALL,
)

_GREEK: Final = frozenset({
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa", "lambda", "mu", "nu", "xi",
    "omicron", "pi", "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
})
# Greek letters whose capital has no LaTeX command are the Latin letter of the same shape.
_LATIN_CAPITALS: Final = {
    "alpha": "A", "beta": "B", "epsilon": "E", "zeta": "Z", "eta": "H", "iota": "I", "kappa": "K", "mu": "M", "nu": "N",
    "omicron": "O", "rho": "P", "tau": "T", "chi": "X",
}

# Names looked up with their case as is. For arrows, lowercase is a single line and uppercase a double line. deg is a function,
# DEG is the degree sign (°).
_EXACT: Final = {
    "larrow": r"\leftarrow", "rarrow": r"\rightarrow", "uparrow": r"\uparrow", "downarrow": r"\downarrow",
    "LARROW": r"\Leftarrow", "RARROW": r"\Rightarrow", "UPARROW": r"\Uparrow", "DOWNARROW": r"\Downarrow",
    "udarrow": r"\updownarrow", "lrarrow": r"\leftrightarrow", "UDARROW": r"\Updownarrow", "LRARROW": r"\Leftrightarrow",
    "leftarrow": r"\leftarrow", "rightarrow": r"\rightarrow",
    "vert": "|", "VERT": r"\|", "DEG": r"^{\circ}",
}

# Basic functions and reserved words set in roman type (spec 1.1.3). A matching prefix alone is read as a function.
_FUNCTIONS: Final = {
    "sin": r"\sin", "cos": r"\cos", "tan": r"\tan", "cot": r"\cot", "sec": r"\sec", "csc": r"\csc",
    "cosec": r"\operatorname{cosec}", "sinh": r"\sinh", "cosh": r"\cosh", "tanh": r"\tanh", "coth": r"\coth",
    "arcsin": r"\arcsin", "arccos": r"\arccos", "arctan": r"\arctan", "arc": r"\operatorname{arc}",
    "asin": r"\operatorname{asin}", "acos": r"\operatorname{acos}", "atan": r"\operatorname{atan}",
    "log": r"\log", "ln": r"\ln", "lg": r"\lg", "exp": r"\exp", "Exp": r"\exp", "lim": r"\lim", "Lim": r"\lim",
    "max": r"\max", "min": r"\min", "det": r"\det", "gcd": r"\gcd", "lcm": r"\operatorname{lcm}", "mod": r"\bmod",
    "hom": r"\hom", "ker": r"\ker", "deg": r"\deg", "arg": r"\arg", "dim": r"\dim", "Pr": r"\Pr",
    "if": r"\text{if}", "for": r"\text{for}", "and": r"\text{and}",
}
# A run-together prefix (`logx`) is split off only for math functions. if, for, and, Pr overlap with word prefixes (format, Prob).
_FUNCTION_PREFIX: Final = re.compile("|".join(sorted((name for name in _FUNCTIONS if name not in ("if", "for", "and", "Pr")),
                                                 key=len, reverse=True)))

# Case-insensitive symbols (lowercase keys). The symbol table of spec 1.2.4.
_SYMBOLS: Final = {
    # big operators — scripts and from/to become the lower and upper limits
    "sum": r"\sum", "prod": r"\prod", "coprod": r"\coprod", "int": r"\int", "oint": r"\oint", "dint": r"\iint",
    "tint": r"\iiint", "odint": r"\oiint", "otint": r"\oiiint", "union": r"\bigcup", "inter": r"\bigcap",
    "smallunion": r"\cup", "smallinter": r"\cap", "smallsum": r"\sum", "smallprod": r"\prod",
    "bigoplus": r"\bigoplus", "bigotimes": r"\bigotimes", "bigodot": r"\bigodot", "biguplus": r"\biguplus",
    "bigsqcup": r"\bigsqcup",
    # sums and sets
    "cap": r"\cap", "cup": r"\cup", "sqcap": r"\sqcap", "sqcup": r"\sqcup", "oplus": r"\oplus", "ominus": r"\ominus",
    "otimes": r"\otimes", "odot": r"\odot", "oslash": r"\oslash", "vee": r"\vee", "wedge": r"\wedge", "land": r"\land",
    "lor": r"\lor", "subset": r"\subset", "supset": r"\supset", "superset": r"\supset", "nsubset": r"\not\subset",
    "subseteq": r"\subseteq", "supseteq": r"\supseteq", "in": r"\in", "owns": r"\ni", "notin": r"\notin",
    # `le`/`ge` only as words of their own: a token is a whole run of letters, and they are never split off a glued word
    # (Pinned by: test_equation.py::TestAddedWords)
    "leq": r"\leq", "geq": r"\geq", "le": r"\leq", "ge": r"\geq", "sqsubset": r"\sqsubset", "sqsupset": r"\sqsupset",
    "sqsubseteq": r"\sqsubseteq", "sqsupseteq": r"\sqsupseteq", "lll": r"\lll",
    "prec": r"\prec", "succ": r"\succ", "uplus": r"\uplus",
    # operations and logic
    "times": r"\times", "div": r"\div", "divide": r"\div", "plusminus": r"\pm", "minusplus": r"\mp", "cdot": r"\cdot",
    "circ": r"\circ",
    "bullet": r"\bullet", "deg": r"^{\circ}", "ast": r"\ast", "star": r"\star", "bigcirc": r"\bigcirc",
    "emptyset": r"\emptyset", "therefore": r"\therefore", "because": r"\because", "identical": r"\text{∷}",
    "exist": r"\exists", "neq": r"\neq", "doteq": r"\doteq", "image": r"\fallingdotseq", "reimage": r"\risingdotseq",
    "sim": r"\sim", "approx": r"\approx", "simeq": r"\simeq", "cong": r"\cong", "equiv": r"\equiv", "asymp": r"\asymp",
    "iso": r"\Bumpeq", "diamond": r"\diamond", "dsum": r"\dotplus", "forall": r"\forall", "prime": "'",
    "partial": r"\partial", "inf": r"\infty", "infinity": r"\infty", "lnot": r"\neg", "propto": r"\propto",
    "xor": r"\veebar", "triangled": r"\nabla", "nabla": r"\nabla", "dagger": r"\dagger", "ddagger": r"\ddagger",
    # arrows (when the case does not match)
    "larrow": r"\leftarrow", "rarrow": r"\rightarrow", "uparrow": r"\uparrow", "downarrow": r"\downarrow",
    "udarrow": r"\updownarrow", "lrarrow": r"\leftrightarrow", "nwarrow": r"\nwarrow", "searrow": r"\searrow",
    "nearrow": r"\nearrow", "swarrow": r"\swarrow", "hookleft": r"\hookleftarrow", "hookright": r"\hookrightarrow",
    "mapsto": r"\mapsto",
    # miscellaneous
    "cdots": r"\cdots", "ldots": r"\ldots", "vdots": r"\vdots", "ddots": r"\ddots", "triangle": r"\triangle",
    "angle": r"\angle", "msangle": r"\measuredangle", "sangle": r"\sphericalangle", "rtangle": r"\text{⊾}",
    "vdash": r"\vdash", "hleft": r"\dashv", "bot": r"\bot", "top": r"\top", "models": r"\models",
    "laplace": r"\mathcal{L}", "centigrade": r"{}^{\circ}\mathrm{C}", "fahrenheit": r"{}^{\circ}\mathrm{F}",
    "lslant": "/", "rslant": r"\backslash", "att": r"\text{※}", "hund": r"\text{‰}", "thou": r"\text{‱}",
    "box": r"\boxed{}", "well": r"\#", "base": r"\text{⌂}", "benzene": r"\text{⌬}",
    "aleph": r"\aleph", "hbar": r"\hbar", "imath": r"\imath", "jmath": r"\jmath", "ohm": r"\Omega", "ell": r"\ell",
    "liter": r"\ell", "wp": r"\wp", "imag": r"\Im", "angstrom": r"\text{Å}", "vartheta": r"\vartheta",
    "varpi": r"\varpi", "varsigma": r"\varsigma", "varupsilon": r"\upsilon", "varphi": r"\varphi",
    "varepsilon": r"\varepsilon",
}

_OPERATORS: Final = {
    "->": r"\to", "<-": r"\leftarrow", "<->": r"\leftrightarrow", "!=": r"\neq", "==": r"\equiv",
    "<<": r"\ll", ">>": r"\gg", "<<<": r"\lll", ">>>": r"\ggg", "<=": r"\leq", ">=": r"\geq", "+-": r"\pm", "-+": r"\mp",
    "$": r"\$", "%": r"\%", "\\": r"\backslash ",
}

# Commands that take the terms before and after. The term before is the one already read, the term after is the next one.
_INFIX: Final = {
    "over": lambda top, bottom: rf"\frac{{{top}}}{{{bottom}}}",
    "atop": lambda top, bottom: rf"{{{top} \atop {bottom}}}",
    "choose": lambda top, bottom: rf"\binom{{{top}}}{{{bottom}}}",
}
_DECORATIONS: Final = {
    "acute": r"\acute", "grave": r"\grave", "dot": r"\dot", "ddot": r"\ddot", "hat": r"\hat", "check": r"\check",
    "tilde": r"\tilde", "bar": r"\overline", "vec": r"\vec", "dyad": r"\overleftrightarrow", "under": r"\underline",
    "arch": r"\overset{\frown}",
}
_ENVIRONMENTS: Final = {
    "matrix": "matrix", "pmatrix": "pmatrix", "bmatrix": "bmatrix", "dmatrix": "vmatrix", "cases": "cases",
    "pile": "matrix", "lpile": "matrix", "rpile": "matrix", "eqalign": "aligned",
}
_DELIMITERS: Final = {
    "(": "(", ")": ")", "[": "[", "]": "]", "{": r"\{", "}": r"\}", "|": "|", "<": r"\langle", ">": r"\rangle",
    ".": ".", "lbrace": r"\{", "rbrace": r"\}", "langle": r"\langle", "rangle": r"\rangle", "vert": "|", "VERT": r"\|",
}
# Commands that attach a term to the preceding one: subscript (_), superscript (^) and limits.
_SCRIPTS: Final = {"^": "^", "sup": "^", "to": "^", "_": "_", "sub": "_", "from": "_"}
_PRESCRIPTS: Final = {"lsub": "_", "lsup": "^"}
_SPACED_SCRIPTS: Final = frozenset({"^", "_", "sup", "sub"})  # scripts consumed by the term after a fraction (not the from/to limits)
# Commands that take the following term as an argument (`_Parser._command`).
_COMMANDS: Final = frozenset({
    "sqrt", "root", "binom", "overbrace", "underbrace", "left", "not", "color", "scale", "rel", "buildrel", *_DECORATIONS, *_ENVIRONMENTS,
})
_DROPPED: Final = frozenset({"rm", "it", "bold", "bigg", "col", "lcol", "rcol"})
_UNSUPPORTED: Final = frozenset({"ladder", "sladder", "longdiv"})
# Words (lowercase) that end a term. They are not consumed inside a term but handled outside it.
_TERM_ENDS: Final = frozenset({"}", "&", "#", "right", "of", *_INFIX, *_SCRIPTS, *_PRESCRIPTS})
_CLOSERS: Final = frozenset({"}", "&", "#"})
_HANGUL: Final = re.compile("[가-힣ㄱ-ㅎㅏ-ㅣ]")
# Quoted text and Hangul are wrapped in \text{} and LaTeX special characters are escaped with text-mode commands: `\backslash` is
# math mode only and was rejected inside \text{} (Pinned by: test_equation.py::TestRules::
# test_backslash_caret_and_tilde_in_text_use_text_mode_commands).
_TEXT_ESCAPES: Final = str.maketrans({
    **{char: "\\" + char for char in "{}$%#&_"}, "\\": r"\textbackslash{}", "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
})
_TEXT_RUN: Final = re.compile(r"\\text\{[^{}\\]*\}(?:\s+\\text\{[^{}\\]*\})+")
_TEXT_PIECE: Final = re.compile(r"\\text\{([^{}\\]*)\}")
# The spec's n-th root `` `^3sqrt x `` (a superscript on a blank + a radical) becomes a LaTeX root index.
_ROOT_INDEX: Final = re.compile(r"(?:^|(?<= ))\{\}\^\{([^{}]*)\} ?\\sqrt\{")  # not a script attached to another term
_COMMAND_END: Final = re.compile(r"\\[A-Za-z]+$")


# Words a run-together word is split into (`costheta`, `timesrmbar`, `barAR`; `_split_glued_words`). Structural words (over,
# to, from, of, left, …), environments and `root` are left out, and so are `le`/`ge` (`pile` is not π + le), `it` and `bold`
# (`limit` is not lim + a font change), the case-sensitive Exp/Lim/Pr and if/for/and, and the symbols that end or start
# ordinary words: in (login, bulletin), ker (tanker), well (maxwell), iso (isobar), land (inland), arch (toparch).
_GLUE_WORDS: Final = frozenset(
    {*_GREEK, *_FUNCTION_PREFIX.pattern.split("|"), *_SYMBOLS, *_DECORATIONS, "sqrt", "rm"}
    - {"le", "ge", "in", "ker", "well", "iso", "land", "arch", "Exp", "Lim"})
# Glue words that take the term after them (a decoration, `sqrt`): a glued word may end on one only right before a `{`
# (`timesrmbar{PD}`). `thetadot` at the end of a script had nothing to decorate and the whole equation fell back to the
# script, and `pibar x` put the bar over the next term (Pinned by: test_equation.py::TestGluedWords::
# test_word_ending_on_a_word_that_takes_an_operand_is_not_split).
_GLUE_OPERAND_WORDS: Final = frozenset(word for word in _GLUE_WORDS if word in _COMMANDS)
_GLUE_LONGEST: Final = max(map(len, _GLUE_WORDS))
_GLUED: Final = re.compile("([a-z]+)([A-Z]*)")


class _Unsupported(Exception):
    """A script that cannot be converted. The caller uses the original."""


def render_equation(script: str) -> str:
    """Equation to put in the body: ` $LaTeX$ `, or the original ` [Equation: …] ` when it cannot be converted. Same shape as a Word
    equation (`_omml.omml_to_latex`)."""
    latex = equation_to_latex(script)
    if latex is not None:
        return f" ${latex}$ "
    return f" [Equation: {' '.join(script.split())}] " if script.strip() else ""


def equation_to_latex(script: str) -> str | None:
    """Convert a Hancom equation script to LaTeX. None when it cannot be converted."""
    if not script.strip() or len(script) > _MAX_SCRIPT_CHARS:
        return None
    try:
        latex = _Parser(_split_glued_words(_TOKEN.findall(script))).document()
    except Exception:  # noqa: BLE001
        return None  # Deliberate: unknown structures and converter defects alike fall back to the original (module docstring)
    latex = _TEXT_RUN.sub(lambda match: r"\text{" + " ".join(_TEXT_PIECE.findall(match.group())) + "}", latex)
    latex = _ROOT_INDEX.sub(r"\\sqrt[\1]{", " ".join(latex.split()))
    if _has_repeated_script(latex):
        return None
    return latex or None


def _has_repeated_script(latex: str) -> bool:
    """True if a term carries two superscripts or two subscripts (`x^{2}^{3}`), which LaTeX rejects. `x^2^3` in a script came
    out that way; what Hancom draws for it is not in the spec, so it is not guessed (nested or not) and the caller keeps the
    original script (deliberate. Pinned by: test_audit_hangul.py::TestEquationWithARepeatedScript)."""
    seen = ""  # scripts already on the term being read
    outer: list[str] = []  # what `seen` goes back to at the `}` of each open group
    index = 0
    while index < len(latex):
        char = latex[index]
        if char in "^_" and latex[index + 1:index + 2] == "{":
            if char in seen:
                return True
            outer.append(seen + char)
            seen = ""
            index += 1
        elif char == "\\":
            seen = ""
            index += 1  # an escaped character (`\{`, `\_`) is neither a brace nor a script
        elif char == "{":
            outer.append("")
            seen = ""
        elif char == "}":
            seen = outer.pop() if outer else ""
        elif not char.isspace():
            seen = ""
        index += 1
    return False


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._index = 0
        self._depth = 0

    def document(self) -> str:
        rows = self._rows(closing=None)
        if len(rows) == 1 and len(rows[0]) == 1:
            return rows[0][0]
        return _environment("aligned", rows)

    def _rows(self, closing: str | None) -> list[list[str]]:
        """A table split by `&` (cell) and `#` (row). With `closing` None it reads to the end of the script; with `}` it reads up
        to that brace and consumes it."""
        rows: list[list[str]] = []
        cells: list[str] = []
        while True:
            cells.append(_join(self._items()))
            token = self._raw()
            if token in ("&", "#"):
                self._index += 1
                if token == "#":
                    rows.append(cells)
                    cells = []
                continue
            if token != closing:
                raise _Unsupported(f"unbalanced {token!r}")

            if closing is not None:
                self._index += 1
            rows.append(cells)
            return rows

    def _items(self, stop: frozenset[str] = frozenset()) -> list[str]:
        """Whitespace-separated terms. Stops before `}`, `&`, `#`, the end of the script, or a word (lowercase) in `stop`
        (with whitespace already skipped)."""
        items: list[str] = []
        while True:
            if (token := self._raw()) is not None and _is_space(token):
                # `~` and `` ` `` are visible blanks, so they are terms that can take a script (`` `^3sqrt x `` is a cube root).
                self._index += 1
                if "~" in token or "`" in token:
                    items.append("")
                continue
            if token is None or token in _CLOSERS or _key(token) in stop:
                return items

            key = _key(token)
            if key in _INFIX:
                self._index += 1
                while items and not items[-1]:
                    items.pop()  # `{a+b} ~over~ {c}` — so the visible blank before it does not steal the numerator
                top = items.pop() if items else ""
                # The braces of an `\atop` are its own group, so they stay on one that is the top of the next: stripped,
                # `a atop b atop c` came out as `{a \atop b \atop c}`, which TeX rejects as ambiguous, and every repetition
                # scanned the whole top again (`a atop a atop …`, 20 such equations in a 1.6 KB file: 10 s)
                # (Pinned by: test_equation.py::TestRepeatedStacks).
                top = top if key == "atop" and isinstance(top, _Stack) else _bare(top)
                latex = _INFIX[key](top, _bare(self._scripted_term()))
                items.append(_Stack(latex) if key == "atop" else latex)
            elif key in _SCRIPTS:
                self._index += 1
                script = f"{_SCRIPTS[key]}{{{_bare(self._script())}}}"
                base = items.pop() if items else ""
                items.append((base or "{}") + script)
            elif key in _PRESCRIPTS:
                self._index += 1
                script = f"{{}}{_PRESCRIPTS[key]}{{{_bare(self._term())}}}"
                items.append(script + (items.pop() if items else ""))
            else:
                start = self._index
                items.append(self._term())
                if self._index == start:
                    raise _Unsupported(f"unreadable {token!r}")  # an unpaired 'right' or 'of' word

    def _term(self, *, scripts: bool = True) -> str:
        """Pieces joined without whitespace (one term). `scripts=False` means the term is the target of a script, so it also stops
        before `^`/`_` and before a command that takes an argument (the superscript of `^3sqrt x` is `3`)."""
        self._skip_space()
        pieces: list[str] = []
        while (token := self._raw()) is not None and not _is_space(token):
            key = _key(token)
            if key in ("^", "_") and scripts:
                self._index += 1
                pieces.append(f"{key}{{{_bare(self._script())}}}")
            elif key in _TERM_ENDS or (not scripts and pieces and key in _COMMANDS):
                break
            else:
                pieces.append(self._piece())
        return _concat(pieces)

    def _scripted_term(self) -> str:
        """The term after a fraction: one term and the spaced super/subscripts that follow it. Hancom stores them spaced, as in
        `a ^{2}`, so folding before the scripts were consumed turned `x ^{2} over a ^{2}` into (x²/a)². Limits (from/to) are not
        consumed — they may be the limits of a Σ after the fraction
        (Pinned by: test_equation.py::TestRules::test_spaced_scripts_stay_on_the_fraction_side_they_follow)."""
        term = self._term()
        while True:
            self._skip_space()
            token = self._raw()
            if token is None or _key(token) not in _SPACED_SCRIPTS:
                return term
            self._index += 1
            term = (term or "{}") + f"{_SCRIPTS[_key(token)]}{{{_bare(self._script())}}}"

    def _script(self) -> str:
        """The target of a script or limit. If it starts with `{…}`, only that group (the +y of `x^{2}+y` is outside the exponent);
        otherwise the pieces joined up to whitespace, a script or a command (`lim_N->inf`)."""
        self._skip_space()
        if self._raw() == "{":
            return self._piece()
        return self._term(scripts=False)

    def _operand(self) -> str:
        """The argument of a command: a single piece. The dx of `sqrt{x^2 +1}dx` is outside the radical, and a script that follows
        attaches to the whole command (`sqrt{x}^2`)."""
        self._skip_space()
        if (token := self._raw()) is None or token in _CLOSERS:
            raise _Unsupported("missing argument")
        return self._piece()

    def _piece(self) -> str:
        self._depth += 1
        if self._depth > _MAX_DEPTH:
            raise _Unsupported("nesting too deep")
        try:
            return self._read_piece()
        finally:
            self._depth -= 1

    def _read_piece(self) -> str:
        token = self._next()
        if token == "{":
            body = _join(self._items())
            if self._next() != "}":
                raise _Unsupported("unclosed {")
            return _Group(f"{{{body}}}")
        if token.startswith('"'):
            return _text(token.strip('"'))
        if token.isascii() and token.isalpha():
            return self._word(token)
        if _HANGUL.match(token):
            return _text(token)
        return _OPERATORS.get(token, token)

    def _word(self, word: str) -> str:
        key = word.lower()
        if word in _EXACT:
            return _EXACT[word]
        if key in _GREEK:
            return _greek(word)
        if word in _FUNCTIONS:
            return _FUNCTIONS[word]
        if key in _UNSUPPORTED:
            raise _Unsupported(word)
        if key in _DROPPED:
            return ""
        if key in _COMMANDS:
            return self._command(key)
        if key in _SYMBOLS:
            return _SYMBOLS[key]
        if (prefix := _FUNCTION_PREFIX.match(word)) is not None:
            return f"{_FUNCTIONS[prefix.group()]} {word[prefix.end():]}"
        return word

    def _command(self, key: str) -> str:
        """A command that takes the following term as an argument."""
        if key == "sqrt":
            return rf"\sqrt{{{_bare(self._operand())}}}"
        if key == "binom":
            top = _bare(self._operand())  # spec: BINOM [whole term] [chosen term]
            return rf"\binom{{{top}}}{{{_bare(self._operand())}}}"
        if key == "root":
            # `root n of x` is the n-th root; `root x` without `of` is the square root of x, as in Hangul's editor. Read as
            # `root n of x` regardless, `30 root5 cos theta` came out as `\sqrt[5]{\cos} \theta` and `2 over root5` fell back
            # to the original script (Pinned by: test_equation.py::TestRootWithoutOf).
            argument = _bare(self._operand())
            after = self._index
            if not self._take("of"):
                self._index = after  # the blank after the argument still ends the term
                return rf"\sqrt{{{argument}}}"
            return rf"\sqrt[{argument}]{{{_bare(self._operand())}}}"
        if key in ("overbrace", "underbrace"):
            # OVERBRACE {body} {label}: the label goes above (below) the brace (Pinned by: test_equation.py::TestAddedWords).
            body = _bare(self._operand())
            mark = "^" if key == "overbrace" else "_"
            return rf"\{key}{{{body}}}{mark}{{{_bare(self._operand())}}}"
        if key in _DECORATIONS:
            return f"{_DECORATIONS[key]}{{{_bare(self._operand())}}}"
        if key in _ENVIRONMENTS:
            if self._next() != "{":
                raise _Unsupported(f"{key} is not followed by {{")
            return _environment(_ENVIRONMENTS[key], self._rows(closing="}"))
        if key == "left":
            opening = self._delimiter()
            body = _join(self._items(stop=frozenset({"right"})))
            closing = self._delimiter() if self._take("right") else "."
            return rf"\left{opening} {body} \right{closing}"
        if key == "not":
            return _concat([r"\not", self._operand()])
        if key == "color":
            self._operand()  # {R,G,B}
            return self._operand()
        if key == "scale":
            self._term()  # size ratio (%)
            return ""

        arrow = self._operand()  # rel, buildrel: text above (and below) the arrow
        above = _bare(self._operand())
        if key == "buildrel":
            return rf"\overset{{{above}}}{{{arrow}}}"
        return rf"\overset{{{above}}}{{\underset{{{_bare(self._operand())}}}{{{arrow}}}}}"

    def _delimiter(self) -> str:
        self._skip_space()
        token = self._raw()
        if token is None:
            return "."
        delimiter = _DELIMITERS.get(token) or _DELIMITERS.get(token.lower())
        if delimiter is None:
            return "."
        self._index += 1
        return delimiter

    def _take(self, word: str) -> bool:
        """If the next token is the word `word`, consume it and return True."""
        self._skip_space()
        token = self._raw()
        if token is None or _key(token) != word:
            return False
        self._index += 1
        return True

    def _raw(self) -> str | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _skip_space(self) -> None:
        while (token := self._raw()) is not None and _is_space(token):
            self._index += 1

    def _next(self) -> str:
        self._skip_space()
        token = self._raw()
        if token is None:
            raise _Unsupported("the script ended midway")
        self._index += 1
        return token


def _split_glued_words(tokens: list[str]) -> list[str]:
    """Split a run-together word the parser would not know into the command words it is made of. Script writers glue command
    words (`costheta`, `timesrmbar{PD}`, `rmAB`, `barAR`), and the word came out as text (`\\cos theta`, `timesrmbar`).

    Only a word whose lowercase part splits completely into `_GLUE_WORDS` is split, longest word first; an uppercase tail is
    kept as one term (point names: `barAR` is the bar over AR). A word with anything else left over (`sinx`, `pile`, `limit`)
    is not split. Linear: each position tries a bounded number of word lengths, and every word is split once
    (Pinned by: test_equation.py::TestGluedWords)."""
    split: list[str] = []
    for index, token in enumerate(tokens):
        if token.isascii() and token.isalpha() and not _is_known_word(token):
            parts = _glued_parts(token)
            if parts is not None and _splits(parts, tokens[index + 1] if index + 1 < len(tokens) else ""):
                split.extend(parts)
                continue
        split.append(token)
    return split


def _splits(parts: list[str], following: str) -> bool:
    """Whether a glued word is split into `parts`: not when its last part takes an operand that is not a `{` written right after
    it (`thetadot`, `a = pibar`, `pibar x`, `rmhat` stay one word, as before glued words were split), and not when it is made of
    `rm` alone (`rmrm`, a font change with nothing to change)."""
    if parts[-1] in _GLUE_OPERAND_WORDS and following != "{":
        return False
    return any(part != "rm" for part in parts)


def _is_known_word(word: str) -> bool:
    key = word.lower()
    return (word in _EXACT or word in _FUNCTIONS or key in _GREEK or key in _UNSUPPORTED or key in _DROPPED
            or key in _COMMANDS or key in _SYMBOLS or key in _TERM_ENDS or key in _DELIMITERS)


def _glued_parts(word: str) -> list[str] | None:
    match = _GLUED.fullmatch(word)
    if match is None:
        return None
    lower, tail = match.groups()
    # ends[i]: the length of the longest word at i after which the rest splits too; 0 when the rest from i does not split.
    ends = [0] * (len(lower) + 1)
    ends[len(lower)] = -1  # the end of the word
    for start in range(len(lower) - 1, -1, -1):
        for length in range(min(_GLUE_LONGEST, len(lower) - start), 0, -1):
            if ends[start + length] and lower[start:start + length] in _GLUE_WORDS:
                ends[start] = length
                break
    if not ends[0]:
        return None
    parts: list[str] = []
    start = 0
    while start < len(lower):
        parts.append(lower[start:start + ends[start]])
        start += ends[start]
    return [*parts, tail] if tail else parts


def _key(token: str) -> str:
    return token.lower() if token.isascii() and token.isalpha() else token


def _is_space(token: str) -> bool:
    return token[0].isspace() or token[0] in "~`"


def _greek(word: str) -> str:
    key = word.lower()
    if not word[0].isupper():
        return "o" if key == "omicron" else "\\" + key
    return _LATIN_CAPITALS.get(key) or "\\" + key.capitalize()


def _text(text: str) -> str:
    return r"\text{" + text.translate(_TEXT_ESCAPES) + "}"


class _Group(str):
    """LaTeX that is one brace group as a whole (`{…}`): a group of the script (`_read_piece`) or an `atop`. Every other term is
    not one, so `_bare` knows without looking at the text."""

    __slots__ = ()


class _Stack(_Group):
    """The LaTeX of an `atop` (`{top \\atop bottom}`), told apart from other terms while it is still one."""

    __slots__ = ()


def _bare(text: str) -> str:
    """Strip one layer of `{…}` that wraps the whole text, so braces do not double up when it becomes an argument of `\\frac{…}{…}`.

    Whether the text is one group is carried by the term (`_Group`), not looked up in it: scanning the text for the brace that
    closes the first, `1 atop1 to 2 atop1 to 2 …` and `1 atop 1 over 1 atop 1 over 1 …` read their whole top again at every
    repetition, 0.46 s for one equation of 10,000 characters and 9 s for 20 of them in a 202 KB HWPX (Pinned by:
    test_equation.py::TestRepeatedStacks::test_stacks_under_limits_and_fractions_take_linear_time). A term made of several pieces
    is never one group: each piece is balanced, so the first brace closes before the end (`{a}+{b}`)."""
    return text[1:-1] if isinstance(text, _Group) else text


def _join(items: list[str]) -> str:
    """Join terms with a space. Visible-blank terms that took no script (empty text) are dropped; a single term is kept as it is
    (still one group if it is one)."""
    kept = [item for item in items if item]
    return kept[0] if len(kept) == 1 else " ".join(kept)


def _concat(pieces: list[str]) -> str:
    """Join the pieces inside a term. A space is inserted when a letter follows `\\sin` (`\\sinx` would be a different command).
    Linear, because only the previous piece is looked at."""
    if sum(1 for piece in pieces if piece) == 1:
        return next(piece for piece in pieces if piece)  # kept as it is (still one group if it is one)
    parts: list[str] = []
    previous = ""
    for piece in pieces:
        if not piece:
            continue
        if piece[0].isalnum() and _COMMAND_END.search(previous):
            parts.append(" ")
        parts.append(piece)
        previous = piece
    return "".join(parts)


def _environment(name: str, rows: list[list[str]]) -> str:
    body = r" \\ ".join(" & ".join(cells) for cells in rows)
    return rf"\begin{{{name}}} {body} \end{{{name}}}"
