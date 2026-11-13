"""Text placed character by character without spaces (Hangul set by a layout program): the spaces are found again from
the advance of each character against the widths of its font.
"""

import statistics

from opendockit.extractors._pdf_chars import _is_hangul
from opendockit.extractors._pdf_pieces import _FontStats


# Repairing separately placed runs of characters (_repair_placed_runs). Some text in PDFs exported by PowerPoint places the text
# layer separately for each word (each time the font changes) and inserts no space characters. The Hangul width the fallback font
# states (1em) is wider than the real advance width, so the next run starts before the end of the previous run. As a result
# (1) pdfminer, pdfplumber and pdfium, which all find spaces by gaps, read the words run together ("자동차공장냉매주입"), and
# (2) sorting by x coordinate scrambled the character order ("연구원(KIER)" → "연구(K원IER)"). 2 real documents, a dozen pages.
#
# Run = characters placed in one go, where the end of one character and the start of the next are exactly equal. A run's start
# position is exact.
# A page is repaired only when it has at least one font confirmed as "separately placed text". The confirmation conditions are in
# _placement_fit: per font and size there must be enough Hangul, almost no space characters and several overlapping boundaries, and
# the real character width (clearly narrower than the width the font states) and the space width must come out consistently from
# the distances by run length. A document whose character spacing differs per line because of justification (a PDF exported from
# Hangul (HWP)) and a document with tightened character spacing break at every character and do not meet these conditions. With
# looser conditions a space got inserted after every character of a government press release PDF ("새 만 금 기 본 계 획") — do
# not revert.
# - Overlap repair: when the next run starts before the end of the previous run, the character box of the previous run is shrunk
#   to the next start point.
# - Space recovery: before a new run that follows a run ending in Hangul, a space is inserted when the distance left over, computed
#   with the real character width, exceeds half the space width.
# Pinned by: test_quality.py::test_pdf_words_placed_separately_without_space_characters_get_their_spaces_back,
#       test_pdf_with_tight_letter_spacing_does_not_get_extra_spaces, test_pdf_overlapping_runs_keep_reading_order,
#       test_pdf_justified_text_with_varying_letter_spacing_is_left_alone.
# In a two-character word the overlap and the word gap almost cancel out, so the break is just over 2% of the character size
# (measured: -0.025em). For characters placed in one go it is 0.
_RESTART_GAP_RATIO = 0.005
_SPACE_POOR_RATIO = 0.2
_MIN_HANGUL_FOR_REPAIR = 15
_MIN_OVERLAPPING_RESTARTS = 3
# Upper and lower bound on the real character width accepted as separately placed text (relative to the 1em width the font states),
# and the tolerance of the distance fit (em).
# Measured: width 0.865em, space 0.24em. A width close to 1em is a normal document without overlap. The lower bound prevents a
# bogus fit: in a PowerPoint PDF that splits words into two-character + one-character runs (character width as the font states,
# only the word gaps widened by 0.3em), run length and boundary kind (after one character a word gap, after two characters inside a
# word) interlock so that 'width 0.67em, space 0.6em' fits, and a space was inserted at every boundary ('상태 에 저장 된',
# Pinned by: test_pdf_round48.py::TestWordsSplitIntoTwoPiecesAtFullWidth). Hangul advancing by less than 0.8 × the font width
# visibly overlaps its neighbours.
_MAX_PLACED_ADVANCE = 0.95
_MIN_PLACED_ADVANCE = 0.8
_FIT_TOLERANCE = 0.08


def _repair_placed_runs(page: object, known_fit: tuple[float, float] | None = None,
                        ) -> tuple[tuple[float, float] | None, bool]:
    """Fix the overlapping character boxes of separately placed runs of characters, and insert space characters at word boundaries
    for fonts that have no space character.

    `page.chars` is modified in place (table extraction uses the same character list). Inserted spaces are marked `restored`.
    The criteria and reasons are in the `_RESTART_GAP_RATIO` comment above. `known_fit` is the value obtained from another page of
    the same document.
    Returns (the value obtained from this page, whether there is a font that looks like such text but could not be fixed for lack of
    a value).
    """
    chars = page.chars
    runs = _placed_runs(chars)
    pairs = [(run, following) for run, following in zip(runs, runs[1:], strict=False)
             if _same_row(run[-1], following[0]) and following[0]["x0"] > run[0]["x0"]]

    fonts: dict[tuple[str, float], _FontStats] = {}
    for run in runs:
        stats = fonts.setdefault(_font_key(run[-1]), _FontStats())
        stats.hangul += sum(1 for char in run if _is_hangul(char["text"]))
        stats.spaces += sum(1 for char in run if char["text"] == " ")
        stats.continuous += sum(1 for previous, char in zip(run, run[1:], strict=False)
                                if _is_hangul(previous["text"]) and _is_hangul(char["text"]))
    for run, following in pairs:
        last, first = run[-1]["text"], following[0]["text"]
        if _is_hangul(last) and (_is_hangul(first) or first.isascii() and first.isalnum()):
            stats = fonts[_font_key(run[-1])]
            stats.restarts.append((run, following[0]))
            overlaps = following[0]["x0"] < run[-1]["x1"]
            stats.overlapping += overlaps
            stats.overlapping_after_word += overlaps and len(run) >= 2

    # Whether this page is "separately placed text" is decided by whether at least one font yields a real character width
    # (_placement_fit). Normal documents whose character spacing differs per line because of justification are filtered out here and
    # nothing changes.
    fits = {key: _placement_fit(stats.restarts) for key, stats in fonts.items()
            if stats.hangul >= _MIN_HANGUL_FOR_REPAIR and stats.overlapping >= _MIN_OVERLAPPING_RESTARTS
            and _looks_placed_without_spaces(stats)}
    fits = {key: fit for key, fit in fits.items() if fit is not None}
    page_fit = ((statistics.median(fit[0] for fit in fits.values()), statistics.median(fit[1] for fit in fits.values()))
                if fits else None)
    # Fonts whose character width could not be obtained on their own: few characters, as in a heading, or two screen fonts (regular
    # and bold) sharing one font name in the text layer so that the widths are mixed (Measured: 0.86em and 0.80em on the same page).
    # They are fixed only when this page or another page of the same document has been confirmed as 'separately placed text'; a value
    # that fits is borrowed, and when there is none every new run after Hangul is treated as a word boundary (a space may end up
    # inside a word where only the font changed, but that is better than whole words running together).
    candidates = [*fits.values(), *([known_fit] if known_fit else [])]
    borrowers = [key for key, stats in fonts.items()
                 if key not in fits and stats.overlapping_after_word and _looks_placed_without_spaces(stats)]
    if not candidates:
        return None, bool(borrowers)

    gap_before: dict[int, dict] = {}
    for key, stats in fonts.items():
        if key not in fits and key not in borrowers:
            continue
        fit = fits.get(key) or next((candidate for candidate in candidates if _fits(stats.restarts, candidate)), None)
        gap_before.update((id(char), run[-1]) for run, char in stats.restarts
                          if fit is None or _is_word_gap(run, char, fit))

    # Word-boundary detection uses only the start points of the runs, so it was finished above. Now shrink the overlapping character
    # boxes to the start point of the next run.
    for run, following in pairs:
        start, end, limit = run[0]["x0"], run[-1]["x1"], following[0]["x0"]
        if limit >= end or limit - start < (end - start) * 0.5:
            continue
        scale = (limit - start) / (end - start)
        for char in run:
            char["x0"], char["x1"] = start + (char["x0"] - start) * scale, start + (char["x1"] - start) * scale
            char["width"] = char["x1"] - char["x0"]

    if not gap_before:
        return page_fit, False
    # The space goes right before the start point of the next word (both in content order and in coordinates).
    rebuilt = []
    for char in chars:
        previous = gap_before.get(id(char))
        if previous is not None:
            rebuilt.append({**previous, "text": " ", "x0": char["x0"] - 0.01, "x1": char["x0"], "width": 0.01,
                            "restored": True})
        rebuilt.append(char)
    chars[:] = rebuilt
    return page_fit, False


def _looks_placed_without_spaces(stats: _FontStats) -> bool:
    """There are almost no space characters, and at least half of the overlapping boundaries follow a run of two or more characters.

    The second condition separates justified documents: a line with tightened character spacing breaks at every character, so all
    overlapping boundaries follow single-character runs.
    """
    return (stats.spaces < stats.hangul * _SPACE_POOR_RATIO and stats.continuous * 2 >= stats.overlapping
            and stats.overlapping_after_word * 2 >= stats.overlapping)


def _placed_runs(chars: list[dict]) -> list[list[dict]]:
    """In content order, group characters on the same line and in the same font whose start continues exactly from the end of the
    previous character into one run."""
    runs: list[list[dict]] = []
    for char in chars:
        previous = runs[-1][-1] if runs else None
        if (previous is not None and _font_key(previous) == _font_key(char) and _same_row(previous, char)
                and abs(char["x0"] - previous["x1"]) <= previous["size"] * _RESTART_GAP_RATIO):
            runs[-1].append(char)
        else:
            runs.append([char])
    return runs


def _same_row(previous: dict, char: dict) -> bool:
    center = (char["top"] + char["bottom"]) / 2
    return abs(center - (previous["top"] + previous["bottom"]) / 2) <= (previous["bottom"] - previous["top"]) * 0.4


def _font_key(char: dict) -> tuple[str, float]:
    return char["fontname"], round(char["size"], 1)


def _measure(run: list[dict], char: dict) -> tuple[int, float] | None:
    """For a run made of Hangul only: (character count, distance from the run's start point to the next run's start point ÷ character
    size)."""
    if not all(_is_hangul(item["text"]) for item in run):
        return None
    return len(run), (char["x0"] - run[0]["x0"]) / run[0]["size"]


def _placement_fit(restarts: list[tuple[list[dict], dict]]) -> tuple[float, float] | None:
    """Return (real character width, space width) in units of the character size if this is separately placed text, otherwise None.

    The distance from a run's start point to the next run's start point is `character count × real character width (+ space width)`.
    The two values are derived from two medians of the distance per character count (most boundaries are between words). All of the
    following must hold for the text to be accepted as separately placed.
    - There are at least two kinds of runs with different character counts, at least 3 of each. In a justified document every
      character is broken off, so all runs are single characters.
    - The real character width is clearly narrower than the width the font declares (the cause of the overlap). Normal documents
      whose widths are right are caught here.
    - Most boundaries fit either "a space width is left over" or "nothing is left over", and the majority of them are between words.
    """
    measured = [item for item in (_measure(run, char) for run, char in restarts) if item]
    by_length: dict[int, list[float]] = {}
    for length, distance in measured:
        by_length.setdefault(length, []).append(distance)
    lengths = sorted((length for length, values in by_length.items() if len(values) >= 3),
                     key=lambda length: -len(by_length[length]))[:2]
    if len(lengths) < 2:
        return None

    short, long = sorted(lengths)
    advance = (statistics.median(by_length[long]) - statistics.median(by_length[short])) / (long - short)
    space = statistics.median(by_length[short]) - short * advance
    if not (_MIN_PLACED_ADVANCE <= advance <= _MAX_PLACED_ADVANCE and 0.1 <= space <= 0.7):
        return None

    return (advance, space) if _fits(restarts, (advance, space)) else None


def _fits(restarts: list[tuple[list[dict], dict]], fit: tuple[float, float]) -> bool:
    """Whether most boundaries fit either "a space width is left over" or "nothing is left over", and the majority of them are
    between words."""
    advance, space = fit
    leftovers = [item[1] - item[0] * advance for item in (_measure(run, char) for run, char in restarts) if item]
    if not leftovers:
        return False
    word_gaps = sum(1 for leftover in leftovers if abs(leftover - space) <= _FIT_TOLERANCE)
    inside_word = sum(1 for leftover in leftovers if abs(leftover) <= _FIT_TOLERANCE)
    return word_gaps + inside_word >= len(leftovers) * 0.8 and word_gaps >= len(leftovers) * 0.5


def _is_word_gap(run: list[dict], char: dict, fit: tuple[float, float]) -> bool:
    """Whether the restart is between words (a space) or inside a word where only the font changed ("개발" in bold + "하여"). A run
    that is not Hangul-only is treated as between words."""
    measured = _measure(run, char)
    advance, space = fit
    return measured is None or measured[1] - measured[0] * advance > space / 2
