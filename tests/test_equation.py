"""Hancom equation script → LaTeX (`extractors/_equation.py`).

The expected answers are the shapes drawn by the command tables and worked examples of Hancom's public document
「한글 문서 파일 구조 - 수식」 (Hangul document file format — equations, revision 1.3).
"""

import time

import pytest

from opendockit.extractors._equation import equation_to_latex, render_equation


class TestSpecExamples:
    """The scripts of spec chapter 2 'worked equation examples' must become the same expressions as the figures."""

    @pytest.mark.parametrize("script, latex", [
        ("10a^3 over b^2 times ~□ ~÷ b^3 over 2a =( 2a^2 over b )^3",
         r"\frac{10a^{3}}{b^{2}} \times □ ÷ \frac{b^{3}}{2a} =( \frac{2a^{2}}{b} )^{3}"),
        ("(A UNION B)^C` =` A^C INTER B^C", r"(A \bigcup B)^{C} = A^{C} \bigcap B^{C}"),
        ("int from 0 to 3 `^3sqrt{x^2 +1}dx", r"\int_{0}^{3} \sqrt[3]{x^{2} +1}dx"),
        ("X = bmatrix {   42 & 52 & 48 & 58 #\n 4 & 5 & 4 & 3 }",
         r"X = \begin{bmatrix} 42 & 52 & 48 & 58 \\ 4 & 5 & 4 & 3 \end{bmatrix}"),
        ("lim_N->inf 1 over N sum_n=1^N\nLEFT(SUM_k=1^n 1 over 2^k right)",
         r"\lim_{N\to\infty} \frac{1}{N} \sum_{n=1}^{N} \left( \sum_{k=1}^{n} \frac{1}{2^{k}} \right)"),
    ], ids=["fraction", "de-morgan", "cube-root", "matrix", "limit-and-sum"])
    def test_worked_examples(self, script, latex):
        result = equation_to_latex(script)

        assert result == latex

    @pytest.mark.parametrize("script, latex", [
        ("1 over 2", r"\frac{1}{2}"),
        ("x atop y", r"{x \atop y}"),
        ("sqrt 2", r"\sqrt{2}"),
        ("E=mc^2", "E=mc^{2}"),
        ("H_2 O", "H_{2} O"),
        ("int _1 ^2 {3x^2}dx", r"\int_{1}^{2} {3x^{2}}dx"),
        ("sum_{x=0} ^{inf}", r"\sum_{x=0}^{\infty}"),
        ("y= lim _{x -> 0} {{1} over {x}}", r"y= \lim_{x \to 0} {\frac{1}{x}}"),
        ("U=(A SMALLUNION B) SMALLINTER C", r"U=(A \cup B) \cap C"),
        ("not =", r"\not="),
        ("cases {2x+y=4 # 3x-4y=-1}", r"\begin{cases} 2x+y=4 \\ 3x-4y=-1 \end{cases}"),
        ("x LSUB y", "{}_{y}x"),
        ("{COLOR {255,0,255} {3}} over {4}", r"\frac{{3}}{4}"),
        ("A REL <-> {+2} {-5} B", r"A \overset{+2}{\underset{-5}{\leftrightarrow}} B"),
        ("n CHOOSE r", r"\binom{n}{r}"),
        ("acute A + dyad AB + under A", r"\acute{A} + \overleftrightarrow{AB} + \underline{A}"),
    ])
    def test_command_table_examples(self, script, latex):
        """Input examples of the basic command table in spec 1.2."""
        result = equation_to_latex(script)

        assert result == latex


class TestRules:
    def test_real_school_document_script(self):
        """Script from a real business document (an HWPX exam agreement form). It mixes the blank `` ` `` and spaced scripts."""
        script = "{ 1} over { n} sum _{i`=`1``} ^{n} (B _{i} `-A _{i} `)"

        result = equation_to_latex(script)

        assert result == r"\frac{1}{n} \sum_{i = 1}^{n} (B_{i} -A_{i} )"

    @pytest.mark.parametrize("script, latex", [
        ("rm kg cdot m/s ^{2}", r"kg \cdot m/s^{2}"),
        ("vec {a} cdot vec {b}", r"\vec{a} \cdot \vec{b}"),
        ("x= {-b +- sqrt {b ^{2} -4ac}} over {2a}", r"x= \frac{-b \pm \sqrt{b^{2} -4ac}}{2a}"),
        ("a+-b -+ c", r"a\pm b \mp c"),
        ("x <= 10, y>=20", r"x \leq 10, y\geq 20"),
    ], ids=["cdot-unit", "cdot-vector", "plus-minus", "attached-signs", "inequalities"])
    def test_dot_product_and_two_character_operators(self, script, latex):
        """The middle dot (`cdot`) and two-character operators (`+-` ±, `-+` ∓, `<=` ≤, `>=` ≥) came out as the original text."""
        result = equation_to_latex(script)

        assert result == latex

    def test_terms_are_split_by_spaces_not_by_words(self):
        """`over` takes the whole space-separated term. Taking it word by word made the numerator just `b`."""
        result = equation_to_latex("a+b over c")

        assert result == r"\frac{a+b}{c}"

    @pytest.mark.parametrize("script, latex", [
        ("x^{2}+y^{2}=r^{2}", "x^{2}+y^{2}=r^{2}"),
        ("int_{0}^{1}f(x)dx", r"\int_{0}^{1}f(x)dx"),
        ("H_{2}O", "H_{2}O"),
        ("log_{2}8", r"\log_{2}8"),
    ])
    def test_braced_script_ends_at_its_closing_brace(self, script, latex):
        """A script grouped with `{…}` ends at its closing brace. Reading up to the next space put the integrand into the upper limit
        and the following term into the exponent."""
        result = equation_to_latex(script)

        assert result == latex

    @pytest.mark.parametrize("script, latex", [
        ("x ^{2} over a ^{2} + y ^{2} over b ^{2} =1", r"\frac{x^{2}}{a^{2}} + \frac{y^{2}}{b^{2}} =1"),
        ("P _{1} V _{1} over T _{1}", r"P_{1} \frac{V_{1}}{T_{1}}"),
        ("x ^{2} atop y ^{2}", r"{x^{2} \atop y^{2}}"),
        ("{a over b} ^{2}", r"{\frac{a}{b}}^{2}"),
        ("{1} over {n} sum from {i=1} to n", r"\frac{1}{n} \sum_{i=1}^{n}"),
    ])
    def test_spaced_scripts_stay_on_the_fraction_side_they_follow(self, script, latex):
        """When a spaced script as Hancom saves it (`x ^{2}`) came after the denominator, it attached to the whole fraction, giving
        (x²/a)². The denominator, like the numerator, takes its scripts before the fraction is folded. A range (from/to) belongs
        to the next term (Σ), not to the denominator."""
        result = equation_to_latex(script)

        assert result == latex

    def test_command_argument_is_one_piece(self):
        """In `sqrt{x^2 +1}dx` the dx is outside the root, and a script after the argument attaches to the whole command
        (`sqrt{x}^2` is (√x)²)."""
        result = equation_to_latex("sqrt{x^2 +1}dx + sqrt{x}^2")

        assert result == r"\sqrt{x^{2} +1}dx + \sqrt{x}^{2}"

    def test_binom_takes_two_following_terms(self):
        """Spec: `[whole term] CHOOSE [chosen term]` or `BINOM [whole term] [chosen term]` — BINOM does not take the preceding
        term."""
        result = equation_to_latex("BINOM n r + n CHOOSE r")

        assert result == r"\binom{n}{r} + \binom{n}{r}"

    def test_identical_is_the_proportion_sign_not_equivalence(self):
        """Spec symbol table: IDENTICAL is ∷; `==` and EQUIV are ≡."""
        result = equation_to_latex("a IDENTICAL b == c")

        assert result == r"a \text{∷} b \equiv c"

    def test_visible_space_before_over_does_not_take_the_numerator(self):
        result = equation_to_latex("{a+b} ~over~ {c}")

        assert result == r"\frac{a+b}{c}"

    def test_root_index_is_folded_only_for_a_standalone_prescript(self):
        """Only `` `^3sqrt x `` is a cube root. A root following a superscript attached to another term must not be turned into a
        root index."""
        result = equation_to_latex("x_{} ^3 sqrt y")

        assert result == r"x_{}^{3} \sqrt{y}"

    def test_case_decides_greek_arrows_and_degree(self):
        """Commands are case-insensitive, but Greek letters, arrows and deg change meaning with case."""
        result = equation_to_latex("alpha Alpha omega Omega larrow LARROW deg DEG TIMES Times")

        assert result == r"\alpha A \omega \Omega \leftarrow \Leftarrow \deg ^{\circ} \times \times"

    def test_functions_are_read_even_when_glued(self):
        """Spec 1.1.3: `logx+sinx` is log x + sin x."""
        result = equation_to_latex("f(x)= logx+sinx")

        assert result == r"f(x)= \log x+\sin x"

    def test_korean_and_quoted_text_become_escaped_text(self):
        """Hangul words run together into one \\text, and LaTeX special characters in quoted text are escaped."""
        result = equation_to_latex('{해결 건수} over {전체 문의} times 100 "가격 $5 & 10%"')

        assert result == r"\frac{\text{해결 건수}}{\text{전체 문의}} \times 100 \text{가격 \$5 \& 10\%}"

    def test_font_commands_are_dropped(self):
        result = equation_to_latex("rm 2H_2 O = 2H_2 + O_2")

        assert result == "2H_{2} O = 2H_{2} + O_{2}"

    def test_line_breaks_and_alignment_make_an_aligned_block(self):
        result = equation_to_latex("x &= 1 # y &= 2")

        assert result == r"\begin{aligned} x & = 1 \\ y & = 2 \end{aligned}"


class TestFallback:
    """When conversion is impossible the result is None, and the caller leaves the original script as is. One equation must not fail
    the whole document."""

    @pytest.mark.parametrize("script", [
        "LONGDIV {6}{422}{2532#24#13#12#12#12#0}",  # long-division layout of the spec — not converted
        "{a over b",
        "a } b",
        "right )",
        "sqrt",
        "   ",
    ])
    def test_unconvertible_script_gives_none(self, script):
        assert equation_to_latex(script) is None

    def test_render_falls_back_to_the_original_script(self):
        rendered = render_equation("LONGDIV {6}{422}\n{2532}")

        assert rendered == " [Equation: LONGDIV {6}{422} {2532}] "

    def test_render_wraps_latex_in_dollars(self):
        assert render_equation("a over b") == r" $\frac{a}{b}$ "
        assert render_equation("") == ""

    @pytest.mark.parametrize("script", [
        "{" * 50_000 + "x" + "}" * 50_000,
        "sqrt " * 20_000 + "x",
        "not " * 20_000 + "x",
        "x^" * 20_000 + "y",
        "a over " * 20_000 + "b",
        "\\x" * 4_999,
        "\\" * 9_999,
        "$" * 9_999,
    ], ids=["nested-braces", "nested-sqrt", "nested-not", "script-chain", "over-chain", "glued-commands", "backslashes",
            "dollars"])
    @pytest.mark.timing
    def test_hostile_scripts_finish_quickly(self, script):
        """Deep nesting and long chains in a crafted file neither blow the recursion nor take time (length/depth caps)."""
        started = time.perf_counter()

        equation_to_latex(script)
        render_equation(script)

        assert time.perf_counter() - started < 1.0
