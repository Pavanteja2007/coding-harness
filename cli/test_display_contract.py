"""P0/W2 T4.W2.3 + T4.W2.4 — fail closed, observably, and the error-UX contract.

Two contracts in one file because they are the same contract seen from two
ends: a value that reaches a terminal must be sanitised, must fail CLOSED when
sanitisation is impossible, must say so in a record somebody can read, and must
do it inside a card that still tells the user what to type next.

**W2.3 — fail closed, tested by injecting BELOW the sanitiser.**

The failure modes differ, which is why there are four tests rather than one
parameterised shape:

| injected failure | injected at | expected |
|---|---|---|
| the pattern matcher RAISES | `shared.security.normalize_for_redaction` | detail withheld; value never appears |
| the pattern matcher returns a NON-TUPLE | the same function | detail withheld |
| `cli.ui`'s redactor binding RAISES | one layer below `sanitize_text` | detail withheld |
| `cli.ui`'s redactor binding returns `None` | one layer below | detail withheld |
| the redactor is an IDENTITY | one layer below | detail withheld, caught by the liveness probe |
| the redactor REDACTS and is then UNDONE | one layer below | **KNOWN LIMIT** — declared, pinned, not hidden |

**Why the last row is a known limit rather than a fifth pass.** It is the case
this round's own gate found: a redactor that redacts correctly and then has its
output restored discloses the value, and `redactor_is_functional()` reports it
as FUNCTIONAL because the probe string is restored too. No O(1) probe can detect
a component that is broken in the same way for every input. The rejected
alternative is to re-scan every rendered value with a SECOND pattern set inside
`cli`, which this repository measured at **+122 %** on a 9 KB diff and which is
the wrong shape anyway: two pattern sets are two answers to "is this a secret"
and the second one rots silently. So it is declared, named in
`cli.ui.redactor_is_functional`'s docstring, and pinned by a test that asserts
the disclosure so nobody "fixes" the probe by removing the test.

**Why injecting BELOW matters.** Stubbing the sanitiser proves nothing: the stub
is the thing under test, so any assertion about it passes. Every injection here
leaves `cli.ui.sanitize_text`, `cli.ui.redact_or_fail`, `cli.ui._withheld` and
`cli.ui._note_withholding` — the whole fail-closed chain — untouched.

**W2.4 — the error-UX contract, because Phase 5 builds on these cards.**

`cli.runview.failure_lines` is a genuinely good piece of work: kind -> why ->
hint -> next-actions -> a GUARANTEED runnable `/command`, because *"a classifier
action can be pure diagnosis, which is correct advice and useless as an
instruction."* Five assertions hold it there, and the sixth — the one that was
BROKEN — is this round's product fix.

Host-only: no Docker, no provider, no network, no credential.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable, Tuple

import pytest
import rich.markup

from cli import runview as rv
from cli import ui

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNVIEW_PATH = REPO_ROOT / "cli" / "runview.py"

SECRET = "sk-FAKE-SECRET-VALUE0123"
SECRET_TAG = "FAKE-SECRET-VALUE0123"
PAYLOAD = "key=" + SECRET

#: A card is useless without an instruction, so this is the shape Phase 5's diff
#: pane and git UI inherit. The prompt's own words: "a classifier action can be
#: pure diagnosis, which is correct advice and useless as an instruction".
RUNNABLE_COMMAND = re.compile(r"/[a-z][a-z0-9_-]+")

#: Every failure kind `cli.fileview.classify_failure` can report. Read from the
#: classifier's own table rather than restated, so a new kind cannot be added
#: without the distinctness assertion covering it.
FAILURE_KINDS: Tuple[str, ...] = ()


def _failure_kinds() -> Tuple[str, ...]:
    from cli import fileview

    return tuple(sorted(fileview._RECOVERY_ACTIONS_BY_KIND))


@pytest.fixture(autouse=True)
def _reset_the_ledger():
    """Every test in this file starts from an empty withholding ledger.

    Without this a test cannot tell whether IT caused a withholding, and a
    ledger that carries another test's failure is a ledger nobody can read.
    """
    ui._SANITIZE_WITHHOLDINGS.clear()
    ui._redactor_ok = None
    yield
    ui._SANITIZE_WITHHOLDINGS.clear()
    ui._redactor_ok = None


# ---------------------------------------------------------------------------
# W2.3 — the four failure modes, injected below the sanitiser.
# ---------------------------------------------------------------------------


class _Poison:
    """A context manager that poisons one module attribute and restores it.

    Restoration is in a `finally` rather than at the end of the test body,
    because a test that fails mid-way and leaves the redactor broken poisons
    every test after it — which is a gate that lies about the next failure.
    """

    def __init__(self, module, name: str, value) -> None:
        self.module = module
        self.name = name
        self.value = value
        self.saved = None

    def __enter__(self):
        self.saved = getattr(self.module, self.name, _MISSING)
        setattr(self.module, self.name, self.value)
        return self

    def __exit__(self, *_exc) -> bool:
        if self.saved is _MISSING:
            delattr(self.module, self.name)
        else:
            setattr(self.module, self.name, self.saved)
        return False


_MISSING = object()


def _raising(*_a, **_kw):
    raise RuntimeError("the pattern matcher exploded")


def _returns_none(*_a, **_kw):
    return None


def _identity(value, *_a, **_kw):
    return value


def _redact_then_undo(value, *args, **kwargs):
    """A redactor that works, and then has its own output restored.

    This is the shape the liveness probe cannot see, and it is here as a REAL
    failure rather than as a hypothetical: it is what a hostile or broken layer
    beneath the redactor looks like from above.
    """
    import shared.security as security

    out = security.redact_text(value, *args, **kwargs)
    return out.replace(security.REDACTED_SECRET, SECRET)


#: ``(label, target, attribute, replacement, expected_outcome)``
#:
#: ``target`` is chosen so the SANITISER ITSELF is never replaced: every entry
#: poisons something `cli/ui.py` reaches on the way to the terminal.
FAILURE_MODES: Tuple[Tuple[str, str, str, Callable, str], ...] = (
    (
        "the authority's own matcher raises",
        "shared.security",
        "normalize_for_redaction",
        _raising,
        "withheld",
    ),
    (
        "the authority's own matcher returns a non-tuple",
        "shared.security",
        "normalize_for_redaction",
        _returns_none,
        "withheld",
    ),
    (
        "the redactor binding cli/ui.py holds raises",
        "cli.ui",
        "redact_text",
        _raising,
        "withheld",
    ),
    (
        "the redactor binding cli/ui.py holds returns None",
        "cli.ui",
        "redact_text",
        _returns_none,
        "withheld",
    ),
    (
        "the redactor is the identity function",
        "cli.ui",
        "redact_text",
        _identity,
        "withheld",
    ),
    (
        "the redactor redacts and its output is then restored",
        "cli.ui",
        "redact_text",
        _redact_then_undo,
        "known_limit",
    ),
)


def _import_target(name: str):
    if name == "cli.ui":
        return ui
    import importlib

    return importlib.import_module(name)


@pytest.mark.parametrize(
    "label,module_name,attribute,replacement,outcome",
    FAILURE_MODES,
    ids=[row[0].replace(" ", "-") for row in FAILURE_MODES],
)
class TestFailClosedByInjectionBelowTheSanitiser:
    """The call chain fails closed, not a given implementation."""

    def test_the_value_never_appears_and_the_detail_is_withheld(
        self, label, module_name, attribute, replacement, outcome
    ) -> None:
        module = _import_target(module_name)
        with _Poison(module, attribute, replacement):
            out = ui.sanitize_text(PAYLOAD)
        if outcome == "known_limit":
            # Declared limit: asserted rather than hoped for, so the row cannot
            # quietly start passing and nobody notices the limit disappeared.
            assert SECRET_TAG in out, (
                f"{label} no longer discloses, so this row should be promoted "
                f"from known_limit to withheld and the probe should be revisited"
            )
            return
        assert SECRET_TAG not in out, f"{label} LEAKED: {out!r}"
        assert "withheld" in out, f"{label} did not say it withheld: {out!r}"

    def test_the_withholding_is_observable_in_the_ledger(
        self, label, module_name, attribute, replacement, outcome
    ) -> None:
        """A silently withheld detail is an invisible failure.

        This is the assertion that was RED before this round's product fix:
        `sanitize_report()` reported `withheld: 0` while the screen showed
        `(detail withheld: ...)`, because an authority-side withholding arrived
        as a perfectly safe STRING and never entered the CLI's own branch. A
        user saw the withholding and the ledger had no record of it, which is
        exactly how a broken redactor becomes a permanent condition nobody
        notices.
        """
        module = _import_target(module_name)
        with _Poison(module, attribute, replacement):
            ui.sanitize_text(PAYLOAD)
            report = ui.sanitize_report()
        if outcome == "known_limit":
            assert report["withheld"] == 0, (
                "the known-limit row now records a withholding; the probe "
                "mechanism changed and this row needs re-deciding"
            )
            return
        assert report["withheld"] >= 1, f"{label} withheld silently: {report}"
        assert report["by_reason"], f"{label} recorded no reason: {report}"
        for reason in report["by_reason"]:
            assert reason in ui.WITHHELD_REASONS, (
                f"{label} recorded an undeclared reason {reason!r}"
            )

    def test_the_record_carries_the_fact_and_never_the_value(
        self, label, module_name, attribute, replacement, outcome
    ) -> None:
        """The receipt describes the leak without containing it.

        A receipt that carried the value would be the disclosure it is
        reporting, so every field is checked for the canary.
        """
        module = _import_target(module_name)
        with _Poison(module, attribute, replacement):
            ui.sanitize_text(PAYLOAD, task_id="fix-abc123")
            events = ui.sanitize_report()["events"]
        blob = repr(events)
        assert SECRET_TAG not in blob, f"{label} put the value in the record"
        if outcome == "known_limit":
            return
        assert events, f"{label} recorded no event"
        row = events[-1]
        assert set(row) == {"seq", "reason", "chars_withheld", "task_id"}, row
        assert row["task_id"] == "fix-abc123", row
        assert row["chars_withheld"] > 0, row


def test_the_sanitiser_is_never_replaced_by_the_injection() -> None:
    """The point of injecting low: the fail-closed chain is still the real one.

    If this ever fails, the four modes above have stopped testing the product
    and started testing a stub — which is the exact failure mode the injection
    strategy exists to avoid.
    """
    import shared.security as security

    original = (
        ui.sanitize_text,
        ui.redact_or_fail,
        ui._withheld,
        ui._note_withholding,
        ui._is_authority_withholding,
    )
    with _Poison(security, "normalize_for_redaction", _raising):
        assert ui.sanitize_text is original[0]
        assert ui.redact_or_fail is original[1]
        assert ui._withheld is original[2]
        assert ui._note_withholding is original[3]
        assert ui._is_authority_withholding is original[4]
        ui.sanitize_text(PAYLOAD)
    assert ui.sanitize_text is original[0]


def test_a_healthy_redactor_still_redacts_after_every_poison() -> None:
    """Restoration is proven, not assumed.

    A poison that leaked into the process would make every LATER test green for
    the wrong reason, so each mode restores and the next assertion re-reads the
    real behaviour.
    """
    import shared.security as security

    assert security.redact_text(PAYLOAD) == "key=[REDACTED_SECRET]"
    for _label, module_name, attribute, replacement, _outcome in FAILURE_MODES:
        module = _import_target(module_name)
        with _Poison(module, attribute, replacement):
            ui.sanitize_text(PAYLOAD)
        ui._redactor_ok = None
        ui._SANITIZE_WITHHOLDINGS.clear()
        assert ui.sanitize_text(PAYLOAD) == "key=[REDACTED_SECRET]", (
            f"the poison from {attribute} was not restored"
        )


def test_the_liveness_probe_scores_an_authority_failure_as_not_functional() -> None:
    """A failed-closed authority has NOT redacted, and the probe must say so.

    Before this round the probe only asked "did the probe string survive?". An
    authority that returns its own `(detail withheld: …)` marker does not contain
    the probe, so the probe scored that failure as SUCCESS — safe once, and
    wrong in the reporting direction always.
    """
    import shared.security as security

    ui._redactor_ok = None
    assert ui.redactor_is_functional() is True
    with _Poison(security, "normalize_for_redaction", _raising):
        ui._redactor_ok = None
        assert ui.redactor_is_functional() is False
    ui._redactor_ok = None
    assert ui.redactor_is_functional() is True


def test_the_rendered_known_limit_is_disclosed_in_the_module_docstring() -> None:
    """A limit nobody can find in the source is a limit nobody believes.

    The probe's limitation is written into `redactor_is_functional`'s docstring
    and into `cli/ui.py`'s module comment, and this asserts the docstring still
    says so — so deleting the disclosure without fixing the probe goes red.
    """
    doc = ui.redactor_is_functional.__doc__ or ""
    assert "declared limit" in doc.lower(), doc
    assert "undone" in doc.lower(), doc
    assert "122" in doc or "second pattern set" in doc.lower(), doc


# ---------------------------------------------------------------------------
# W2.4 — the error-UX contract.
# ---------------------------------------------------------------------------


#: Every input a failure card can be handed, including the ones nobody would
#: choose to test: empty, `None`, a raw escape, a markup tag, a credential.
CARD_INPUTS: Tuple[object, ...] = (
    "",
    "   ",
    None,
    "x",
    "pytest: 2 failed",
    "connection refused",
    "429 rate limit exceeded",
    'Traceback (most recent call last):\n  File "a.py", line 1\nRuntimeError: boom',
    "\x1b[31mred\x1b[0m",
    "[bold red]evil[/]name",
    PAYLOAD,
    "weird[name].py: 'utf-8' codec can't decode byte 0x9d",
)


class TestTheErrorCardContract:
    """Five assertions Phase 5 inherits when it builds on these cards."""

    def test_1_every_failure_renders_at_least_one_line(self) -> None:
        """Contract 1: a card that renders nothing is the absence of an affordance.

        Checked for EVERY input including empty and `None`, because the moment a
        card has nothing to show is exactly the moment somebody is least able to
        find another surface.
        """
        for value in CARD_INPUTS:
            lines = rv.failure_lines(value, task_id="fix-abc123")
            assert isinstance(lines, list), (value, type(lines))
            assert len(lines) >= 1, f"an empty card for {value!r}"
            assert all(isinstance(line, str) and line for line in lines), value

    def test_2_every_failure_card_names_at_least_one_runnable_command(self) -> None:
        """Contract 2: at least one `/command` on every card.

        The reason is in `cli/runview.py`: a classifier action can be pure
        diagnosis, which is correct advice and useless as an instruction. This
        is the assertion that stops a future "simplification" of the guaranteed
        affordance from making every card advice-only.
        """
        for value in CARD_INPUTS:
            joined = "\n".join(rv.failure_lines(value, task_id="fix-abc123"))
            assert RUNNABLE_COMMAND.search(joined), f"no runnable command for {value!r}"

    def test_3_every_interpolated_value_is_escaped(self) -> None:
        """Contract 3: every interpolated value goes through the escaper.

        Read from the SOURCE rather than observed from the output, because the
        output of a single card cannot tell an escaped value from a value that
        happened to contain no brackets.
        """
        tree = ast.parse(RUNVIEW_PATH.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "failure_lines"
        )
        # The escaper is bound to the local name `escape` and that local IS
        # `_escape`, never `rich.markup.escape` directly.
        bound: list = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "escape":
                        bound.append(ast.unparse(node.value))
        assert bound == ["_escape"], (
            f"failure_lines binds escape to {bound!r}; it must be the "
            "fail-closed helper, not rich.markup.escape"
        )
        assert "from rich.markup import escape" not in ast.unparse(fn), (
            "failure_lines imports rich.markup.escape again, which is the "
            "fail-open version this round removed"
        )
        # And behaviourally: a bracketed value survives VISIBLY rather than
        # being eaten by a style tag.
        card = "\n".join(rv.failure_lines("[bold red]evil[/]name", task_id="t"))
        assert "evil" in card, card
        assert "[bold red]evil" in card, card

    def test_4_every_kind_maps_to_a_distinct_non_empty_action_set(self) -> None:
        """Contract 4: the closed kind vocabulary maps to distinct action sets.

        Two kinds that render the SAME advice are one kind wearing two names, and
        the second name is a maintenance hazard: somebody updates the advice for
        one and not the other. Read from `cli.fileview`'s own table so a new kind
        cannot escape this assertion.
        """
        from cli import fileview

        table = fileview._RECOVERY_ACTIONS_BY_KIND
        assert table, "the failure-kind vocabulary is empty"
        by_actions: dict = {}
        for kind, actions in table.items():
            assert actions, f"{kind} has no actions"
            by_actions.setdefault(tuple(actions), []).append(kind)
        shared = {acts: kinds for acts, kinds in by_actions.items() if len(kinds) > 1}
        assert not shared, (
            "these failure kinds share one action set, so the vocabulary "
            "over-promises what it distinguishes: "
            + "; ".join(f"{kinds} -> {acts}" for acts, kinds in shared.items())
        )
        # And every kind produces a card that is non-empty and runnable.
        for kind in sorted(table):
            lines = rv.failure_lines(f"{kind}: a representative failure", task_id="t")
            assert len(lines) >= 1, kind
            assert RUNNABLE_COMMAND.search("\n".join(lines)), kind

    def test_5_the_card_never_carries_a_traceback_unless_the_failure_is_one(
        self,
    ) -> None:
        """Contract 5: a traceback appears only when the failure IS a traceback.

        Two reasons this matters and they are different. A card that prints a
        traceback for a plain refusal trains a reader to skip the card. A card
        that prints NO traceback for a traceback failure sends the reader to
        `/trace` for something the card already had.
        """
        non_traceback = [
            value
            for value in CARD_INPUTS
            if "Traceback (most recent call last)" not in str(value or "")
        ]
        for value in non_traceback:
            joined = "\n".join(rv.failure_lines(value, task_id="t"))
            assert "Traceback (most recent call last)" not in joined, (
                f"a plain failure rendered a traceback: {value!r} -> {joined!r}"
            )
        real = 'Traceback (most recent call last):\n  File "a.py", line 1\nRuntimeError: boom'
        joined = "\n".join(rv.failure_lines(real, task_id="t"))
        assert joined.strip(), "a traceback failure rendered an empty card"


class TestTheCardFailsClosedRatherThanLeaking:
    """The parts of the contract that were BROKEN and are now pinned."""

    def test_a_credential_in_a_failure_excerpt_never_reaches_the_card(self) -> None:
        """A pytest excerpt is untrusted content, and the card renders it.

        `cli/runview.py::failure_record` truncates the excerpt and
        `failure_lines` escapes it — but escaping is MARKUP safety, not
        credential safety. The card is therefore a display path and is listed in
        `cli/test_render_path_pin.py`'s untrusted table through its producer,
        and the behavioural half of that claim is here.
        """
        card = "\n".join(rv.failure_lines(PAYLOAD, task_id="fix-abc123"))
        assert SECRET_TAG not in card, card
        assert "[REDACTED_SECRET]" in card, card

    def test_a_raising_escaper_withholds_rather_than_raising(self) -> None:
        """THE FIX THIS ROUND MADE. `failure_lines` claimed "pure and total".

        It was not. `rich.markup.escape` is a third-party function on a render
        path and `failure_lines` called it directly, so a raising escaper
        propagated out of the renderer — at the moment of a failure, which is the
        worst moment for a renderer to raise. Measured before the fix:
        `RuntimeError: escaper exploded` escaped `failure_lines`.
        """
        saved = rich.markup.escape

        def boom(_value):
            raise RuntimeError("escaper exploded")

        rich.markup.escape = boom
        try:
            lines = rv.failure_lines(PAYLOAD, task_id="fix-abc123")
        finally:
            rich.markup.escape = saved
        assert isinstance(lines, list) and len(lines) >= 1, lines
        joined = "\n".join(lines)
        assert SECRET_TAG not in joined, f"the withheld path leaked: {joined!r}"
        assert "(value withheld: escaper unavailable)" in joined, joined

    def test_the_runnable_command_survives_a_raising_escaper(self) -> None:
        """Withholding must not cost the card its one instruction.

        The first version of the fix withheld the action rows too, which left a
        six-line card with no `/command` on it — safe, and useless. The
        guaranteed affordance is a product-owned literal with no markup in it, so
        `_escape` short-circuits when there is nothing to escape.
        """
        saved = rich.markup.escape

        def boom(_value):
            raise RuntimeError("escaper exploded")

        rich.markup.escape = boom
        try:
            joined = "\n".join(rv.failure_lines(PAYLOAD, task_id="fix-abc123"))
        finally:
            rich.markup.escape = saved
        assert RUNNABLE_COMMAND.search(joined), joined
        assert SECRET_TAG not in joined, joined

    def test_the_escaper_helper_short_circuits_only_when_there_is_nothing_to_escape(
        self,
    ) -> None:
        """The short-circuit is narrow, and this pins how narrow — by MEASUREMENT.

        A value with no `[` and no backslash cannot open or close a rich style
        tag, so skipping the escaper is a no-op rather than a hole. Anything else
        still goes through it, and the case that WOULD have been eaten is
        asserted here so the shortcut cannot widen.

        The exact characters rich escapes are the AUTHORITY's business, not
        this helper's: `rich.markup.escape` is what runs on the slow path, so
        the assertion compares against `rich.markup.escape` itself rather than
        restating its rules. Restating them would be a second answer to "what
        does markup need escaped", and the second one rots.
        """
        for value in ("plain text", "/doctor for the machine", "42", ""):
            assert rv._escape(value) == value, value
        for value in ("[bold]", "a[b]c", "back\\slash", "name[/]x"):
            assert rv._escape(value) == rich.markup.escape(value), value
        # The property the shortcut rests on: nothing rich would have changed.
        assert rich.markup.escape("[bold]") != "[bold]"

    def test_the_card_is_total_against_a_hostile_classifier(self) -> None:
        """`failure_record` delegates classification; a classifier that raises
        must degrade to `kind: unknown`, not take the card with it.

        A failure card is rendered at the moment something has already gone
        wrong, which is the worst possible time to discover that the card itself
        can raise.
        """
        from cli import fileview

        saved = fileview.classify_failure

        def boom(*_a, **_kw):
            raise RuntimeError("the classifier exploded")

        fileview.classify_failure = boom
        try:
            lines = rv.failure_lines("a representative failure", task_id="t")
        finally:
            fileview.classify_failure = saved
        joined = "\n".join(lines)
        assert len(lines) >= 1
        assert RUNNABLE_COMMAND.search(joined), joined
        assert "classifier unavailable" in joined, joined


def test_the_two_cards_agree_on_the_facts() -> None:
    """`failure_record` and `failure_lines` read the same record.

    They are two renderings of one thing, and a card that disagrees with the
    record it renders is a card a reader cannot act on.
    """
    record = rv.failure_record("pytest: 2 failed", task_id="fix-abc123")
    lines = rv.failure_lines("pytest: 2 failed", task_id="fix-abc123")
    joined = "\n".join(lines)
    assert str(record["kind"]) in joined, (record["kind"], joined)
    assert "fix-abc123" in joined, joined


def test_the_failure_kinds_are_read_from_the_classifier_not_restated() -> None:
    """The vocabulary is DERIVED, so a new kind cannot escape the contract.

    A hard-coded copy of the kind list in this file would be a second answer to
    "what can go wrong", and the second one rots.
    """
    from cli import fileview

    assert _failure_kinds() == tuple(sorted(fileview._RECOVERY_ACTIONS_BY_KIND))
    assert len(_failure_kinds()) >= 15, _failure_kinds()
