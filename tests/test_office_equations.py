"""Office equations (OMML → LaTeX, `_shared.omml_to_latex`): fractions without a bar, characters right after a one-character script,
group characters, brace delimiters, systems of equations.

DOCX and PPTX use the same function. The documents are built as DOCX for the checks.
"""

import pytest

from opendockit import convert
from tests.test_office import _docx_with_body


def _run(text: str) -> str:
    return f"<m:r><m:t>{text}</m:t></m:r>"


def _fraction(numerator: str, denominator: str, kind: str = "") -> str:
    properties = f'<m:fPr><m:type m:val="{kind}"/><m:ctrlPr/></m:fPr>' if kind else ""
    return f"<m:f>{properties}<m:num>{_run(numerator)}</m:num><m:den>{_run(denominator)}</m:den></m:f>"


def _delimiters(content: str, begin: str | None = None, end: str | None = None) -> str:
    """m:d. When `begin`/`end` is None the attribute is not written (default delimiters `(` and `)`)."""
    properties = "".join(f'<m:{name} m:val="{value}"/>' for name, value in (("begChr", begin), ("endChr", end))
                         if value is not None)
    return f"<m:d><m:dPr>{properties}<m:ctrlPr/></m:dPr><m:e>{content}</m:e></m:d>"


def _script(kind: str, base: str, script: str) -> str:
    return f"<m:{kind}><m:e>{_run(base)}</m:e><m:{kind[1:].lower()}>{_run(script)}</m:{kind[1:].lower()}></m:{kind}>"


def _function(name: str, argument: str) -> str:
    return f"<m:func><m:fName>{_run(name)}</m:fName><m:e>{_run(argument)}</m:e></m:func>"


def _equations(*equations: str) -> str:
    return "".join(f"<w:p><m:oMath>{equation}</m:oMath></w:p>" for equation in equations)


class TestOmmlFractionsWithoutABar:
    """A fraction without a bar (m:fPr/m:type noBar) came out as `\\frac`, so the binomial coefficient of Word's built-in 'Binomial
    Theorem' equation read as a division `(\\frac{n}{k})`. Hancom equations (test_equation.py) already give `\\binom`."""

    def test_fraction_without_a_bar_in_parentheses_is_a_binomial_coefficient(self):
        body = _equations(_delimiters(_fraction("n", "k", "noBar")))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$\\binom{n}{k}$"

    def test_bare_fraction_without_a_bar_is_stacked_without_parentheses(self):
        body = _equations(_fraction("n", "k", "noBar"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "${n \\atop k}$"

    def test_linear_and_skewed_fractions_are_written_with_a_slash(self):
        body = _equations(_fraction("a+b", "c", "lin"), _fraction("1", "2", "skw"), _fraction("x", "y", "bar"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "${a+b}/c$\n\n$1/2$\n\n$\\frac{x}{y}$"


class TestOmmlScriptsFollowedByText:
    """A character directly following a one-character script made the script and the next character read as one, as in
    `x^ka^{n-k}` and `a_ncos`."""

    def test_one_character_script_followed_by_a_letter_is_braced(self):
        body = _equations(_script("sSup", "x", "k") + _script("sSup", "a", "n-k"),
                          _script("sSup", "a", "2") + _run("+") + _script("sSup", "b", "2"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$x^{k}a^{n-k}$\n\n$a^2+b^2$"

    def test_function_name_is_separated_from_what_comes_before_it(self):
        body = _equations(_script("sSub", "a", "n") + _function("cos", "x"), _run("2") + _function("sin", "x"),
                          _function("cos", "y"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$a_n cos x$\n\n$2 sin x$\n\n$cos y$"


class TestOmmlScriptOnAScriptedBase:
    """When the base already carries a script on the same side (`(x²)³`) the result was `x^2^3`. That is invalid LaTeX and is
    confused with `x^{2^3}`. The base is braced."""

    def test_script_whose_base_has_the_same_script_is_braced(self):
        squared = "<m:sSup><m:e>" + _run("x") + "</m:e><m:sup>" + _run("2") + "</m:sup></m:sSup>"
        indexed = "<m:sSub><m:e>" + _run("a") + "</m:e><m:sub>" + _run("i") + "</m:sub></m:sSub>"
        body = _equations(f"<m:sSup><m:e>{squared}</m:e><m:sup>{_run('3')}</m:sup></m:sSup>",
                          f"<m:sSub><m:e>{indexed}</m:e><m:sub>{_run('j')}</m:sub></m:sSub>",
                          f"<m:sSup><m:e>{indexed}</m:e><m:sup>{_run('2')}</m:sup></m:sSup>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "${x^2}^3$\n\n${a_i}_j$\n\n$a_i^2$"


class TestOmmlGroupCharactersAndBraces:
    """The character of a group character (m:groupChr) was dropped, brace delimiters were read as LaTeX grouping (`{a,b}`) and
    vanished, and a system of equations (an m:eqArr inside an m:d that has only the opening `{`) came out without a closing
    delimiter as `\\{x+y=1 \\\\ x-y=3`."""

    def test_group_character_is_kept(self):
        default = f"<m:limLow><m:e><m:groupChr><m:e>{_run('a+b')}</m:e></m:groupChr></m:e><m:lim>{_run('n개')}</m:lim></m:limLow>"
        arrow = ('<m:groupChr><m:groupChrPr><m:chr m:val="→"/><m:pos m:val="top"/></m:groupChrPr>'
                 f"<m:e>{_run('x')}</m:e></m:groupChr>")
        body = _equations(default, arrow)

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$\\underbrace{a+b}_{n개}$\n\n$\\overset{→}{x}$"

    def test_braces_used_as_delimiters_are_escaped(self):
        body = _equations(_delimiters(_run("a,b"), "{", "}"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$\\{a,b\\}$"

    def test_system_of_equations_is_written_as_cases(self):
        rows = f"<m:eqArr><m:e>{_run('x+y=1')}</m:e><m:e>{_run('x-y=3')}</m:e></m:eqArr>"
        body = _equations(_delimiters(rows, "{", ""))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$\\begin{cases}x+y=1 \\\\ x-y=3\\end{cases}$"


class TestOmmlTrackedChanges:
    """Text deleted inside an equation under change tracking (an m:r wrapped by w:del / w:moveFrom) was read too, so an equation
    whose coefficient was changed from 0.3 to 0.5 became `E=A×0.30.5`. As in body paragraphs, the deleted side is left out and
    only the inserted side (w:ins / w:moveTo) is kept."""

    def test_deleted_math_run_is_left_out_and_inserted_one_is_kept(self):
        equation = (_run("E=A×") + '<w:del w:id="1" w:author="a"><m:r><w:rPr><w:del w:id="3" w:author="a"/></w:rPr>'
                    "<m:t>0.3</m:t></m:r></w:del>" + f'<w:ins w:id="2" w:author="a">{_run("0.5")}</w:ins>')
        body = _equations(equation)

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$E=A×0.5$"

    def test_math_moved_away_is_left_out_and_kept_at_its_new_place(self):
        equation = (f'<w:moveFrom w:id="1" w:author="a">{_run("b+")}</w:moveFrom>' + _run("a")
                    + f'<w:moveTo w:id="2" w:author="a">{_run("+b")}</w:moveTo>')
        body = _equations(equation)

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$a+b$"


class TestOmmlMathAlphabets:
    """The math script style (m:rPr/m:scr: double-struck, script, fraktur) was not read, so the set of real numbers ℝ came out as R
    and the Laplace transform ℒ as L, the same as ordinary variables. Only the letters are wrapped in the LaTeX commands
    (`\\mathbb`, `\\mathcal`, `\\mathfrak`)."""

    def test_double_struck_script_and_fraktur_letters_are_marked(self):
        def styled(text: str, style: str) -> str:
            return f'<m:r><m:rPr><m:scr m:val="{style}"/><m:sty m:val="p"/></m:rPr><m:t>{text}</m:t></m:r>'

        equation = (_run("x∈") + styled("R", "double-struck") + _run(", ") + styled("L", "script") + _run("{f}, ")
                    + styled("g", "fraktur") + _run(", ") + styled("x", "roman") + _run(", ") + styled("∈", "double-struck"))
        body = _equations(equation)

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$x∈\\mathbb{R}, \\mathcal{L}{f}, \\mathfrak{g}, x, ∈$"


class TestOmmlPhantom:
    """A phantom (m:phant) is text that only takes up space. The text of one made invisible (m:show 0 — the ECMA-376 default is on)
    was joined too, giving `hiddenx`. A visible phantom (the default) keeps its text."""

    @pytest.mark.parametrize("value", ["0", "off", "false"])
    def test_hidden_phantom_is_left_out(self, value):
        phantom = f'<m:phant><m:phantPr><m:show m:val="{value}"/></m:phantPr><m:e>{_run("hidden")}</m:e></m:phant>'
        body = _equations(phantom + _run("x"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$x$"

    def test_shown_phantom_keeps_its_text(self):
        phantom = f'<m:phant><m:phantPr><m:zeroWid/></m:phantPr><m:e>{_run("a")}</m:e></m:phant>'
        body = _equations(phantom + _run("+b"))

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$a+b$"
