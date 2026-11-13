"""Hancom equation script (HWP equation record, HWPX hp:script) → LaTeX.

The rules follow Hancom's public document "Hangul Document File Structure - Equation" (revision 1.3).

- Terms are separated by whitespace (space, line break, `~`, `` ` ``). The numerator of `10a^3 over b^2` is `10a^3`, and the
  subscript of `lim_N->inf` is the whole `N->inf`. So `over` and scripts take whitespace-separated terms, not words.
- Commands are case-insensitive (`OVER`, `over`). Only Greek letters, arrows and `deg`/`DEG` change meaning with case
  (`alpha` α vs `Alpha` Α, `larrow` ← vs `LARROW` ⇐).
- Basic functions are read as functions even when written without a space (`logx` → log x).

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
    "smallunion": r"\cup", "smallinter": r"\cap",
    # sums and sets
    "cap": r"\cap", "cup": r"\cup", "sqcap": r"\sqcap", "sqcup": r"\sqcup", "oplus": r"\oplus", "ominus": r"\ominus",
    "otimes": r"\otimes", "odot": r"\odot", "oslash": r"\oslash", "vee": r"\vee", "wedge": r"\wedge",
    "subset": r"\subset", "supset": r"\supset", "subseteq": r"\subseteq", "supseteq": r"\supseteq", "in": r"\in",
    "owns": r"\ni", "notin": r"\notin", "leq": r"\leq", "geq": r"\geq", "sqsubset": r"\sqsubset",
    "sqsupset": r"\sqsupset", "sqsubseteq": r"\sqsubseteq", "sqsupseteq": r"\sqsupseteq", "lll": r"\lll",
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
    "xor": r"\veebar", "triangled": r"\nabla", "dagger": r"\dagger", "ddagger": r"\ddagger",
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
    "well": r"\#", "base": r"\text{⌂}", "benzene": r"\text{⌬}",
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
    "sqrt", "root", "binom", "left", "not", "color", "scale", "rel", "buildrel", *_DECORATIONS, *_ENVIRONMENTS,
})
_DROPPED: Final = frozenset({"rm", "it", "bold", "bigg", "col", "lcol", "rcol"})
_UNSUPPORTED: Final = frozenset({"ladder", "sladder", "longdiv"})
# Words (lowercase) that end a term. They are not consumed inside a term but handled outside it.
_TERM_ENDS: Final = frozenset({"}", "&", "#", "right", "of", *_INFIX, *_SCRIPTS, *_PRESCRIPTS})
_CLOSERS: Final = frozenset({"}", "&", "#"})
_HANGUL: Final = re.compile("[가-힣ㄱ-ㅎㅏ-ㅣ]")
# Quoted text and Hangul are wrapped in \text{} and LaTeX special characters are escaped.
_TEXT_ESCAPES: Final = str.maketrans({
    **{char: "\\" + char for char in "{}$%#&_"}, "\\": r"\backslash ", "^": r"\^{}", "~": r"\~{}",
})
_TEXT_RUN: Final = re.compile(r"\\text\{[^{}\\]*\}(?:\s+\\text\{[^{}\\]*\})+")
_TEXT_PIECE: Final = re.compile(r"\\text\{([^{}\\]*)\}")
# The spec's n-th root `` `^3sqrt x `` (a superscript on a blank + a radical) becomes a LaTeX root index.
_ROOT_INDEX: Final = re.compile(r"(?:^|(?<= ))\{\}\^\{([^{}]*)\} ?\\sqrt\{")  # not a script attached to another term
_COMMAND_END: Final = re.compile(r"\\[A-Za-z]+$")


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
        latex = _Parser(_TOKEN.findall(script)).document()
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
                top = _bare(items.pop() if items else "")
                items.append(_INFIX[key](top, _bare(self._scripted_term())))
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
            return f"{{{body}}}"
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
            degree = _bare(self._operand())
            self._take("of")
            return rf"\sqrt[{degree}]{{{_bare(self._operand())}}}"
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


def _bare(text: str) -> str:
    """Strip one layer of `{…}` that wraps the whole text, so braces do not double up when it becomes an argument of `\\frac{…}{…}`."""
    if not (text.startswith("{") and text.endswith("}")):
        return text
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0 and index < len(text) - 1:
            return text  # the first `{` closes before the end: `{a}+{b}`
        index += 1
    return text[1:-1]


def _join(items: list[str]) -> str:
    """Join terms with a space. Visible-blank terms that took no script (empty text) are dropped."""
    return " ".join(item for item in items if item)


def _concat(pieces: list[str]) -> str:
    """Join the pieces inside a term. A space is inserted when a letter follows `\\sin` (`\\sinx` would be a different command).
    Linear, because only the previous piece is looked at."""
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
