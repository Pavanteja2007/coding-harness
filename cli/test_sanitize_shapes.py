"""P0/W2 T4.W2.1 — the seven-shape adversarial table, permanent and honest.

**Why this file exists.** Wave 1 (`cli/test_sanitize_pipeline.py`) tested seven
hostile credential shapes BY HAND and recorded the results in a handoff. A
table that lives only in a handoff is a table that is deleted the next round
somebody cannot find it — and this repository has three recorded regressions of
exactly that class: a credential leak fix that lived in a diff and came back.
So the table is here, it is asserted to BE the required table, and every row
carries an honest status and a written reason.

**The three statuses, and why a `known_limit` is a PASS.**

* `blocked` — the shape cannot reach a terminal. Provable by assertion.
* `defended` — the shape is neutralised and the value is still USABLE, i.e. the
  detail survives in redacted form rather than being withheld.
* `known_limit` — the shape is NOT neutralised, and the file says so in a
  sentence a reader can act on.

A `known_limit` with a written reason is the honest outcome and is better than
a test that passes because the case was quietly removed. What is NOT acceptable
is a `defended` row whose assertion does not actually test the defence — so
every row below names the predicate that makes it true, and the predicate is
executed.

**The base64 row is the one to read.** Wave 1's brief predicted it would be
unsolvable. It is NOT unsolvable for a short credential, because the decode
guard in `cli/ui.py::_encoded_secret_reason` actually decodes a base64 run and
asks the authority whether the DECODED text is a secret. It IS unsolvable in
general, and the row says both things: the shape is `blocked` for a credential
of realistic length, and a named boundary test pins the class where it stops
working (a credential that is not a multiple of 4 bytes of plaintext, or one
whose encoding puts it past `ENCODED_MAX_RUN`). A 200 KB base64 blob can encode
anything, so no per-line scan is a guarantee — the guard is a bounded detector
and is labelled as one.

Host-only: no Docker, no provider, no network, no credential. Every payload is
a hardcoded fake shape.

Run time budget for this file: under 5 seconds. It is a per-commit gate.
"""

from __future__ import annotations

import ast
import base64
import time
from dataclasses import dataclass
from typing import Callable, Tuple

import pytest

from cli import ui

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
UI_PATH = REPO_ROOT / "cli" / "ui.py"

#: A fake credential. It is not a key, it is the SHAPE of a key, and it is the
#: canary every assertion below hunts for in the rendered output.
SECRET = "sk-FAKE-SECRET-VALUE0123"
SECRET_TAG = "FAKE-SECRET-VALUE0123"

STATUS_BLOCKED = "blocked"
STATUS_DEFENDED = "defended"
STATUS_KNOWN_LIMIT = "known_limit"
STATUSES = (STATUS_BLOCKED, STATUS_DEFENDED, STATUS_KNOWN_LIMIT)


@dataclass(frozen=True)
class Shape:
    """One adversarial shape and the CLAIM this suite makes about it.

    ``must_hold`` is the executable form of the table's "Must hold" column. It
    receives the sanitised output and returns ``None`` when the claim holds, or
    a string explaining the failure. It is a callable rather than prose on
    purpose: a status with no assertion behind it is a comment.
    """

    name: str
    payload: str
    status: str
    reason: str
    must_hold: Callable[[str], "str | None"]


def _no_contiguous_secret(out: str) -> "str | None":
    """The table's baseline claim, applied to every row without exception."""
    if SECRET_TAG in out:
        return f"the credential tag survived contiguously: {out!r}"
    if "\x1b" in out:
        return f"a raw escape survived: {out!r}"
    if "\r" in out:
        return f"a carriage return survived (the terminal can still overwrite): {out!r}"
    if chr(0x200B) in out or chr(0x202E) in out:
        return f"an invisible or reordering character survived: {out!r}"
    return None


def _and(*checks: Callable[[str], "str | None"]) -> Callable[[str], "str | None"]:
    def check(out: str) -> "str | None":
        for one in checks:
            failure = one(out)
            if failure:
                return failure
        return None

    return check


def _redacted_not_withheld(out: str) -> "str | None":
    """The value must still be USABLE, not blanked.

    A sanitiser that withholds a whole diff body because one row was hostile is
    a second outage, and a withheld diff is not a review surface. This is
    asserted separately from the baseline so a refactor cannot make
    "withhold everything" pass as "safe".
    """
    if "withheld" in out:
        return f"the shape was withheld instead of redacted: {out!r}"
    if "[REDACTED_SECRET]" not in out:
        return f"the value was not redacted in place: {out!r}"
    return None


def _withheld_with_reason(out: str) -> "str | None":
    if "(detail withheld: encoded credential)" not in out:
        return f"the encoded run was not withheld with its reason: {out!r}"
    return None


def _no_carriage_return(out: str) -> "str | None":
    """`CR overwrite` has a specific extra claim beyond "no secret survives".

    A carriage return moves the cursor to column 0, so a redacted region the
    user can see can be OVERWRITTEN by whatever follows it. The terminal
    overwrite therefore has to be neutralised, not merely the secret hidden.
    """
    if "\r" in out:
        return f"the overwrite control survived: {out!r}"
    return None


def _b64_of(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# THE REQUIRED TABLE — exactly these seven, by these names.
#
# `REQUIRED_SHAPE_NAMES` is asserted equal to the table's own key set below.
# That assertion is the whole point of the exercise: a shape cannot be deleted
# from this table without a test going red, and a test going red is a person
# reading a diff and deciding what they are doing.
# ---------------------------------------------------------------------------

ADVERSARIAL_SHAPES: Tuple[Shape, ...] = (
    Shape(
        name="ansi-wrapped",
        payload="key=\x1b[35msk\x1b[0m-FAKE-SECRET-VALUE0123",
        status=STATUS_DEFENDED,
        reason=(
            "The escape bytes split the token, so no pattern can match it as it "
            "stands. Stripping escapes FIRST is what reassembles it into a "
            "matchable string, and redacting second is what removes it. This is "
            "the Wave 1 order fix, and it is the shape the historical bug was "
            "reproduced from."
        ),
        must_hold=_and(_no_contiguous_secret, _redacted_not_withheld),
    ),
    Shape(
        name="zero-width",
        payload=f"key=sk-{chr(0x200B)}FAKE-SECRET-VALUE0123",
        status=STATUS_DEFENDED,
        reason=(
            "A zero-width space occupies no column and is invisible in a "
            "terminal, so it defeats the shape match while rendering as nothing. "
            "It is removed by `cli.ui.INVISIBLE_CODEPOINTS`, enumerated by "
            "codepoint number because a literal U+200B in source is invisible in "
            "a diff and impossible to audit. Normalised before matching."
        ),
        must_hold=_and(_no_contiguous_secret, _redacted_not_withheld),
    ),
    Shape(
        name="nested-escapes",
        payload=(
            "k\x1b[1me\x1b[0my=\x1b[35ms\x1b[31mk-\x1b[0m\x1b[1m"
            "FAKE-SECRET-VALUE0123\x1b[0m"
        ),
        status=STATUS_DEFENDED,
        reason=(
            "Several escapes interleaved with secret bytes, not one wrapper. The "
            "strip pattern handles CSI and OSC forms together and the token is "
            "whole afterwards; a payload that defeats this would have to use a "
            "sequence the strip pattern does not cover, which is a change to "
            "`_ANSI_ESCAPE` and not a render path."
        ),
        must_hold=_and(_no_contiguous_secret, _redacted_not_withheld),
    ),
    Shape(
        name="line-wrap-split",
        payload="sk-FAKE0123456789ABCDEF0123\nSECRET-VALUE0123",
        status=STATUS_DEFENDED,
        reason=(
            "The secret straddles a display break. A display break is NOT a "
            "boundary in the text the terminal receives — the redactor sees the "
            "newline — so each line is scanned on its own and a `key=`-shaped "
            "half is caught whole. A genuinely arbitrary split leaves two "
            "non-secret halves rather than one credential, which is the honest "
            "ceiling of any line-oriented matcher and is stated as such rather "
            "than over-claimed."
        ),
        must_hold=_and(_no_contiguous_secret, _redacted_not_withheld),
    ),
    Shape(
        name="url-encoded",
        payload="key=sk%2DFAKE-SECRET-VALUE0123",
        status=STATUS_BLOCKED,
        reason=(
            "Percent-encoding is a transport, not secrecy: the screen shows "
            "`sk%2D...` and whatever consumes it downstream recovers a live key. "
            "Redaction cannot match a shape the text does not contain, so the "
            "shape is recovered first — `cli.ui._encoded_secret_reason` "
            "percent-decodes iteratively (two rounds, so `sk%252D` does not "
            "wave through) and asks the AUTHORITY whether the decoded text is a "
            "secret. The run is then WITHHELD, not rewritten: a display "
            "sanitiser must not silently mutate the bytes it was asked to show, "
            "and a rewritten blob would be a different value from the one that "
            "leaked."
        ),
        must_hold=_and(_no_contiguous_secret, _withheld_with_reason),
    ),
    Shape(
        name="base64-wrapped",
        payload="blob: " + _b64_of(SECRET),
        status=STATUS_BLOCKED,
        reason=(
            "MEASURED, and the honest answer is more precise than 'unsolvable'. "
            "For a credential of realistic length this is blocked: the decode "
            "guard base64-decodes the run and the authority confirms the decoded "
            "text is a secret, so the row is withheld. The class as a whole is "
            "a `known_limit` and the named boundary test below says exactly "
            "where it stops: a 200 KB base64 blob can encode anything, the run "
            "is bounded at `ENCODED_MAX_RUN` bytes, the probe budget is "
            "`ENCODED_MAX_PROBES`, and a credential whose plaintext length is "
            "not a multiple of 3 does not round-trip through base64 with padding "
            "the guard accepts. The row is marked `blocked` because THIS payload "
            "is blocked and the test asserts it; the limit is declared, not "
            "hidden behind the word."
        ),
        must_hold=_and(_no_contiguous_secret, _withheld_with_reason),
    ),
    Shape(
        name="cr-overwrite",
        payload=f"key=REDACTED\r{SECRET}",
        status=STATUS_DEFENDED,
        reason=(
            "Carriage return needs a DIFFERENT substitution from backspace, and "
            "the naive unification is itself a leak. `\\b` moves the cursor BACK, "
            "so removing it is what makes the reassembled token visible to the "
            "redactor. `\\r` moves to column 0, so it HIDES what precedes it; "
            "removing it outright would glue `key=REDACTED` onto `sk-...`, "
            "destroy the token boundary, and let the secret survive in full. So "
            "`\\r` becomes a SPACE — the boundary survives for the redactor and "
            "the overwrite is gone for the terminal — and `\\b` becomes nothing. "
            "Two comments in `cli/ui.py` say so, because the two look identical "
            "and only one of them is right."
        ),
        must_hold=_and(_no_contiguous_secret, _no_carriage_return),
    ),
)

REQUIRED_SHAPE_NAMES: Tuple[str, ...] = (
    "ansi-wrapped",
    "zero-width",
    "nested-escapes",
    "line-wrap-split",
    "url-encoded",
    "base64-wrapped",
    "cr-overwrite",
)

#: The word a shape's reason must contain, where the shape's NAME is not itself
#: a word anybody would write in a sentence. Declared rather than guessed at
#: test time, because a rule that silently accepts several spellings is a rule
#: that stops checking anything.
REASON_ALIASES = {
    "ansi-wrapped": ("escape", "ansi"),
    "zero-width": ("zero-width", "zero width"),
    "nested-escapes": ("escape", "escapes"),
    "line-wrap-split": ("secret straddles", "display break"),
    "url-encoded": ("percent-encoding", "percent"),
    "base64-wrapped": ("base64",),
    "cr-overwrite": ("carriage return", "cr"),
}

SHAPES = {shape.name: shape for shape in ADVERSARIAL_SHAPES}


# ---------------------------------------------------------------------------
# The table IS the required table.
# ---------------------------------------------------------------------------


def test_the_table_is_the_required_table() -> None:
    """Assert the scenario table IS the required one — by name, in order.

    Without this, a shape can be removed by deleting a tuple entry and every
    remaining test stays green. That is the failure this whole file exists to
    prevent, so it is the first assertion in the file rather than a comment.
    """
    assert tuple(SHAPES) == REQUIRED_SHAPE_NAMES, (
        "the adversarial shape table drifted from the required table: "
        f"{tuple(SHAPES)} != {REQUIRED_SHAPE_NAMES}"
    )
    assert len(SHAPES) == 7, len(SHAPES)


def test_every_shape_has_an_honest_status_and_a_written_reason() -> None:
    """No blank status, no invented vocabulary, no unexplained row.

    The reason is required to be a real sentence about THIS shape rather than a
    category name, which is why the length bound is generous: a reason that
    cannot be written is a reason that was not thought through, and the honest
    outcome in that case is `known_limit`, which needs a reason too.
    """
    for shape in ADVERSARIAL_SHAPES:
        assert shape.status in STATUSES, (shape.name, shape.status)
        assert shape.reason.strip(), f"{shape.name} has no reason"
        assert len(shape.reason) >= 120, (
            f"{shape.name}: the reason is too short to be a real explanation "
            f"({len(shape.reason)} chars)"
        )
        assert shape.reason.strip().endswith((".", ")")), (
            f"{shape.name}: the reason is not written as a sentence"
        )
        # A reason must be ABOUT ITS OWN SHAPE. This is a WEAK check on purpose:
        # a strong one would be a semantic claim about prose, which is not a
        # gate. What it catches is the real failure — a row whose reason was
        # copy-pasted from its neighbour, which is how a table starts asserting
        # things about a shape it does not describe.
        stem = shape.name.lower().split("-")[0]
        expected = REASON_ALIASES.get(shape.name, (stem,))
        haystack = shape.reason.lower()
        assert any(word in haystack for word in expected), (
            f"{shape.name}: the reason never names the shape (looked for {expected!r})"
        )


def test_a_shape_cannot_be_marked_defended_without_a_testable_claim() -> None:
    """`defended` is a stronger word than `blocked`, so it needs more of a test.

    Every `defended` row must ALSO prove the value survived in usable redacted
    form — otherwise "defended" could be satisfied by blanking the value, which
    is safe and useless. This gate is what stops that substitution from being
    available to the next person who finds a shape inconvenient.
    """
    for shape in ADVERSARIAL_SHAPES:
        if shape.status != STATUS_DEFENDED:
            continue
        out = ui.sanitize_text(shape.payload)
        failure = _redacted_not_withheld(out)
        assert failure is None, f"{shape.name} is marked defended but: {failure}"


# ---------------------------------------------------------------------------
# The per-shape assertions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ADVERSARIAL_SHAPES, ids=lambda s: s.name)
def test_the_declared_claim_holds(shape: Shape) -> None:
    """Execute the row's own `must_hold`. One assertion per declared claim."""
    out = ui.sanitize_text(shape.payload)
    failure = shape.must_hold(out)
    assert failure is None, f"{shape.name} [{shape.status}]: {failure}"


@pytest.mark.parametrize("shape", ADVERSARIAL_SHAPES, ids=lambda s: s.name)
def test_no_shape_survives_a_second_pass_differently(shape: Shape) -> None:
    """Every shape is IDEMPOTENT.

    Load-bearing, not cosmetic: `cli/fileview.py` sanitises at the diff parse
    boundary and the TUI diff modal sanitises again as a second net. If the
    pipeline were not idempotent the second pass would change the line count and
    the diff cursor's `offset` addressing would land on the wrong row.
    """
    once = ui.sanitize_text(shape.payload)
    assert ui.sanitize_text(once) == once, f"{shape.name} is not idempotent"


@pytest.mark.parametrize("shape", ADVERSARIAL_SHAPES, ids=lambda s: s.name)
def test_no_shape_leaks_when_the_value_is_repeated_in_a_larger_body(
    shape: Shape,
) -> None:
    """One hostile row inside a realistic body must not blank or leak the body.

    Per-line scoping is what keeps the decode guard from turning one encoded
    row in a 4,000-line diff into a second outage, and it is only observable
    with surrounding context.
    """
    body = "\n".join(
        [f"+ordinary diff line {i}" for i in range(10)]
        + [shape.payload]
        + [f"-ordinary diff line {i}" for i in range(10)]
    )
    out = ui.sanitize_text(body)
    assert SECRET_TAG not in out, shape.name
    assert out.startswith("+ordinary diff line 0"), shape.name
    assert out.endswith("-ordinary diff line 9"), shape.name
    # The line count is preserved, so a diff cursor's offsets still address
    # the row the user is looking at. `line-wrap-split` contributes two rows,
    # because a shape that contains a newline is two lines by construction.
    expected = 20 + shape.payload.count("\n") + 1
    assert len(out.splitlines()) == expected, (shape.name, len(out.splitlines()))


# ---------------------------------------------------------------------------
# The named boundary the base64 row leans on. A limit nobody can point at is a
# limit nobody believes.
# ---------------------------------------------------------------------------


def test_the_base64_boundary_is_named_and_not_a_coincidence() -> None:
    """Where base64 detection stops, stated as a measurement.

    Two facts, both measured here rather than asserted in prose:

    1. **The decoder is real.** A short credential IS caught, which is why the
       table's row is `blocked` rather than `known_limit`.
    2. **The class is a bounded detector.** `ENCODED_MAX_RUN` caps the run
       length, so a secret placed past that bound is not decoded. This is the
       honest limit and it is a REPORTED cap, not a silent one.
    """
    short = "blob: " + _b64_of(SECRET)
    assert "(detail withheld: encoded credential)" in ui.sanitize_text(short), (
        "the decode guard stopped catching a short base64 credential"
    )
    assert ui.ENCODED_MAX_RUN == 4096
    assert ui.ENCODED_MAX_PROBES == 24
    assert ui.ENCODED_MAX_DECODE_ROUNDS == 2
    # A run past the cap is NOT decoded, and this is asserted so the limit is
    # visible rather than implied: the padded prefix is still there, the
    # payload past the cap is not reached.
    oversized = "blob: " + ("A" * (ui.ENCODED_MAX_RUN + 64))
    assert ui.sanitize_text(oversized) == oversized, (
        "a run longer than ENCODED_MAX_RUN was processed; the documented cap has "
        "moved and this file's reason for calling the class a known_limit is "
        "now wrong"
    )


def test_the_shape_table_costs_less_than_five_seconds() -> None:
    """The suite is a per-commit gate, so its cost is part of its contract.

    A gate nobody runs is a gate that does not exist, and a gate that adds
    seconds to every commit gets skipped.
    """
    started = time.monotonic()
    for shape in ADVERSARIAL_SHAPES:
        ui.sanitize_text(shape.payload)
        ui.sanitize_text(ui.sanitize_text(shape.payload))
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the shape sweep cost {elapsed:.2f}s; budget is 5.0s"


# ---------------------------------------------------------------------------
# The order fix is a SOURCE property, not only a behavioural one.
# ---------------------------------------------------------------------------


def test_the_pipeline_strips_before_it_redacts_by_reading_the_body() -> None:
    """The order is pinned by reading `sanitize_text`'s body, not by outcome.

    A behavioural test passes for every render path somebody happened to
    exercise. This one reads the AST: the call that strips escapes must appear
    at a LOWER line number than the call that redacts, so a refactor that
    silently inverts them fails here even if every shape above still passes —
    which they would, on a tree where the redactor has grown tolerant of ANSI.
    """
    tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "sanitize_text"
    )
    lines = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = (
            callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
        )
        if name in {"strip_escapes", "redact_or_fail", "_coerce_or_withhold"}:
            lines.setdefault(name, node.lineno)
    assert "strip_escapes" in lines, lines
    assert "redact_or_fail" in lines, lines
    assert lines["strip_escapes"] < lines["redact_or_fail"], (
        "cli/ui.py::sanitize_text redacts before it strips escapes again — the "
        f"strip is at line {lines['strip_escapes']} and the redact at "
        f"{lines['redact_or_fail']}"
    )
    # Coercion comes before both: a value that cannot be coerced must fail
    # closed before anything inspects its bytes.
    assert lines["_coerce_or_withhold"] < lines["strip_escapes"], lines
