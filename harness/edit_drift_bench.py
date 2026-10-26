"""The edit-drift benchmark: does the fuzzy cascade match in the RIGHT PLACE?

**Why this module exists.** Wave 1 shipped a fuzzy edit cascade whose entire
value proposition is *"it matches when exact matching fails."* That claim is
worthless — and dangerous — without a benchmark, because there are two failure
directions and only one of them is fun:

* **under-match** (fails to match) — a failed edit. Annoying, visible, SAFE.
* **wrong-match** (matches the wrong site) — **corruption**. Invisible,
  DANGEROUS. The edit lands, the run reports success, the tests pass because
  the change was harmless, and the actual bug is still there.

**So the gate in this module is ``wrong_matched == 0``, not a match rate.**
A cascade that is excellent on nine classes and wrong on the tenth is a decision
the user must be able to *see*, which is why every result is published PER
CLASS (:func:`format_table`) and why no aggregate success number exists anywhere
in this file.

**The oracle is a round trip, and it does not consult the matcher.** Each case
declares the exact file content a CORRECT edit must produce
(:attr:`DriftCase.intended_post`), computed by string surgery on the drifted
input. The benchmark then inverts the drift on whatever the matcher produced and
asks whether it equals the intent. A matcher that "helpfully" found a 0.7
similar block somewhere else fails that equality — which is the definition of
corruption — without any threshold, heuristic or opinion from this file.

**Held out, twice over.** Two corpora, and the second one is the point:

1. :func:`synthetic_corpus` — the ten drift CLASSES, built from the failure
   modes, on fixtures small enough to reason about exhaustively.
2. :func:`held_out_corpus` — the same ten classes applied to **real files in
   this repository**, with the target block located by ``ast`` and never
   hand-picked. This is "code you did not design the cases for": the drift is
   ours, the code is the project's.

A benchmark written to match its own implementation proves nothing, so the
synthetic corpus is treated as the *development* surface and the held-out
corpus is the one the gate is asserted on. Both are reported.

**Where the interesting refusals are.** Classes 8 (a similarly-named function
in another file) and 10 (a minified/generated file) are GUARD cases: the
brief's claim is that the disproportionate-match guard must catch them, and
:class:`DriftCase.guard_expect` names which rule is supposed to fire so a
refusal for the wrong reason is distinguishable from a refusal for the right
one. A refusal that arrives via an accidental threshold is still a refusal, and
still safe — but it is a different fact about the system and is reported as one.

**Assumes:** `text` is decoded file content. Never raises: a benchmark that
crashes instead of reporting a class is a benchmark whose coverage is unknown.
"""

from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DRIFT_CLASSES",
    "OUTCOMES",
    "BenchmarkReport",
    "CaseResult",
    "ClassResult",
    "DriftCase",
    "Transform",
    "format_table",
    "held_out_corpus",
    "run_benchmark",
    "synthetic_corpus",
]

#: The ten drift classes, in the brief's order. The tuple is asserted by the
#: test module to be exactly these ten, so a class cannot be quietly dropped.
DRIFT_CLASSES: Tuple[str, ...] = (
    "trailing_whitespace",       # 1
    "indentation_tabs",          # 2
    "crlf",                      # 3
    "unicode_nfd",               # 4
    "smart_quotes",              # 5
    "block_reorder",             # 6
    "duplicate_target",          # 7
    "similar_name_other_file",   # 8  (guard)
    "very_long_line",            # 9
    "minified_generated",        # 10 (guard)
)

#: Classes the benchmark DISCOVERED, beyond the brief's ten. Kept as a separate
#: tuple so the brief's contract stays exactly ten and auditable, and so an
#: eleventh class can never be mistaken for one that was asked for. Both entries
#: were found by this benchmark; see :func:`synthetic_corpus`.
#:
#: * ``block_tail_blank_lines`` -- a line-count region running past the block.
#: * ``absent_target`` -- **the caller asked for a symbol that does not exist**,
#:   and the ladder matched something anyway. This is the most important class
#:   in the file: a hallucinated function name is an ordinary thing for a model
#:   to do, and before ``harness.fuzzy_edit``'s weak-anchor guard it produced a
#:   SUCCESSFUL, corrupting write. See ``SCORED_MATCH_STAGES``.
EXTRA_CLASSES: Tuple[str, ...] = ("block_tail_blank_lines", "absent_target")

#: The three outcomes. Deliberately NOT four: "refused" and "under-matched" are
#: the same event, and a benchmark that splits one event into two labels
#: invites a reader to average across them.
OUTCOMES: Tuple[str, ...] = ("matched", "wrong_matched", "refused")


# ---------------------------------------------------------------------------
# Drift transforms. Each is COMPOSABLE (T(a+b) == T(a)+T(b)) and INVERTIBLE,
# and both properties are ASSERTED at corpus-build time. Composability is what
# lets the benchmark compute a drifted block's exact offsets by transforming
# the prefix and the block separately; invertibility is what lets it decide
# whether a post-edit file is the one that was intended.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transform:
    """A named, composable, invertible drift.

    ``forward`` is the drift (what happened to the file behind the model's
    back). ``inverse`` undoes it so a post-edit file can be compared against
    the intent in the file's own terms.
    """

    name: str
    forward: Callable[[str], str]
    inverse: Callable[[str], str]

    def composable(self) -> bool:
        """Return whether ``T(a+b) == T(a)+T(b)`` on a two-part probe.

        Checked rather than assumed because the whole offset oracle rests on
        it, and a non-compositional transform would silently produce a
        plausible-looking wrong answer for every case that uses it.
        """
        for a, b in (("alpha\n", "beta\n"), ("", "solo\n"), ("x = 1\n", "")):
            if self.forward(a + b) != self.forward(a) + self.forward(b):
                return False
        return True

    def invertible(self) -> bool:
        """Return whether ``inverse(forward(t)) == t`` on a probe."""
        probe = "def f():\n    return 'x'\n"
        return self.inverse(self.forward(probe)) == probe


def _split_terminator(line: str) -> Tuple[str, str]:
    """Split one line into ``(body, terminator)``."""
    stripped = line.rstrip("\r\n")
    return stripped, line[len(stripped) :]


def _drift_trailing_whitespace(text: str) -> str:
    """Append two spaces to every non-blank line."""
    out = []
    for line in text.splitlines(keepends=True):
        body, term = _split_terminator(line)
        out.append((body + "  " + term) if body.strip() else line)
    return "".join(out)


def _un_drift_trailing_whitespace(text: str) -> str:
    """Remove exactly the two spaces the forward pass appended.

    Deliberately "remove two", not "strip all". An inverse that stripped every
    trailing space would be *more* aggressive than its own forward and would
    silently eat whitespace it never added — which would make a wrong match
    compare EQUAL to the intent and hide the very corruption this benchmark
    measures.
    """
    out = []
    for line in text.splitlines(keepends=True):
        body, term = _split_terminator(line)
        out.append((body[:-2] + term) if body.strip() and body.endswith("  ") else line)
    return "".join(out)


def _drift_indentation_tabs(text: str) -> str:
    """Replace each leading group of four spaces with one tab."""
    out = []
    for line in text.splitlines(keepends=True):
        body, term = _split_terminator(line)
        stripped = body.lstrip(" ")
        indent = body[: len(body) - len(stripped)]
        out.append(indent.replace("    ", "\t") + stripped + term)
    return "".join(out)


def _un_drift_indentation_tabs(text: str) -> str:
    """Replace each leading tab with four spaces."""
    out = []
    for line in text.splitlines(keepends=True):
        body, term = _split_terminator(line)
        stripped = body.lstrip("\t")
        indent = body[: len(body) - len(stripped)]
        out.append(indent.replace("\t", "    ") + stripped + term)
    return "".join(out)


def _drift_crlf(text: str) -> str:
    return _un_drift_crlf(text).replace("\n", "\r\n")


def _un_drift_crlf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _drift_nfd(text: str) -> str:
    """Decompose to NFD. Only meaningful on text that is already NFC."""
    return unicodedata.normalize("NFD", text)


def _un_drift_nfd(text: str) -> str:
    return unicodedata.normalize("NFC", text)


#: Typographic punctuation -> ASCII, used by :func:`_prepare_fixture` to fold a
#: fixture so it can never carry a smart quote its own forward pass did not
#: introduce.
#:
#: This mapping was originally written the other way round and keyed by STRING,
#: and both mistakes were silent. ``str.translate`` looks keys up by ORDINAL, so
#: a string-keyed table is a no-op rather than an error; and having the drift
#: and its inverse point the same way made the smart-quote class VACUOUS -- the
#: fixture had ASCII quotes, the forward pass rewrote them *to ASCII* (a no-op),
#: the case still matched ``exact``, and class 5 reported a pass while
#: measuring nothing. Caught by
#: `test_the_fixture_preparer_actually_removes_what_the_inverses_need` plus
#: :func:`_assert_drift_is_meaningful`. Both maps are now written out longhand
#: rather than derived, so the direction is visible at the definition.
_SMART_TO_ASCII: Dict[int, str] = {
    0x201C: '"', 0x201D: '"', 0x2018: "'", 0x2019: "'",
}
#: ASCII -> typographic. This is the DRIFT the class is named for.
_ASCII_TO_SMART: Dict[int, int] = {0x22: 0x201C, 0x27: 0x2018}


def _drift_smart_quotes(text: str) -> str:
    return text.translate(_ASCII_TO_SMART)


def _un_drift_smart_quotes(text: str) -> str:
    return text.translate(_SMART_TO_ASCII)


def _prepare_fixture(text: str) -> str:
    """Put a source into the exact form every transform's inverse requires.

    **This is not cosmetic — it is what makes the oracle sound.** Each inverse
    is exact only against a fixture in a known shape, and a fixture that is not
    in that shape produces a round-trip comparison that can be EQUAL for the
    wrong reason (a wrong match whose damage the inverse happens to undo). Four
    normalisations, each undoing a pre-existing condition rather than a drift:

    * **trailing whitespace stripped** — the inverse removes exactly two
      trailing spaces, so pre-existing trailing whitespace would survive the
      forward pass and break the round trip.
    * **CRLF normalised to LF** — the forward pass collapses CRLF first, so a
      CRLF fixture would not come back as itself.
    * **tabs expanded to four spaces** — the tab transform rewrites leading
      whitespace, so a literal tab in the fixture would be converted by the
      *inverse* and never restored.
    * **smart quotes folded to ASCII, NFC** — likewise for the quote and
      composition transforms.

    Applied to the fixture, never to the drift's output, so it cannot mask a
    wrong match: it changes what the ladder sees, not what the grader compares.
    """
    text = _un_drift_smart_quotes(text)
    text = _un_drift_crlf(text)
    text = text.expandtabs(4)
    text = "".join(
        (body.rstrip(" ") + term) if body.strip() else line
        for line, (body, term) in (
            (line, _split_terminator(line)) for line in text.splitlines(keepends=True)
        )
    )
    return unicodedata.normalize("NFC", text)


def _drift_is_meaningful(name: str, base: str, drifted: str) -> bool:
    """Return whether the drift actually changed the file.

    **Written because a drift silently did nothing and the class passed anyway.**
    The smart-quote transform's inverse was a ``str.translate`` with string keys,
    which Python ignores (it looks up by ordinal), so the fixture carried ASCII
    quotes, the forward pass rewrote them, the case still matched ``exact``, and
    class 5 measured nothing at all while reporting a pass. A drift class whose
    drift is a no-op on its own fixture tests nothing, so this is CHECKED rather
    than assumed, and a case that fails it is SKIPPED rather than counted.

    **A skip is a fact about the corpus, not a silent gap.** A real source file
    with no non-ASCII text in its selected block cannot express the NFD drift at
    all -- there is nothing to decompose -- and forcing it to produce a case
    would mean inventing text the file does not contain. The count is published
    on the report so a reader can see that class 4 ran on 4 of 5 modules rather
    than being quietly absent.
    """
    del name
    return drifted != base


TRANSFORMS: Dict[str, Transform] = {
    "trailing_whitespace": Transform(
        "trailing_whitespace", _drift_trailing_whitespace, _un_drift_trailing_whitespace
    ),
    "indentation_tabs": Transform(
        "indentation_tabs", _drift_indentation_tabs, _un_drift_indentation_tabs
    ),
    "crlf": Transform("crlf", _drift_crlf, _un_drift_crlf),
    "unicode_nfd": Transform("unicode_nfd", _drift_nfd, _un_drift_nfd),
    "smart_quotes": Transform("smart_quotes", _drift_smart_quotes, _un_drift_smart_quotes),
}


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftCase:
    """One drift case: a known-correct answer the matcher can be graded against.

    The scenario modelled throughout is the REAL one, not a convenient one: the
    model read the file *before* the drift, so ``old`` is the block as the
    model remembers it, and the bytes on disk are the DRIFTED file. That is why
    exact matching fails, and it is why the correct answer is still
    unambiguous.
    """

    name: str
    drift_class: str
    description: str
    #: the bytes on disk, already drifted
    text: str
    #: ``old_string`` as the model has it in its prompt (PRE-drift)
    old: str
    #: ``new_string`` the model wants written
    new: str
    #: ``"apply"`` or ``"refuse"`` — what a correct system does
    expect: str
    #: the exact text a CORRECT apply yields; ``None`` for a refusal case
    intended_post: Optional[str] = None
    #: the disproportionate-guard rule expected to fire, for guard classes
    guard_expect: str = ""
    #: extra files present, for the multi-file class
    sibling_files: Mapping[str, str] = field(default_factory=dict)
    #: the transform to undo before comparing (identity when there is none)
    inverse: Optional[Callable[[str], str]] = None

    def normalise(self, post: str) -> str:
        """Undo the drift so two files can be compared in the same terms."""
        return self.inverse(post) if self.inverse else post


@dataclass(frozen=True)
class CaseResult:
    """One case's grade."""

    name: str
    drift_class: str
    outcome: str
    stage: str
    confidence: float
    guard: str
    reason: str
    expected: str
    #: set when ``outcome == "wrong_matched"``
    detail: str = ""

    @property
    def is_wrong(self) -> bool:
        return self.outcome == "wrong_matched"

    @property
    def matched_as_expected(self) -> bool:
        """Whether this case did what its class says a correct system does.

        Two vocabularies meet here and conflating them is a bug this property
        exists to prevent: ``expected`` is the class's INTENT (``apply`` /
        ``refuse``) while ``outcome`` is what happened (``matched`` /
        ``wrong_matched`` / ``refused``). A ``refuse`` class is satisfied by
        ``refused``; an ``apply`` class is satisfied only by ``matched`` --
        ``wrong_matched`` never satisfies it, which is the whole gate.
        """
        want = "matched" if self.expected == "apply" else "refused"
        return self.outcome == want


@dataclass
class ClassResult:
    """Every case of one drift class, and the per-class verdict.

    Published separately because the brief's central warning is that a cascade
    excellent on nine classes and catastrophic on the tenth is a DECISION the
    user must be able to make — and a decision needs the tenth.
    """

    drift_class: str
    cases: List[CaseResult] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return sum(1 for c in self.cases if c.outcome == "matched")

    @property
    def wrong_matched(self) -> int:
        return sum(1 for c in self.cases if c.is_wrong)

    @property
    def refused(self) -> int:
        return sum(1 for c in self.cases if c.outcome == "refused")

    @property
    def expected(self) -> str:
        """The single expectation shared by the class, or ``"mixed"``."""
        seen = {c.expected for c in self.cases}
        return seen.pop() if len(seen) == 1 else "mixed"

    def guard_held(self) -> bool:
        """Whether every guard case was caught by the guard it was asked for.

        ``True`` vacuously for a class with no guard expectation, which is why
        :meth:`guard_cases` exists separately — "no guard was expected" and
        "the guard held" must never render as the same word.
        """
        return all(
            (not c.expected.startswith("refuse"))
            or c.outcome == "refused"
            for c in self.guard_cases()
        )

    def guard_cases(self) -> List[CaseResult]:
        return [c for c in self.cases if c.expected == "refuse"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drift_class": self.drift_class,
            "cases": len(self.cases),
            "matched": self.matched,
            "wrong_matched": self.wrong_matched,
            "refused": self.refused,
            "expected": self.expected,
            "guard_cases": len(self.guard_cases()),
            "guard_held": self.guard_held(),
        }


@dataclass
class BenchmarkReport:
    """The whole run, per class and in total."""

    name: str
    classes: List[ClassResult] = field(default_factory=list)

    @property
    def cases(self) -> List[CaseResult]:
        return [c for cls in self.classes for c in cls.cases]

    @property
    def wrong_matched(self) -> int:
        return sum(1 for c in self.cases if c.is_wrong)

    @property
    def matched(self) -> int:
        return sum(1 for c in self.cases if c.outcome == "matched")

    @property
    def refused(self) -> int:
        return sum(1 for c in self.cases if c.outcome == "refused")

    @property
    def unexpected(self) -> List[CaseResult]:
        """Cases that did NOT do what the class says a correct system does.

        An ``apply`` class that refused is an under-match (safe, disappointing);
        a ``refuse`` class that matched is a CORRUPTION. Both are listed here
        and neither is averaged into a score.
        """
        return [c for c in self.cases if not c.matched_as_expected]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corpus": self.name,
            "total_cases": len(self.cases),
            "matched": self.matched,
            "wrong_matched": self.wrong_matched,
            "refused": self.refused,
            "classes": [c.to_dict() for c in self.classes],
            "unexpected": [
                {"name": c.name, "expected": c.expected, "outcome": c.outcome,
                 "guard": c.guard, "reason": c.reason[:200]}
                for c in self.unexpected
            ],
        }


def format_table(report: BenchmarkReport) -> str:
    """Render the per-class table. No aggregate success column exists.

    The absence is the point: an aggregate match rate is exactly the number
    that lets a wrong-match hide behind nine good classes.
    """
    head = (
        f"{'class':<24} {'exp':<7} {'n':>2} {'matched':>7} {'WRONG':>5} "
        f"{'refused':>7} {'guard':>6}"
    )
    lines = [f"edit-drift benchmark - {report.name}", head, "-" * len(head)]
    for cls in report.classes:
        lines.append(
            f"{cls.drift_class:<24} {cls.expected:<7} {len(cls.cases):>2} "
            f"{cls.matched:>7} {cls.wrong_matched:>5} {cls.refused:>7} "
            f"{('held' if cls.guard_cases() else '-'):>6}"
        )
    lines.append("-" * len(head))
    lines.append(
        f"{'TOTAL':<24} {'':>7} {len(report.cases):>2} {report.matched:>7} "
        f"{report.wrong_matched:>5} {report.refused:>7}"
    )
    lines.append(
        f"GATE wrong_matched == 0 : {'PASS' if report.wrong_matched == 0 else 'FAIL'}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade(case: DriftCase, config: Optional[Mapping[str, Any]] = None) -> CaseResult:
    """Run one case through the ladder and grade the outcome.

    **The grading never asks the matcher whether it was right.** It asks
    whether the file the edit would produce is the file that was intended, once
    both are expressed in the same convention. A matcher that returns a
    confident, guard-approved, wrong offset scores exactly the same as one that
    returns nothing — and the difference between those two is the whole reason
    this benchmark exists.

    The splice performed here is byte-for-byte the one `harness.editor
    .apply_text_edit` performs (``chosen = (before[start:end], replacement)``
    then a single splice), so grading at this level measures what would be
    written, not what the ladder hoped.
    """
    from harness import fuzzy_edit

    try:
        text = case.text
        match, reason = fuzzy_edit.resolve_fuzzy(text, case.old, case.new, config=config)
    except Exception as exc:  # pragma: no cover - the ladder is total by design
        return CaseResult(
            case.name, case.drift_class, "refused", "", 0.0, "",
            f"the ladder raised ({exc!r}); treated as no match", case.expect,
        )

    if match is None:
        return CaseResult(
            case.name, case.drift_class, "refused", "", 0.0, "",
            reason or "no match", case.expect,
        )

    produced = text[: match.start] + match.replacement + text[match.end :]

    if case.expect == "refuse":
        # It matched at all. Compare against intent to say WHERE it went, so
        # the report names a location rather than just "it should not have".
        detail = ""
        if case.intended_post is not None and produced != case.intended_post:
            detail = _first_difference(case.normalise(produced), case.intended_post)
        else:
            detail = (
                "produced a match where the class requires a refusal "
                f"(stage={match.stage}, guard={match.guard or 'none'})"
            )
        return CaseResult(
            case.name, case.drift_class, "wrong_matched", match.stage,
            match.confidence, match.guard, reason, case.expect, detail,
        )

    if case.intended_post is None:  # pragma: no cover - malformed case
        return CaseResult(
            case.name, case.drift_class, "refused", match.stage, match.confidence,
            match.guard, "an 'apply' case declared no intended_post", case.expect,
        )

    if produced == case.intended_post:
        return CaseResult(
            case.name, case.drift_class, "matched", match.stage, match.confidence,
            match.guard, reason, case.expect,
        )

    # A match WAS produced and the file is not the intended one. That is
    # corruption, and it is graded as corruption regardless of how the offsets
    # compare — a "close but not equal" match is still a file nobody asked for.
    detail = _first_difference(case.normalise(produced), case.intended_post)
    return CaseResult(
        case.name, case.drift_class, "wrong_matched", match.stage,
        match.confidence, match.guard, reason, case.expect, detail,
    )


def _first_difference(produced: str, intended: str) -> str:
    """Describe where two files first diverge, in a form a reader can act on."""
    limit = min(len(produced), len(intended))
    at = limit
    for i in range(limit):
        if produced[i] != intended[i]:
            at = i
            break
    lo = max(0, at - 40)
    return (
        f"first divergence at char {at} (len produced={len(produced)}, "
        f"len intended={len(intended)}); "
        f"produced={produced[lo:at + 40]!r} intended={intended[lo:at + 40]!r}"
    )


def run_benchmark(
    cases: Sequence[DriftCase],
    *,
    name: str = "corpus",
    config: Optional[Mapping[str, Any]] = None,
) -> BenchmarkReport:
    """Grade every case and return the per-class report.

    Never raises on a bad case — a case that cannot be graded is reported as a
    refusal, because a benchmark that stops at the first surprise has an
    unknown denominator and an unknown coverage.
    """
    by_class: Dict[str, ClassResult] = {}
    for case in cases:
        by_class.setdefault(case.drift_class, ClassResult(case.drift_class)).cases.append(
            grade(case, config=config)
        )
    ordered_names = DRIFT_CLASSES + EXTRA_CLASSES
    ordered = [
        by_class[n] for n in ordered_names if n in by_class
    ] + [c for k, c in by_class.items() if k not in ordered_names]
    return BenchmarkReport(name=name, classes=ordered)


# ---------------------------------------------------------------------------
# Corpus 1 - synthetic, one small fixture per drift class
# ---------------------------------------------------------------------------

_SYNTHETIC_SOURCE = _prepare_fixture('''\
"""Fixture module for the edit-drift benchmark."""

from __future__ import annotations


def compute_totals(entries):
    """Sum the entries, labelled "running total" — café style."""
    running = 0
    breakdown = []
    for entry in entries:
        running += entry.amount
        breakdown.append(entry.label)
    return running, breakdown


def unrelated_helper(alpha, beta):
    """A second function with a totally different body."""
    return alpha + beta


def another_helper(alpha, beta, gamma):
    """A third function, so the target is a realistic fraction of the file."""
    total = 0
    for value in (alpha, beta, gamma):
        if value is None:
            continue
        total += abs(value)
    return total


def trailing_return(value):
    return value


def yet_another_helper(values):
    """A fourth function; keeps the fixture's line count honest."""
    return sorted(values, reverse=True)
''')

#: The block every synthetic case targets: a real function with real context.
#: Its extent ends at a CANONICAL boundary -- the last statement's own newline,
#: NOT the blank lines that follow it. That is deliberate and it took a
#: measurement to see why: an ``old_string`` that ends in blank lines makes the
#: ladder count those blanks as content and return a region past the block, so a
#: fixture built that way measures ONE defect five times over and hides it
#: behind four unrelated drift classes. The trailing-blank-line shape is real
#: and is its own class below (``block_tail_blank_lines``).
_TARGET = "def compute_totals(entries):"
_TARGET_END = "\n\n\ndef unrelated_helper"


def _synthetic_post(text: str, start: int, end: int, new: str) -> str:
    return text[:start] + new + text[end:]


def _drifted_case(
    name: str,
    drift_class: str,
    description: str,
    transform: Optional[Transform],
    *,
    expect: str,
    new: str = "    running = 0\n    breakdown = []\n    return running, breakdown\n",
    block: Optional[Tuple[int, int]] = None,
    guard_expect: str = "",
    text: Optional[str] = None,
    old: Optional[str] = None,
    sibling_files: Optional[Mapping[str, str]] = None,
) -> DriftCase:
    """Build one case by transforming a known span of a known source.

    The drifted span is located by **composability** — transform the prefix and
    the block separately and add the lengths — so the ground truth is computed
    from the transform, not from anything the matcher believes. The composability
    of every transform is asserted below rather than assumed.
    """
    base = _SYNTHETIC_SOURCE
    if transform is not None and not (transform.composable() and transform.invertible()):
        raise AssertionError(
            f"transform {transform.name!r} is not composable+invertible; the "
            "offset oracle would be unsound on it"
        )

    if text is None:
        if block is None:
            start = base.index(_TARGET)
            end = base.index(_TARGET_END) + 1
            block = (start, end)
        start, end = block
        body = base[start:end]
        if transform is None:
            drifted = base
            d_start, d_end = start, end
        else:
            drifted = transform.forward(base)
            d_start = len(transform.forward(base[:start]))
            d_end = d_start + len(transform.forward(body))
    else:
        drifted = text
        d_start = d_end = 0

    model_old = old if old is not None else base[block[0] : block[1]] if block else ""

    return DriftCase(
        name=name,
        drift_class=drift_class,
        description=description,
        text=drifted,
        old=model_old,
        new=new,
        expect=expect,
        intended_post=(
            _synthetic_post(drifted, d_start, d_end, new) if expect == "apply" else None
        ),
        guard_expect=guard_expect,
        sibling_files=dict(sibling_files or {}),
        inverse=transform.inverse if transform is not None else None,
    )


def synthetic_corpus() -> List[DriftCase]:
    """The ten classes on fixtures small enough to reason about exhaustively.

    This is the DEVELOPMENT surface. The gate is asserted on
    :func:`held_out_corpus`.
    """
    base = _SYNTHETIC_SOURCE
    start = base.index(_TARGET)
    end = base.index(_TARGET_END) + 1
    span = (start, end)

    cases: List[DriftCase] = [
        _drifted_case(
            "syn.trailing_whitespace", "trailing_whitespace",
            "trailing spaces added to every line of the target",
            TRANSFORMS["trailing_whitespace"], expect="apply", block=span,
        ),
        _drifted_case(
            "syn.indentation_tabs", "indentation_tabs",
            "four-space indentation replaced with tabs",
            TRANSFORMS["indentation_tabs"], expect="apply", block=span,
        ),
        _drifted_case(
            "syn.crlf", "crlf",
            "LF converted to CRLF behind the model's back",
            TRANSFORMS["crlf"], expect="apply", block=span,
        ),
        _drifted_case(
            "syn.unicode_nfd", "unicode_nfd",
            "text decomposed from NFC to NFD",
            TRANSFORMS["unicode_nfd"], expect="apply", block=span,
        ),
        _drifted_case(
            "syn.smart_quotes", "smart_quotes",
            "ASCII quotes replaced with typographic quotes",
            TRANSFORMS["smart_quotes"], expect="apply", block=span,
        ),
    ]

    # Class 6 - the block MOVED. Exact matching is expected to succeed; what
    # must be verified is that it lands on the MOVED target and not at the
    # stale offset the model remembers.
    moved = (
        base[:start]
        + base[end:]
        + "\n"
        + base[start:end]
    )
    new_block = "def compute_totals(entries):\n    return 0, []\n"
    moved_start = moved.index(_TARGET)
    cases.append(
        DriftCase(
            name="syn.block_reorder", drift_class="block_reorder",
            description="the target block was moved to the end of the file",
            text=moved,
            old=base[start:end],
            new=new_block,
            expect="apply",
            intended_post=_synthetic_post(moved, moved_start, moved_start + len(_block_of(moved, moved_start)), new_block),
        )
    )

    # Class 7 - a DUPLICATE of the target elsewhere. `ambiguous_match` on the
    # exact path and the uniqueness guard on the fuzzy path are both correct.
    dup_at = base.index("def trailing_return")
    duplicated = base[:dup_at] + base[start:end] + "\n\n" + base[dup_at:]
    cases.append(
        DriftCase(
            name="syn.duplicate_target", drift_class="duplicate_target",
            description="an exact copy of the target was inserted earlier in the file",
            text=duplicated,
            old=base[start:end],
            new="    return 0, []\n",
            expect="refuse",
            guard_expect="uniqueness",
        )
    )

    # Class 8 - a SIMILARLY-NAMED function in another file. The ladder only
    # ever sees ONE file's text, so the claim under test is that resolving in
    # A cannot be satisfied by B's content and that a write cannot escape A.
    sibling = (
        "def compute_totals(entries):\n"
        '    """A DIFFERENT implementation with a similar name."""\n'
        "    return sum(e.amount for e in entries), []\n"
    )
    cases.append(
        DriftCase(
            name="syn.similar_name_other_file", drift_class="similar_name_other_file",
            description="another file declares a similarly-named function",
            text=_SYNTHETIC_SOURCE,
            old=base[start:end],
            new="    return 0, []\n",
            expect="apply",
            intended_post=_synthetic_post(base, start, end, "    return 0, []\n"),
            sibling_files={"other_module.py": sibling},
            guard_expect="containment",
        )
    )

    # Class 9 - a line over 2000 characters inside the target. The file is padded
    # so the target is a REALISTIC fraction of it: a 2000-character line inside
    # a 900-character file would cover three quarters of the file, and the
    # region-coverage guard would then refuse for a reason that is an artefact
    # of the fixture rather than a fact about long lines.
    filler = "".join(
        f"\n\ndef filler_{i}(value):\n"
        f'    """Filler function {i} so the target block is a sane fraction."""\n'
        f"    return value + {i}\n"
        for i in range(28)
    )
    long_line = "    breakdown = ['" + ("x" * 2100) + "']\n"
    long_base = base[:start] + _TARGET + "\n" + long_line + base[end:] + filler
    l_start = long_base.index(_TARGET)
    l_end = long_base.index(_TARGET_END) + 1
    new_long = "    running = 0\n    return running, []\n"
    cases.append(
        DriftCase(
            name="syn.very_long_line", drift_class="very_long_line",
            description="the target contains a line of over 2000 characters",
            text=long_base,
            old=long_base[l_start:l_end],
            new=new_long,
            expect="apply",
            intended_post=_synthetic_post(long_base, l_start, l_end, new_long),
        )
    )

    # Class 10 - a MINIFIED file. One line, no structure, no anchor lines. The
    # correct behaviour is a REFUSAL: fuzzy-matching generated code is how a
    # rebuild target gets corrupted.
    minified_body = " ".join(
        part for part in base.replace("\n", " ").split("  ") if part.strip()
    )
    minified = minified_body + "\n"
    cases.append(
        DriftCase(
            name="syn.minified_generated", drift_class="minified_generated",
            description="the file was minified to a single line before the edit",
            text=minified,
            old=base[start:end],
            new="    return 0, []\n",
            expect="refuse",
            guard_expect="region_coverage",
        )
    )

    # -------------------------------------------------------------------------
    # Class 11 - DISCOVERED BY THIS BENCHMARK, not in the brief's ten.
    #
    # The target block ends in BLANK LINES. Every ladder stage computes a
    # region's extent from a LINE COUNT, and `"a\n\n\n".split("\n")` is
    # `['a','','','']` -- a newline-terminated block splits into trailing EMPTY
    # "lines" that are counted as content. The region therefore reached one line
    # PAST the caller's block. Measured on this very fixture before the fix: a
    # 258-character target returned a 294-character region whose tail was
    # `def unrelated_helper(alpha, beta):`, so applying the edit DELETED the
    # next function's signature.
    #
    # It is published as its own class rather than folded into the five
    # normalisation classes because it is a different failure with a different
    # fix, and because a fixture that ended in blank lines would have made four
    # unrelated drift classes look broken for one reason.
    # -------------------------------------------------------------------------
    tail_start = base.index(_TARGET)
    tail_end = base.index("\ndef unrelated_helper") + 1  # swallows the blanks
    tail_old = base[tail_start:tail_end]
    # The correct answer keeps the block's TRAILING BLANK LINES: the clamp
    # gives back only the region beyond the caller's last content LINE, and a
    # line's extent runs to its own terminator. `.rstrip()` would end one
    # newline short and make this case fail by a single character.
    tail_content_end = base.index("\n", base.index("return running, breakdown")) + 1
    new_tail = "def compute_totals(entries):\n    return 0, []\n"
    cases.append(
        DriftCase(
            name="syn.block_tail_blank_lines", drift_class="block_tail_blank_lines",
            description="old_string ends in blank lines, so a line-count region "
                        "runs past the block into the next declaration",
            text=base,
            old=tail_old,
            new=new_tail,
            expect="apply",
            intended_post=base[:tail_start] + new_tail + base[tail_content_end:],
            guard_expect="region_extent",
        )
    )

    # -------------------------------------------------------------------------
    # Class 12 - DISCOVERED BY THIS BENCHMARK, and the most important class
    # in the file.
    #
    # **The caller's old_string names a function that does not exist anywhere in
    # the file.** A hallucinated symbol is an ordinary thing for a model to do,
    # and it is the case where a fuzzy cascade is most dangerous: there is no
    # drift to absorb and no right answer to find, so any match is by
    # construction the wrong one.
    #
    # Measured on this fixture BEFORE the weak-anchor guard: the ladder anchored
    # on the line `return 1` -- which genuinely IS unique in the file, so the
    # unique-anchor rule was satisfied -- and returned the window
    # `def a():\n\treturn 1\n` at similarity **0.667**, clearing the loose 0.65
    # cascade threshold by 0.017. `block_anchor` is exempt from the size floor,
    # so no guard fired, the edit was APPLIED, and `def a` was overwritten by
    # the replacement. A successful corrupting write, from a typo.
    #
    # The fix is `harness.fuzzy_edit.SCORED_MATCH_STAGES` + the STRONG
    # threshold; legitimate `block_anchor` hits on real code score 0.969-0.995,
    # so requiring 0.90 costs nothing.
    # -------------------------------------------------------------------------
    filler_for_absent = "".join(
        f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24)
    )
    absent_file = base + filler_for_absent
    absent_drifted = TRANSFORMS["indentation_tabs"].forward(absent_file)
    cases.append(
        DriftCase(
            name="syn.absent_target", drift_class="absent_target",
            description="old_string names a function that is not in the file at all",
            text=absent_drifted,
            old="def zzz_absent():\n    return 1\n",
            new="X\n",
            expect="refuse",
            guard_expect="anchor_strength",
        )
    )
    return cases


def _block_of(text: str, start: int) -> str:
    """Return the function block beginning at ``start`` in ``text``."""
    nxt = text.find("\ndef ", start + 1)
    return text[start:] if nxt < 0 else text[start : nxt + 1]


# ---------------------------------------------------------------------------
# Corpus 2 - HELD OUT: real files in this repository
# ---------------------------------------------------------------------------

#: Real modules used for the held-out corpus. Ordinary production code with
#: ordinary docstrings -- not fixtures anybody designed for this benchmark, and
#: not files this round authored.
#:
#: The list is chosen so that EVERY class is measurable on real code. That is
#: not a cosmetic constraint: an earlier list of five small ASCII-only modules
#: left the NFD class with **zero** held-out cases, because a pure-ASCII block
#: has nothing to decompose, and the class would have been quietly absent from
#: the held-out table while the synthetic table showed it passing. Two of the
#: entries are the vendored files, so the cascade is also measured against the
#: code it is built on.
_HELD_OUT_MODULES: Tuple[str, ...] = (
    "harness/third_party/opencode_replacer.py",
    "harness/third_party/aider_bitap.py",
    "harness/coordination.py",
    "harness/codemod.py",
    "harness/decision_memory.py",
)


def _real_block(text: str, *, require_non_ascii: bool = False) -> Optional[Tuple[int, int]]:
    """Locate a real function block by AST. Never hand-picked.

    Requires: a module docstring (so NFD/smart-quote drifts are meaningful), at
    least three lines of body (so the anchor rules have something to work with),
    and a first line that is UNIQUE in the file (so the "duplicate target" class
    is the only thing that introduces ambiguity).

    ``require_non_ascii`` exists because the NFD class is otherwise
    **unmeasurable**: the largest block in a pure-ASCII source file has nothing
    to decompose, so every module silently skipped class 4 and the held-out
    corpus reported no NFD row at all. Passing this flag asks for a block that
    actually carries non-ASCII text -- still located by AST, still never chosen
    by hand.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    def has_non_ascii(value: str) -> bool:
        return any(ord(char) > 127 for char in value)

    best: Optional[Tuple[int, int]] = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (ast.get_docstring(node) or ""):
            continue
        body_lines = (node.end_lineno or node.lineno) - node.lineno
        if body_lines < 3:
            continue
        start = offsets[node.lineno - 1]
        end = (
            offsets[node.end_lineno - 1] + len(lines[node.end_lineno - 1])
            if node.end_lineno
            else len(text)
        )
        if text.count(lines[node.lineno - 1]) != 1:
            continue
        if require_non_ascii and not has_non_ascii(text[start:end]):
            continue
        if best is None or (end - start) > (best[1] - best[0]):
            best = (start, end)
    return best


#: Cases a corpus declined to build because the drift does not apply to the
#: fixture -- e.g. a real block with no non-ASCII text cannot express the NFD
#: drift. Published on the report so a class that ran on fewer modules is
#: visible rather than quietly short.
SKIPPED_DRIFTS: List[str] = []


def held_out_corpus(root: str) -> List[DriftCase]:
    """The same ten classes applied to REAL repository files.

    ``root`` is the repository root. Every case's target block is located by
    ``ast`` in a file this benchmark did not author, and the drift is applied by
    the same composable transforms — so a class that only works on the tidy
    synthetic fixture is caught here rather than shipping.
    """
    import os

    cases: List[DriftCase] = []
    modules = [
        m for m in _HELD_OUT_MODULES if os.path.isfile(os.path.join(root, m))
    ]
    for index, module in enumerate(modules):
        with open(os.path.join(root, module), "r", encoding="utf-8") as handle:
            source = _prepare_fixture(handle.read())
        block = _real_block(source)
        if block is None:
            continue
        start, end = block
        body = source[start:end]
        stem = os.path.splitext(os.path.basename(module))[0]

        for transform in TRANSFORMS.values():
            drifted = transform.forward(source)
            if drifted == source and transform.name == "unicode_nfd":
                # Re-locate a block that actually carries non-ASCII text; the
                # largest block in a pure-ASCII file has nothing to decompose.
                alt = _real_block(source, require_non_ascii=True)
                if alt is not None:
                    start, end = alt
                    body = source[start:end]
            if drifted == source:
                SKIPPED_DRIFTS.append(
                    f"{stem}/{transform.name}: the selected real block has no "
                    f"{transform.name} to drift"
                )
                continue
            drifted = transform.forward(source)
            d_start = len(transform.forward(source[:start]))
            d_end = d_start + len(transform.forward(body))
            new = _short_replacement(body)
            cases.append(
                DriftCase(
                    name=f"held.{stem}.{transform.name}",
                    drift_class=transform.name,
                    description=f"{transform.name} applied to a real block in {module}",
                    text=drifted,
                    old=body,
                    new=new,
                    expect="apply",
                    intended_post=drifted[:d_start] + new + drifted[d_end:],
                    inverse=transform.inverse,
                )
            )

        # 6 - the real block MOVED to the end of a real file.
        moved = source[:start] + source[end:] + "\n" + body
        m_start = moved.rindex(body)
        new = _short_replacement(body)
        cases.append(
            DriftCase(
                name=f"held.{stem}.block_reorder",
                drift_class="block_reorder",
                description=f"a real block in {module} was moved to the end",
                text=moved, old=body, new=new, expect="apply",
                intended_post=moved[:m_start] + new + moved[m_start + len(body):],
            )
        )

        # 7 - the real block DUPLICATED earlier in a real file.
        dup_at = max(0, start - len(body) - 8)
        duplicated = source[:dup_at] + body + "\n\n" + source[dup_at:]
        cases.append(
            DriftCase(
                name=f"held.{stem}.duplicate_target",
                drift_class="duplicate_target",
                description=f"a real block in {module} was duplicated earlier",
                text=duplicated, old=body, new=_short_replacement(body),
                expect="refuse", guard_expect="uniqueness",
            )
        )

        # 8 - a similarly-named function in ANOTHER real file.
        sibling_name = _similar_name_module(stem, index)
        new = _short_replacement(body)
        cases.append(
            DriftCase(
                name=f"held.{stem}.similar_name_other_file",
                drift_class="similar_name_other_file",
                description="a similarly-named function exists in a sibling module",
                text=source, old=body, new=new, expect="apply",
                intended_post=source[:start] + new + source[end:],
                sibling_files={f"{stem}_sibling.py": sibling_name},
                guard_expect="containment",
            )
        )

        # 9 - a real block with a line pushed over 2000 characters.
        lines = body.splitlines(keepends=True)
        padded = lines[0] + '    _long = "' + ("y" * 2100) + '"\n' + "".join(lines[1:])
        long_source = source[:start] + padded + source[end:]
        new = _short_replacement(padded)
        cases.append(
            DriftCase(
                name=f"held.{stem}.very_long_line",
                drift_class="very_long_line",
                description=f"a real block in {module} carries a 2000+ character line",
                text=long_source, old=padded, new=new, expect="apply",
                intended_post=long_source[:start] + new + long_source[start + len(padded):],
            )
        )

        # 11 - the real block's `old` ends in BLANK LINES, which is the shape the
        # `region_extent` clamp exists for. Built by appending the blank lines
        # to the caller's copy of a real block; the file is untouched.
        tail_old = body + "\n\n\n"
        tail_source = source[:end] + "\n\n" + source[end:]
        t_content_end = tail_source.index("\n", end - 1) + 1
        new = _short_replacement(body)
        cases.append(
            DriftCase(
                name=f"held.{stem}.block_tail_blank_lines",
                drift_class="block_tail_blank_lines",
                description=f"a real block's old_string ends in blank lines ({module})",
                text=tail_source, old=tail_old, new=new, expect="apply",
                intended_post=tail_source[:start] + new + tail_source[t_content_end:],
                guard_expect="region_extent",
            )
        )

        # 12 - a caller asked for a symbol that does not exist in this real file.
        cases.append(
            DriftCase(
                name=f"held.{stem}.absent_target",
                drift_class="absent_target",
                description=f"old_string names a function absent from {module}",
                text=source,
                old="def zzz_absent_symbol():\n    return 1\n",
                new="X\n",
                expect="refuse",
                guard_expect="anchor_strength",
            )
        )

        # 10 - the real file MINIFIED to one line.
        minified = " ".join(source.split()) + "\n"
        cases.append(
            DriftCase(
                name=f"held.{stem}.minified_generated",
                drift_class="minified_generated",
                description=f"{module} was flattened to a single line",
                text=minified, old=body, new=_short_replacement(body),
                expect="refuse", guard_expect="anchor",
            )
        )
    return cases


def _short_replacement(body: str) -> str:
    """A short, obviously-different replacement for a real block.

    Kept deliberately tiny and syntactically whole. The benchmark's question is
    "did the edit land in the right PLACE", and a large replacement would make
    a wrong match produce a syntax error — which the editor's pre-commit gate
    would catch, and which would therefore *hide* the very wrong-match this
    benchmark exists to detect.
    """
    lines = body.splitlines(keepends=True)
    head = lines[0] if lines else "def replacement():"
    return head + "    return None\n"


def _similar_name_module(stem: str, index: int) -> str:
    """A sibling module carrying a similarly-named function to ``stem``."""
    return (
        f'"""{stem}_sibling - a similarly-named function, different body."""\n\n\n'
        f"def {stem}_near_duplicate(value):\n"
        '    """Same shape, different behaviour."""\n'
        "    return value\n"
    )


def default_repo_root() -> str:
    """The repository root, derived from this file's location."""
    import os

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
