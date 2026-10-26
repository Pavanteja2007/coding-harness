"""Long-horizon scaffolding: the initializer, the JSON feature ledger, and the
session discipline that makes a multi-session task survivable.

This module exists because of one measured finding. Anthropic's documented
result is the sharpest statement of the problem available: *"Compaction isn't
sufficient. Out of the box, even a frontier coding model ... running on the
Claude Agent SDK in a loop across multiple context windows **will fall short of
building a production-quality web app** if it's only given a high-level
prompt."* The mechanism they landed on was an **initializer agent** that
scaffolds on the first run, plus a feature ledger whose features all start
**failing**, plus one-feature-per-session.

Four design decisions here are the whole content of the module, and each is
about what the AGENT cannot be trusted with:

1. **The feature ledger is JSON, not Markdown, and that is not a style
   choice.** Anthropic reports the reason empirically: *"After some
   experimentation, we landed on using JSON, as the model is less likely to
   inappropriately change or overwrite JSON files compared to Markdown
   files."* This repository already agrees from a different direction --
   ``harness/config.py`` is a table merged into every task and
   ``state.json`` is the Boundary-4 contract, both JSON, both read-only to
   the agent. Rule: **anything the agent must not rewrite is JSON. Anything
   the agent is supposed to append to is Markdown.**

2. **The agent can change the ``status`` field and nothing else. This is
   enforced STRUCTURALLY, not by prompt.** :data:`AGENT_WRITABLE_LEDGER_FIELDS`
   is the single definition, :func:`apply_ledger_patch` is the only writer a
   tool or an agent reaches, and it refuses every other key by name. An agent
   that can rewrite its own success criteria can rewrite its own grade, so the
   criteria are not the agent's to edit.

3. **A feature marked ``passing`` requires its verification to have RUN.**
   The agent's claim is not evidence. :func:`mark_feature_status` refuses a
   ``passing`` transition with no :class:`VerificationRun` behind it, and the
   refusal is the same class of refusal as the field restriction above: a
   mechanism, not a reminder.

4. **A feature with no verification method is not a feature.** It is a wish.
   :func:`Feature` construction refuses an empty ``verification``, so the
   ledger cannot come into existence holding one.

Everything here is deliberately **total**: nothing raises out of the public
entry points except where a refusal *is* the answer, and every refusal is a
named, renderable value carrying a reason and an owner. A ledger writer that
raises on a model's bad JSON would kill the run over a formatting detail; a
ledger writer that silently accepts one would let the agent grade itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The closed vocabularies. Every one of these is a CLOSED SET, because a
# vocabulary nobody bounded is a vocabulary nobody can count.
# ---------------------------------------------------------------------------

#: The ledger's on-disk schema id. Bumped only by an incompatible change, and
#: a document carrying an unknown id is REFUSED WHOLE rather than half-read --
#: half-applying a ledger is how a run silently loses a feature.
FEATURE_LEDGER_SCHEMA = "neo/feature-ledger/v1"

#: The progress ledger's schema id, for the same reason.
PROGRESS_SCHEMA = "neo/progress-ledger/v1"

#: Every feature starts here. A ledger that ships a feature already passing is
#: a ledger whose author wrote down a grade instead of a goal.
FEATURE_STATUSES: Tuple[str, ...] = ("failing", "passing", "blocked", "skipped")

#: The status a feature is born with and the status a ``passing`` claim is
#: measured against.
INITIAL_FEATURE_STATUS = "failing"
PASSING_FEATURE_STATUS = "passing"

#: **The single definition of what the agent may write.** One field. Every
#: enforcement point in this module and its pins reads THIS tuple, so widening
#: it is one visible edit rather than a search.
AGENT_WRITABLE_LEDGER_FIELDS: Tuple[str, ...] = ("status",)

#: How a feature can be checked. ``command`` is a runnable check, ``e2e`` is
#: the end-to-end operation (:func:`run_end_to_end_verification`), and
#: ``human_check`` is an explicit prose answer to "how would a human check
#: this?" -- which is a legitimate method and is NOT a run.
VERIFICATION_METHODS: Tuple[str, ...] = ("command", "e2e", "human_check")

#: How a verification RUN ended. ``not_run`` is a real value and not a synonym
#: for ``fail``: "we did not check" and "we checked and it broke" are different
#: facts, and the ledger keeps them apart.
#:
#: ``flaky`` is P4/W2-T1, added because the eight-scenario pressure attack
#: produced a FALSE PASS without it. A check that passed on the first attempt and
#: failed on the rerun had to be recorded as ``pass`` with the flakiness buried in
#: a prose ``note``, because there was no other way to say it -- and a row reading
#: ``outcome="pass"`` is pass evidence. ``is_pass_evidence`` requires
#: ``outcome == "pass"``, so adding the word here is sufficient: a flaky check now
#: cannot be a pass at all. The repository has said since Phase 2 that flaky is
#: not a pass; this is where that rule acquires a representation.
VERIFICATION_OUTCOMES: Tuple[str, ...] = ("pass", "fail", "error", "not_run", "flaky")

#: The two kinds of evidence a ``passing`` claim can rest on. ``run`` is a
#: verification that executed. ``attested`` is a human saying so, which is real
#: evidence about a human_check feature and is NOT evidence for a command.
EVIDENCE_KINDS: Tuple[str, ...] = ("run", "attested")

#: Whether a ``passing`` claim on ATTESTED-only evidence is refused by default.
#: It is. A command-verified feature whose "run" is a human's word is exactly
#: the substitution Phase 2's verifier gate exists to prevent, and the escape
#: hatch is a named argument rather than a silent default.
FEATURE_PASSING_REQUIRES_RUN = True

#: The initializer's run script name. Anthropic: *"init.sh ... This saves
#: Claude some tokens in every session since it doesn't have to figure out how
#: to test the code."*
INIT_SCRIPT_NAME = "init.sh"

#: The progress ledger's file name. Markdown ON PURPOSE: this is the one
#: artifact the agent is *supposed* to append to every session. See rule 1.
PROGRESS_LEDGER_NAME = "PROGRESS.md"

#: The feature ledger's file name.
FEATURE_LEDGER_NAME = "features.json"

#: The refusals this module can return, as a closed set. A reader can branch
#: on the reason without parsing prose.
LEDGER_REFUSALS: Tuple[str, ...] = (
    "unknown_schema",
    "malformed_document",
    "not_a_feature",
    "unknown_feature",
    "field_not_writable",
    "status_not_in_vocabulary",
    "unknown_status_value",
    "verification_required",
    "verification_method_required",
    "no_such_verification",
    "verification_did_not_pass",
    "status_already_set",
    "one_feature_per_session",
    "no_feature_claimed",
    "test_tampering",
    "artifacts_off_root",
    "initializer_degraded",
)

#: The closed set of reasons a session's changes may be refused for touching
#: tests. Anthropic's own words: *"It is unacceptable to remove or edit tests
#: because this could lead to missing or buggy functionality."*
TEST_INTEGRITY_REFUSALS: Tuple[str, ...] = (
    "test_removed",
    "test_edited",
    "test_renamed_away",
    "verdict_defeated",
)

__all__ = [
    "AGENT_WRITABLE_LEDGER_FIELDS",
    "EVIDENCE_KINDS",
    "FEATURE_LEDGER_NAME",
    "FEATURE_LEDGER_SCHEMA",
    "FEATURE_PASSING_REQUIRES_RUN",
    "FEATURE_STATUSES",
    "INITIAL_FEATURE_STATUS",
    "INIT_SCRIPT_NAME",
    "LEDGER_REFUSALS",
    "PASSING_FEATURE_STATUS",
    "PROGRESS_LEDGER_NAME",
    "PROGRESS_SCHEMA",
    "TEST_INTEGRITY_REFUSALS",
    "VERIFICATION_METHODS",
    "VERIFICATION_OUTCOMES",
    "ArtifactPointer",
    "EndToEndReport",
    "EndToEndSpec",
    "EndToEndStepResult",
    "Feature",
    "FeatureLedger",
    "InitializerReceipt",
    "LedgerRefusal",
    "LongHorizonSession",
    "ProjectProfile",
    "TestIntegrityVerdict",
    "VerificationMethod",
    "VerificationRun",
    "apply_ledger_patch",
    "check_verification_evidence",
    "end_to_end_report",
    "feature_ledger_coverage",
    "feature_status_summary",
    "initialize_project",
    "load_feature_ledger",
    "load_progress_ledger",
    "mark_feature_status",
    "new_feature",
    "new_feature_ledger",
    "render_feature_ledger",
    "render_init_script",
    "render_progress_ledger",
    "render_session_brief",
    "run_end_to_end_verification",
    "run_feature_verification",
    "run_git",
    "session_end_receipt",
    "verify_test_integrity",
    "write_feature_ledger",
    "write_progress_ledger",
]


# ---------------------------------------------------------------------------
# Refusals. A refusal is a VALUE with a reason and an owner, never an
# exception a caller has to catch to keep running.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRefusal:
    """One named refusal: what was asked, why it was refused, who owns it.

    ``owner`` is load-bearing for this phase specifically. Every mechanism here
    is a harness-side implementation of a rule the project states elsewhere --
    test-removal is refused by ``phases/DOCTRINE.md``'s spirit and by Phase 2's
    floor layer, and Phase 4's floor layer is T3's. Naming the owner on the
    refusal is how a defence-in-depth check stops reading as the enforcement.
    """

    reason: str
    detail: str
    subject: str = ""
    owner: str = "T1 / harness"

    @property
    def ok(self) -> bool:
        """Return whether this is a pass. A refusal is never a pass."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this refusal."""
        return {
            "reason": self.reason,
            "detail": self.detail,
            "subject": self.subject,
            "owner": self.owner,
        }

    def render(self) -> str:
        """Return the one quotable line naming the refusal and its owner."""
        subject = f" [{self.subject}]" if self.subject else ""
        return f"REFUSED ({self.reason}){subject}: {self.detail}  [owner: {self.owner}]"


def _refused(reason: str, detail: str, subject: str = "", owner: str = "T1 / harness") -> LedgerRefusal:
    """Build a refusal, refusing an unknown reason rather than inventing one.

    A vocabulary nobody bounded is a vocabulary nobody can count, so an
    unrecognised reason is itself a refusal. It raises, because a call site
    that invents a reason has a bug that a value would hide.
    """
    if reason not in LEDGER_REFUSALS:
        raise ValueError(f"unknown refusal reason: {reason}")
    return LedgerRefusal(reason=reason, detail=str(detail), subject=str(subject), owner=owner)


# ---------------------------------------------------------------------------
# Verification. A feature is only as real as the thing that checks it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationMethod:
    """HOW a feature is checked. Never empty -- that is the whole point.

    ``command`` is a runnable check. ``e2e`` names an end-to-end operation
    (see :func:`run_end_to_end_verification`). ``human_check`` carries the
    explicit prose answer to "how would a human check this?" -- Anthropic's
    phrasing of the requirement, and a legitimate method for a feature no
    command can observe.

    The two fields coexist so a receipt can say WHICH of them it used. A
    feature verified by a human is not a feature verified by a command, and a
    reader who cannot tell the difference is reading a lie.
    """

    method: str = "command"
    command: str = ""
    human_check: str = ""
    e2e_spec: Optional["EndToEndSpec"] = None
    timeout_s: float = 300.0

    def __post_init__(self) -> None:
        method = str(self.method or "").strip().lower()
        if method not in VERIFICATION_METHODS:
            raise ValueError(
                f"verification method must be one of {VERIFICATION_METHODS}, got {method!r}"
            )
        object.__setattr__(self, "method", method)
        if method == "command":
            if not str(self.command or "").strip():
                raise ValueError(
                    "a command verification needs a command; a feature with no "
                    "verification is not a feature, it is a wish"
                )
            object.__setattr__(self, "human_check", "")
        elif method == "human_check":
            if not str(self.human_check or "").strip():
                raise ValueError(
                    "a human_check verification needs the human's procedure; an "
                    "empty one cannot be checked by anyone"
                )
            object.__setattr__(self, "command", "")
        elif method == "e2e":
            if self.e2e_spec is None:
                raise ValueError("an e2e verification needs an EndToEndSpec")

    @property
    def runnable(self) -> bool:
        """Return whether this method can produce a ``run``-kind evidence row."""
        return self.method in ("command", "e2e")

    def describe(self) -> str:
        """Return the one line a reader needs to reproduce this check."""
        if self.method == "command":
            return f"command: {self.command}"
        if self.method == "e2e":
            return "e2e: " + (self.e2e_spec.describe() if self.e2e_spec else "(no spec)")
        return f"human check: {self.human_check}"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this method."""
        return {
            "method": self.method,
            "command": self.command,
            "human_check": self.human_check,
            "e2e": self.e2e_spec.to_dict() if self.e2e_spec is not None else None,
            "timeout_s": float(self.timeout_s),
        }


@dataclass(frozen=True)
class VerificationRun:
    """One attempt at a feature's verification. A record of a RUN, not a claim.

    ``ran`` is separate from ``outcome`` on purpose. ``outcome == "not_run"``
    with ``ran is False`` means nobody checked; ``outcome == "fail"`` with
    ``ran is True`` means somebody checked and it broke. A ledger that
    collapsed them would let a ``passing`` claim rest on either.
    """

    feature_id: str
    method: str = "command"
    command: str = ""
    outcome: str = "not_run"
    ran: bool = False
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    output_tail: str = ""
    started_at: str = ""
    kind: str = "run"
    note: str = ""

    def __post_init__(self) -> None:
        outcome = str(self.outcome or "not_run").strip().lower()
        if outcome not in VERIFICATION_OUTCOMES:
            raise ValueError(f"outcome must be one of {VERIFICATION_OUTCOMES}")
        kind = str(self.kind or "run").strip().lower()
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {EVIDENCE_KINDS}")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "kind", kind)
        if kind == "attested":
            object.__setattr__(self, "ran", False)
            object.__setattr__(self, "outcome", "not_run")

    @property
    def is_pass_evidence(self) -> bool:
        """Return whether this row may support a ``passing`` claim."""
        return bool(self.ran and self.outcome == "pass")

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this run."""
        return {
            "feature_id": self.feature_id,
            "method": self.method,
            "command": self.command,
            "outcome": self.outcome,
            "ran": bool(self.ran),
            "exit_code": self.exit_code,
            "duration_s": round(float(self.duration_s), 4),
            "output_tail": self.output_tail,
            "started_at": self.started_at,
            "kind": self.kind,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerificationRun":
        """Rebuild a run from its JSON projection, tolerating an absent key."""
        data = dict(value or {})
        return cls(
            feature_id=str(data.get("feature_id") or ""),
            method=str(data.get("method") or "command"),
            command=str(data.get("command") or ""),
            outcome=str(data.get("outcome") or "not_run"),
            ran=bool(data.get("ran", False)),
            exit_code=(
                int(data["exit_code"]) if data.get("exit_code") is not None else None
            ),
            duration_s=float(data.get("duration_s") or 0.0),
            output_tail=str(data.get("output_tail") or ""),
            started_at=str(data.get("started_at") or ""),
            kind=str(data.get("kind") or "run"),
            note=str(data.get("note") or ""),
        )


@dataclass(frozen=True)
class EndToEndStepResult:
    """One executed end-to-end step."""

    name: str
    phase: str  # setup | drive | teardown
    command: str
    exit_code: Optional[int] = None
    ran: bool = False
    duration_s: float = 0.0
    output_tail: str = ""

    @property
    def ok(self) -> bool:
        """Return whether this step ran and succeeded."""
        return bool(self.ran and self.exit_code == 0)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this step."""
        return {
            "name": self.name,
            "phase": self.phase,
            "command": self.command,
            "exit_code": self.exit_code,
            "ran": bool(self.ran),
            "duration_s": round(float(self.duration_s), 4),
            "output_tail": self.output_tail,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class EndToEndReport:
    """The end-to-end operation's receipt.

    **This is the gap T4.W1.4 asks about, answered.** ``execution.verify``
    answers "did the tests pass" -- a unit-and-suite question. Anthropic's named
    failure is different: the model *"would fail to recognize that the feature
    didn't work end-to-end"*, and the fix was *"providing Claude with these kinds
    of testing tools"*. So the end-to-end operation is a first-class thing here:
    setup steps, drive steps, teardown steps, each with its own exit code, and
    a receipt that says ``teardown_ran`` because an abandoned dev server is a
    leaked process and a leaked process makes the NEXT session's result
    unreproducible.
    """

    feature_id: str
    ran: bool = False
    outcome: str = "not_run"
    steps: Tuple[EndToEndStepResult, ...] = ()
    teardown_ran: bool = False
    teardown_error: str = ""
    duration_s: float = 0.0
    available: bool = True
    unavailable_reason: str = ""

    @property
    def ok(self) -> bool:
        """Return whether the end-to-end operation ran and every step passed."""
        return bool(self.ran and self.outcome == "pass" and self.teardown_ran)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this report."""
        return {
            "feature_id": self.feature_id,
            "ran": bool(self.ran),
            "outcome": self.outcome,
            "steps": [step.to_dict() for step in self.steps],
            "teardown_ran": bool(self.teardown_ran),
            "teardown_error": self.teardown_error,
            "duration_s": round(float(self.duration_s), 4),
            "available": bool(self.available),
            "unavailable_reason": self.unavailable_reason,
            "ok": self.ok,
        }


def end_to_end_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a JSON projection of an :class:`EndToEndReport` mapping.

    The CLI/TUI surface reads receipts, not objects. This is the one place the
    report crosses that boundary, so the ``not_run`` vs ``fail`` distinction is
    made exactly once.
    """
    data = dict(report or {})
    return {
        "feature_id": str(data.get("feature_id") or ""),
        "ran": bool(data.get("ran", False)),
        "outcome": str(data.get("outcome") or "not_run"),
        "teardown_ran": bool(data.get("teardown_ran", False)),
        "available": bool(data.get("available", True)),
        "unavailable_reason": str(data.get("unavailable_reason") or ""),
        "ok": bool(data.get("ok", False)),
    }


# ---------------------------------------------------------------------------
# End-to-end verification as an OPERATION, not a description.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndToEndSpec:
    """A declared end-to-end check: start it, drive it, tear it down.

    The three phases exist because the failure this closes is not "the unit
    tests passed". It is "the feature does not work in the running product".
    Expressing that as three command phases makes it a thing a harness runs and
    a receipt records, rather than a thing the model is asked to remember.

    Assumes the injected runner executes a command string in the repository
    and returns ``(exit_code, output_tail, duration_s)``. Nothing here runs a
    process itself -- see :func:`run_end_to_end_verification`.
    """

    setup: Tuple[str, ...] = ()
    drive: Tuple[str, ...] = ()
    teardown: Tuple[str, ...] = ()
    feature_id: str = ""
    timeout_s: float = 600.0

    def __post_init__(self) -> None:
        for name in ("setup", "drive", "teardown"):
            value = tuple(str(item) for item in (getattr(self, name) or ()) if str(item).strip())
            object.__setattr__(self, name, value)
        if not self.drive:
            raise ValueError(
                "an end-to-end spec needs at least one drive step; setup alone "
                "does not check that the feature works"
            )

    def phases(self) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        """Return ``(phase, commands)`` in execution order, teardown LAST."""
        return (
            ("setup", self.setup),
            ("drive", self.drive),
            ("teardown", self.teardown),
        )

    def commands(self) -> Tuple[str, ...]:
        """Return every command this spec runs, in execution order."""
        out: List[str] = []
        for _phase, commands in self.phases():
            out.extend(commands)
        return tuple(out)

    def describe(self) -> str:
        """Return the one line naming the phases, so a receipt is reproducible."""
        parts = [
            f"{phase}[{len(commands)}]" for phase, commands in self.phases() if commands
        ]
        return "setup/drive/teardown " + " ".join(parts) if parts else "(empty)"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this spec."""
        return {
            "feature_id": self.feature_id,
            "setup": list(self.setup),
            "drive": list(self.drive),
            "teardown": list(self.teardown),
            "timeout_s": float(self.timeout_s),
        }


def run_end_to_end_verification(
    spec: EndToEndSpec,
    *,
    runner: Optional[Any] = None,
    clock: Optional[Any] = None,
) -> EndToEndReport:
    """Run a declared end-to-end check and return a receipt.

    **The teardown runs even when a drive step fails.** That is the whole reason
    this is a function and not three calls a caller could forget: a dev server
    left running makes the next session's result unreproducible, and an
    unreproducible session is exactly what this phase exists to prevent.

    ``runner`` is injected and is REQUIRED for anything to actually execute.
    With no runner the report is ``available: False`` with a reason -- never
    ``ran: True`` on an assertion. The runner's signature is
    ``(command, timeout_s) -> (exit_code, output_tail, duration_s)``; a runner
    that does not declare ``timeout_s`` is called without it, following this
    repository's established ``_boundary_names`` rule (a permissive signature
    is not evidence of support).
    """
    import inspect

    if runner is None:
        return EndToEndReport(
            feature_id=spec.feature_id,
            ran=False,
            outcome="not_run",
            available=False,
            unavailable_reason=(
                "no end-to-end runner is bound, so nothing was checked; this is "
                "NOT a claim that the feature does or does not work"
            ),
        )

    accepts_timeout = "timeout_s" in inspect.signature(runner).parameters
    steps: List[EndToEndStepResult] = []
    teardown_ran = False
    teardown_error = ""
    started = (clock() if callable(clock) else 0.0) or 0.0
    elapsed = 0.0
    drive_failed = False
    drive_ran = False

    for phase, commands in spec.phases():
        for index, command in enumerate(commands):
            # Teardown is unconditional; a failing drive step does not skip it.
            if phase != "teardown" and drive_failed:
                break
            try:
                if accepts_timeout:
                    outcome = runner(command, timeout_s=spec.timeout_s)
                else:
                    outcome = runner(command)
            except Exception as exc:  # a broken runner is an error, not a pass
                result = EndToEndStepResult(
                    name=f"{phase}[{index}]",
                    phase=phase,
                    command=command,
                    exit_code=None,
                    ran=False,
                    output_tail=f"{type(exc).__name__}: {exc}"[:400],
                )
                steps.append(result)
                elapsed += result.duration_s
                if phase == "drive":
                    drive_failed = True
                elif phase == "teardown":
                    teardown_error = result.output_tail
                continue
            exit_code, output_tail, duration_s = _unpack_runner_outcome(outcome)
            result = EndToEndStepResult(
                name=f"{phase}[{index}]",
                phase=phase,
                command=command,
                exit_code=exit_code,
                ran=True,
                duration_s=float(duration_s or 0.0),
                output_tail=str(output_tail or "")[:2000],
            )
            steps.append(result)
            elapsed += result.duration_s
            if phase == "drive":
                drive_ran = True
                if not result.ok:
                    drive_failed = True
            elif phase == "teardown":
                teardown_ran = True

    if not drive_ran:
        outcome = "not_run"
    elif drive_failed:
        outcome = "fail"
    else:
        outcome = "pass"
    return EndToEndReport(
        feature_id=spec.feature_id,
        ran=drive_ran,
        outcome=outcome,
        steps=tuple(steps),
        teardown_ran=teardown_ran or not spec.teardown,
        teardown_error=teardown_error,
        duration_s=elapsed or ((clock() - started) if callable(clock) else 0.0),
        available=True,
    )


def _unpack_runner_outcome(outcome: Any) -> Tuple[Optional[int], str, float]:
    """Return ``(exit_code, output_tail, duration_s)`` from a runner's answer.

    Tolerates a tuple of 2 or 3, and an object with ``exit_code``/
    ``output``. A runner that returns something unrecognisable is
    ``(None, str(value), 0.0)`` -- which reads as an ERROR step, never as a
    pass, because :attr:`EndToEndStepResult.ok` requires ``exit_code == 0``.
    """
    if isinstance(outcome, Mapping):
        return (
            outcome.get("exit_code"),
            str(outcome.get("output") or outcome.get("output_tail") or ""),
            float(outcome.get("duration_s") or 0.0),
        )
    if isinstance(outcome, (tuple, list)):
        items = list(outcome)
        if len(items) >= 3:
            return (items[0], str(items[1]), float(items[2] or 0.0))
        if len(items) == 2:
            return (items[0], str(items[1]), 0.0)
        if len(items) == 1:
            return (items[0], "", 0.0)
        return (None, "", 0.0)
    exit_code = getattr(outcome, "exit_code", None)
    if exit_code is not None:
        return (
            exit_code,
            str(getattr(outcome, "output", "") or getattr(outcome, "text", "") or ""),
            float(getattr(outcome, "duration_s", 0.0) or 0.0),
        )
    if isinstance(outcome, int):
        return (outcome, "", 0.0)
    return (None, f"runner returned an unrecognised value: {outcome!r}"[:200], 0.0)


def run_feature_verification(
    feature_id: str,
    method: VerificationMethod,
    *,
    runner: Optional[Any] = None,
    e2e_runner: Optional[Any] = None,
    clock: Optional[Any] = None,
    started_at: str = "",
) -> Tuple[VerificationRun, Optional[EndToEndReport]]:
    """Run one feature's verification and return ``(run, e2e_report_or_None)``.

    A ``human_check`` produces an ``attested``-shaped row with
    ``ran=False, outcome="not_run"`` and a note naming the human procedure. It
    is NOT counted as a run, and :func:`check_verification_evidence` is the
    place that refuses to let it carry a command-verified feature.

    The e2e report is returned separately because a feature may have BOTH a
    unit method and an end-to-end check, and the two receipts must not be
    merged into one boolean.
    """
    if method.method == "human_check":
        return (
            VerificationRun(
                feature_id=feature_id,
                method="human_check",
                command=method.human_check,
                outcome="not_run",
                ran=False,
                started_at=started_at,
                kind="attested",
                note=(
                    "this feature is verified by a human procedure, not by a "
                    "command; a passing claim on it is ATTESTED, not RUN"
                ),
            ),
            None,
        )

    if method.method == "e2e":
        report = run_end_to_end_verification(
            method.e2e_spec, runner=e2e_runner or runner, clock=clock
        )
        run = VerificationRun(
            feature_id=feature_id,
            method="e2e",
            command="; ".join(method.e2e_spec.commands()) if method.e2e_spec else "",
            outcome=report.outcome,
            ran=report.ran,
            exit_code=0 if report.outcome == "pass" else (1 if report.ran else None),
            output_tail=(
                "" if report.ok else f"end-to-end {report.outcome}: {report.unavailable_reason}"
            ),
            started_at=started_at,
            kind="run",
            note=f"steps={len(report.steps)} teardown_ran={report.teardown_ran}",
        )
        return run, report

    if runner is None:
        return (
            VerificationRun(
                feature_id=feature_id,
                method="command",
                command=method.command,
                outcome="not_run",
                ran=False,
                started_at=started_at,
                kind="run",
                note="no verification runner is bound; nothing was checked",
            ),
            None,
        )

    import inspect

    accepts_timeout = "timeout_s" in inspect.signature(runner).parameters
    try:
        if accepts_timeout:
            outcome = runner(method.command, timeout_s=method.timeout_s)
        else:
            outcome = runner(method.command)
    except Exception as exc:
        return (
            VerificationRun(
                feature_id=feature_id,
                method="command",
                command=method.command,
                outcome="error",
                ran=False,
                started_at=started_at,
                kind="run",
                output_tail=f"{type(exc).__name__}: {exc}"[:400],
            ),
            None,
        )
    exit_code, output_tail, duration_s = _unpack_runner_outcome(outcome)
    return (
        VerificationRun(
            feature_id=feature_id,
            method="command",
            command=method.command,
            outcome="pass" if exit_code == 0 else ("fail" if exit_code is not None else "error"),
            ran=exit_code is not None,
            exit_code=exit_code,
            duration_s=float(duration_s or 0.0),
            output_tail=str(output_tail or "")[:2000],
            started_at=started_at,
            kind="run",
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Features and the ledger.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Feature:
    """One requirement, its verification, and its status.

    Frozen on purpose. Every status change produces a NEW ``Feature`` via
    :func:`mark_feature_status`; there is no ``feature.status = "passing"``
    anywhere in this module, and the agent-facing writer
    (:func:`apply_ledger_patch`) rebuilds rather than mutates. That is what
    makes "the agent can change only the status field" a property of the data
    rather than a promise in a prompt.
    """

    id: str
    description: str = ""
    verification: VerificationMethod = field(
        default_factory=lambda: VerificationMethod(method="human_check", human_check="(unset)")
    )
    status: str = INITIAL_FEATURE_STATUS
    evidence: Tuple[VerificationRun, ...] = ()
    sessions_touched: Tuple[str, ...] = ()
    notes: str = ""
    derived_from: str = ""

    def __post_init__(self) -> None:
        identifier = str(self.id or "").strip()
        if not identifier:
            raise ValueError("a feature needs an id")
        object.__setattr__(self, "id", identifier)
        status = str(self.status or INITIAL_FEATURE_STATUS).strip().lower()
        if status not in FEATURE_STATUSES:
            raise ValueError(f"status must be one of {FEATURE_STATUSES}, got {status!r}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "evidence", tuple(self.evidence or ()))
        object.__setattr__(self, "sessions_touched", tuple(self.sessions_touched or ()))

    @property
    def verification_ran(self) -> bool:
        """Return whether a verification has actually EXECUTED for this feature."""
        return any(bool(run.ran) for run in self.evidence)

    @property
    def evidence_kind(self) -> str:
        """Return ``run``, ``attested``, or ``""`` -- never guessed.

        A feature with evidence is one or the other. Reporting a default of
        ``"run"`` for a feature whose only evidence is a human's word is the
        exact substitution the ledger exists to prevent.
        """
        if not self.evidence:
            return ""
        kinds = {run.kind for run in self.evidence if run.ran or run.kind == "attested"}
        if not kinds:
            return ""
        if "run" in kinds:
            return "run"
        return "attested"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection, including read-only fields."""
        return {
            "id": self.id,
            "description": self.description,
            "verification": self.verification.to_dict(),
            "status": self.status,
            "evidence": [run.to_dict() for run in self.evidence],
            "sessions_touched": list(self.sessions_touched),
            "notes": self.notes,
            "derived_from": self.derived_from,
            "verification_ran": self.verification_ran,
            "evidence_kind": self.evidence_kind,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Feature":
        """Rebuild a feature from its JSON projection.

        Refuses a feature with no usable verification by raising, so a corrupt
        document cannot half-load into a ledger holding a feature nobody can
        check.
        """
        data = dict(value or {})
        raw = dict(data.get("verification") or {})
        e2e_raw = raw.get("e2e")
        e2e_spec = None
        if isinstance(e2e_raw, Mapping):
            e2e_spec = EndToEndSpec(
                setup=tuple(e2e_raw.get("setup") or ()),
                drive=tuple(e2e_raw.get("drive") or ()),
                teardown=tuple(e2e_raw.get("teardown") or ()),
                feature_id=str(e2e_raw.get("feature_id") or data.get("id") or ""),
                timeout_s=float(e2e_raw.get("timeout_s") or 600.0),
            )
        method = str(raw.get("method") or "").strip().lower()
        if method == "human_check" or (not method and raw.get("human_check")):
            verification = VerificationMethod(
                method="human_check",
                human_check=str(raw.get("human_check") or "(unspecified human check)"),
            )
        elif method == "e2e" or e2e_spec is not None:
            verification = VerificationMethod(
                method="e2e", e2e_spec=e2e_spec or EndToEndSpec(drive=("true",))
            )
        else:
            verification = VerificationMethod(
                method="command", command=str(raw.get("command") or "")
            )
        return cls(
            id=str(data.get("id") or ""),
            description=str(data.get("description") or ""),
            verification=verification,
            status=str(data.get("status") or INITIAL_FEATURE_STATUS),
            evidence=tuple(
                VerificationRun.from_dict(item) for item in (data.get("evidence") or ())
            ),
            sessions_touched=tuple(str(item) for item in (data.get("sessions_touched") or ())),
            notes=str(data.get("notes") or ""),
            derived_from=str(data.get("derived_from") or ""),
        )


def new_feature(
    feature_id: str,
    description: str,
    verification: VerificationMethod,
    *,
    derived_from: str = "",
) -> Feature:
    """Build one feature, born ``failing``.

    The birth status is not a default a caller can pass: a feature cannot be
    constructed already passing, because the only way that value could exist
    is if something already claimed it was verified.
    """
    return Feature(
        id=str(feature_id),
        description=str(description),
        verification=verification,
        status=INITIAL_FEATURE_STATUS,
        derived_from=str(derived_from),
    )


@dataclass(frozen=True)
class FeatureLedger:
    """The whole ledger. JSON on disk; every field below is read-only to the
    agent except ``features[*].status`` (and the evidence a harness run
    appends beside it)."""

    schema: str = FEATURE_LEDGER_SCHEMA
    project_id: str = ""
    request: str = ""
    created_at: str = ""
    features: Tuple[Feature, ...] = ()
    sessions: Tuple[str, ...] = ()
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", tuple(self.features or ()))
        object.__setattr__(self, "sessions", tuple(self.sessions or ()))

    def feature(self, feature_id: str) -> Optional[Feature]:
        """Return one feature by id, or ``None``."""
        for item in self.features:
            if item.id == str(feature_id):
                return item
        return None

    def with_status(self, feature_id: str, status: str, evidence: Sequence[VerificationRun]) -> "FeatureLedger":
        """Return a NEW ledger with one feature's status replaced.

        No mutation, no in-place assignment, and ``evidence`` is REPLACED by
        the caller-supplied sequence rather than appended to whatever was there:
        a caller cannot smuggle in an extra row by passing a longer list than
        the feature previously had, because the list it passes is the list that
        lands.
        """
        updated = tuple(
            replace(item, status=status, evidence=tuple(evidence))
            if item.id == str(feature_id)
            else item
            for item in self.features
        )
        return replace(self, features=updated, digest="")

    def to_dict(self) -> Dict[str, Any]:
        """Return the ledger's on-disk JSON projection."""
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "request": self.request,
            "created_at": self.created_at,
            "sessions": list(self.sessions),
            "features": [item.to_dict() for item in self.features],
        }

    def render(self) -> str:
        """Return the canonical on-disk text.

        Sorted keys and two-space indent, so a ledger written twice from the
        same state is byte-identical -- which is what makes "did the agent
        change anything it was not allowed to" answerable with a diff.
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def new_feature_ledger(
    project_id: str,
    request: str,
    features: Sequence[Feature],
    *,
    created_at: str = "",
    sessions: Sequence[str] = (),
) -> FeatureLedger:
    """Build a fresh ledger from features that are all still ``failing``."""
    return FeatureLedger(
        schema=FEATURE_LEDGER_SCHEMA,
        project_id=str(project_id),
        request=str(request),
        created_at=str(created_at),
        features=tuple(features),
        sessions=tuple(str(item) for item in (sessions or ())),
    )


def render_feature_ledger(ledger: FeatureLedger) -> str:
    """Return the ledger's canonical on-disk text (the single renderer)."""
    return ledger.render()


def write_feature_ledger(path: Any, ledger: FeatureLedger) -> Dict[str, Any]:
    """Write the ledger atomically and return a receipt.

    Atomic (tmp + ``os.replace``) because a ledger truncated by a crash is a
    ledger whose features have silently vanished, and the whole point of the
    artifact is that it survives the session that wrote it.
    """
    text = ledger.render()
    receipt = _atomic_write_text(path, text)
    receipt["schema"] = ledger.schema
    receipt["features"] = len(ledger.features)
    receipt["digest"] = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return receipt


def load_feature_ledger(path: Any) -> Tuple[Optional[FeatureLedger], Optional[LedgerRefusal]]:
    """Load a ledger, or return a named refusal.

    Returns a tuple rather than raising: a run whose ledger is corrupt should
    be able to say so and continue with a fresh one, and a loader that raises
    would take the run down over a formatting detail. An unknown schema is
    refused WHOLE, never half-read.
    """
    try:
        raw = _read_text(path)
    except FileNotFoundError:
        return None, _refused(
            "malformed_document",
            f"no feature ledger at {path}; an initializer must write one",
            subject=str(path),
        )
    except OSError as exc:
        return None, _refused(
            "malformed_document", f"feature ledger unreadable: {exc}", subject=str(path)
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        return None, _refused(
            "malformed_document", f"feature ledger is not valid JSON: {exc}", subject=str(path)
        )
    if not isinstance(document, Mapping):
        return None, _refused(
            "malformed_document", "feature ledger is not a JSON object", subject=str(path)
        )
    schema = str(document.get("schema") or "")
    if schema != FEATURE_LEDGER_SCHEMA:
        return None, _refused(
            "unknown_schema",
            f"feature ledger schema {schema!r} is not {FEATURE_LEDGER_SCHEMA!r}; "
            "refused whole rather than half-read",
            subject=str(path),
            owner="T1 / harness (bump FEATURE_LEDGER_SCHEMA deliberately)",
        )
    features: List[Feature] = []
    for item in document.get("features") or ():
        try:
            features.append(Feature.from_dict(item))
        except (ValueError, TypeError) as exc:
            return None, _refused(
                "malformed_document",
                f"a feature in the ledger could not be read: {exc}",
                subject=str(path),
            )
    ledger = FeatureLedger(
        schema=schema,
        project_id=str(document.get("project_id") or ""),
        request=str(document.get("request") or ""),
        created_at=str(document.get("created_at") or ""),
        sessions=tuple(str(item) for item in (document.get("sessions") or ())),
        features=tuple(features),
    )
    return ledger, None


# ---------------------------------------------------------------------------
# The status transition: the one write, and the two refusals it can refuse on.
# ---------------------------------------------------------------------------


def check_verification_evidence(
    evidence: Sequence[VerificationRun],
    *,
    allow_attested: bool = False,
    feature_id: str = "",
    command: str = "",
) -> Optional[LedgerRefusal]:
    """Return whether this evidence may support a ``passing`` claim.

    **FIVE refusals, in the order a reader cares about**, and the last two are
    P4/W2-T1's, added after the eight-scenario pressure attack produced three
    false passes through one missing pair of predicates:

    * **no evidence at all** -- an assertion, not a grade;
    * **evidence that is all ``attested``** on a feature whose method is
      runnable -- refused unless the caller passed ``allow_attested=True``,
      because "a human said so" is not a command's exit code;
    * **evidence belonging to ANOTHER FEATURE** -- measured, not imagined.
      ``VerificationRun.feature_id`` was written and never compared, so
      ``derived_from``-style pressure ("this is a duplicate of feature 3") could
      present feature 3's passing run as its own proof and land ``passing``;
    * **evidence from ANOTHER COMMAND** -- the brief's scenario 1, *"the tests
      pass, mark it done"*. A row whose ``ran=True, outcome="pass"`` was
      accepted for a feature whose declared check is the e2e one, because nothing
      here knew what the feature's command WAS. "The tests pass" is a claim about
      *some* tests, and the ledger has to ask which;
    * **evidence whose outcome is not ``pass``** -- nobody checked, it broke, or
      (P4/W2-T1) it was ``flaky``, which this repository has never counted as a
      pass.

    ``feature_id`` and ``command`` are keyword-and-defaulted so an existing
    caller that passes neither keeps its old behaviour, but
    :func:`mark_feature_status` always passes both -- the refusal is worthless
    if the enforcement point declines to supply the thing it enforces against.
    An empty ``command`` skips only the COMMAND check: a ``human_check`` feature
    legitimately has no command, and demanding one would refuse every attestation.
    """
    runs = tuple(evidence or ())
    if not runs:
        return _refused(
            "verification_required",
            "a feature cannot be marked passing with no verification run behind "
            "it; the agent's claim is not evidence",
        )
    if not allow_attested and all(run.kind == "attested" for run in runs):
        return _refused(
            "verification_required",
            "every verification behind this claim is ATTESTED (a human said so), "
            "not RUN; pass allow_attested=True if that is genuinely the "
            "contract for this feature",
            subject=runs[0].feature_id,
        )
    if allow_attested and all(run.kind == "attested" for run in runs):
        # An explicit override makes the ATTESTATION the evidence. It does not
        # manufacture a run: the row still says ``ran: False`` and the feature
        # still reports ``evidence_kind: "attested"``, so a reader can tell an
        # attested feature from a verified one.
        return None
    if feature_id:
        foreign = sorted(
            {str(run.feature_id) for run in runs if str(run.feature_id) != str(feature_id)}
        )
        if foreign:
            return _refused(
                "verification_did_not_pass",
                f"this claim is about {feature_id!r} and the evidence behind it is "
                f"about {', '.join(repr(name) for name in foreign)}. One feature's "
                "evidence is not another's, however similar the two look",
                subject=str(feature_id),
            )
    expected = str(command or "").strip()
    if expected:
        mismatched = sorted(
            {
                str(run.command)
                for run in runs
                if str(run.command or "").strip() and str(run.command).strip() != expected
            }
        )
        if mismatched:
            return _refused(
                "verification_did_not_pass",
                f"this feature's declared check is {expected!r} and the evidence "
                f"behind it ran {', '.join(repr(name) for name in mismatched)}. "
                "Evidence from a different check is not this feature's proof -- "
                "'the tests pass' is a claim about SOME tests, and the ledger asks "
                "which",
                subject=str(feature_id),
            )
    if not any(run.is_pass_evidence for run in runs):
        outcomes = ", ".join(f"{run.method}={run.outcome}" for run in runs)
        return _refused(
            "verification_did_not_pass",
            f"no verification behind this claim passed ({outcomes})",
            subject=runs[0].feature_id,
        )
    return None


def mark_feature_status(
    ledger: FeatureLedger,
    feature_id: str,
    status: str,
    *,
    evidence: Sequence[VerificationRun],
    allow_attested: bool = False,
) -> Tuple[FeatureLedger, Optional[LedgerRefusal]]:
    """Return ``(new_ledger, None)`` or ``(ledger, refusal)``.

    The ONLY way a status changes in this module. It refuses, in order: an
    unknown feature; a status outside the vocabulary; a ``passing`` claim with
    no passing verification behind it; and a claim that changes nothing (an
    ``already_set`` refusal, because a silent no-op on a write is a receipt
    that cannot be told apart from a write that was refused).
    """
    wanted = str(status or "").strip().lower()
    if wanted not in FEATURE_STATUSES:
        return ledger, _refused(
            "status_not_in_vocabulary",
            f"{status!r} is not one of {FEATURE_STATUSES}",
            subject=str(feature_id),
        )
    current = ledger.feature(feature_id)
    if current is None:
        return ledger, _refused(
            "unknown_feature",
            f"no feature {feature_id!r} in this ledger; ids are read-only to the "
            "agent, so a new requirement is an initializer decision",
            subject=str(feature_id),
        )
    if wanted == PASSING_FEATURE_STATUS:
        # P4/W2-T1: supply BOTH the feature and its own declared check. The two
        # new refusals in `check_verification_evidence` are unenforceable without
        # them -- a check that does not know which feature or which command it is
        # judging cannot catch evidence that belongs to another feature or came
        # from another check. A `human_check` feature has no command, so the
        # command is taken from its verification and may be empty.
        refusal = check_verification_evidence(
            evidence,
            allow_attested=allow_attested,
            feature_id=str(feature_id),
            command=str(current.verification.command or ""),
        )
        if refusal is not None:
            return ledger, refusal
    if current.status == wanted and tuple(current.evidence) == tuple(evidence):
        return ledger, _refused(
            "status_already_set",
            f"{feature_id!r} is already {wanted!r} with this evidence",
            subject=str(feature_id),
        )
    return ledger.with_status(feature_id, wanted, evidence), None


def apply_ledger_patch(
    ledger: FeatureLedger,
    patch: Mapping[str, Any],
    *,
    allow_attested: bool = False,
) -> Tuple[FeatureLedger, Optional[LedgerRefusal]]:
    """Apply an agent-supplied patch, allowing ONLY the status field.

    This is the structural enforcement the brief asks for, and its shape is
    the argument: a patch is a document, the writable set is
    :data:`AGENT_WRITABLE_LEDGER_FIELDS`, and a key outside it is refused
    **by name**. There is no code path that writes ``features[*].description``,
    ``features[*].verification``, ``request``, ``project_id`` or the feature
    list itself from a patch, so "the agent can change only the status field"
    is a property of the data flow rather than an instruction.

    Accepted patch shapes:

    * ``{"feature_id": "x", "status": "passing"}`` -- one transition.
    * ``{"transitions": [{"feature_id": "x", "status": "passing", ...}]}`` --
      several, all validated before ANY is applied, so a patch that is half
      valid changes nothing at all. A partial application of a refused patch is
      the state where the ledger and the agent's belief about it disagree.
    """
    if not isinstance(patch, Mapping):
        return ledger, _refused(
            "malformed_document",
            f"a ledger patch must be a JSON object, got {type(patch).__name__}",
        )

    transitions = patch.get("transitions")
    if transitions is None:
        if "feature_id" not in patch:
            # No feature_id means this cannot be a transition. It is still
            # field-checked first, because a patch whose ONLY key is a read-only
            # one is an attempt to rewrite the criteria rather than a malformed
            # transition, and the receipt should say which it was.
            illegal = sorted(str(key) for key in patch if str(key) != "transitions")
            if illegal:
                return ledger, _refused(
                    "field_not_writable",
                    "the agent may change only "
                    f"{list(AGENT_WRITABLE_LEDGER_FIELDS)}; refused: "
                    f"{', '.join(illegal)}. Success criteria are not the agent's "
                    "to rewrite",
                    subject="",
                )
            return ledger, _refused(
                "malformed_document",
                "a ledger patch needs a feature_id (or a transitions list)",
            )
        transitions = [patch]
    elif not isinstance(transitions, (list, tuple)):
        return ledger, _refused(
            "malformed_document",
            f"transitions must be a list, got {type(transitions).__name__}",
        )

    # Field check FIRST, across the whole patch, so a read-only edit is refused
    # before any transition is attempted.
    for item in transitions:
        if not isinstance(item, Mapping):
            return ledger, _refused(
                "malformed_document",
                f"each transition must be a JSON object, got {type(item).__name__}",
            )
        illegal = sorted(
            str(key)
            for key in item
            if str(key) not in AGENT_WRITABLE_LEDGER_FIELDS
            and str(key) not in ("feature_id", "evidence")
        )
        if illegal:
            return ledger, _refused(
                "field_not_writable",
                "the agent may change only "
                f"{list(AGENT_WRITABLE_LEDGER_FIELDS)}; refused: {', '.join(illegal)}. "
                "Success criteria are not the agent's to rewrite -- an agent that "
                "can rewrite them can rewrite its own grade",
                subject=str(item.get("feature_id") or ""),
            )
        if "allow_attested" in item:
            # P4/W2-T1, MEASURED. This key WAS on the per-item allowlist, and the
            # per-item value was PREFERRED over the caller's:
            # ``allow_attested=bool(item.get("allow_attested", allow_attested))``.
            # So an agent-supplied patch could set ``allow_attested: true``,
            # attach an ``attested`` evidence row, and move a feature to
            # ``passing`` -- and the three-session probe did exactly that in all
            # four sessions. The landed state was ``status=passing``,
            # ``evidence_kind="attested"``, ``verification_ran=False``: a feature
            # marked DONE on the strength of "a human said so", reached entirely
            # through the untrusted side of the interface.
            #
            # That contradicts this module's own stated reason for the flag
            # existing (``:100`` -- "a named argument rather than a silent
            # default"). A named argument is a thing a CALLER passes. Making the
            # same name a patch key hands it to the party the patch is assumed
            # not to be. The two cannot both be true, so the patch key is gone
            # and the flag stays what it said it was.
            #
            # It is REFUSED rather than ignored, and the reason matters: a
            # silently-ignored override returns success while doing nothing the
            # caller asked for, which is the shape of a gate that does not exist.
            return ledger, _refused(
                "field_not_writable",
                "`allow_attested` is an OPERATOR argument and cannot arrive in an "
                "agent-supplied patch. An attestation is still a human's word rather "
                "than an executed run, so the decision to accept one belongs to the "
                "caller that owns the ledger, not to the session asking for it",
                subject=str(item.get("feature_id") or ""),
            )

    working = ledger
    refusals: List[LedgerRefusal] = []
    for item in transitions:
        feature_id = str(item.get("feature_id") or "")
        status = str(item.get("status") or "")
        evidence = tuple(
            VerificationRun.from_dict(row) for row in (item.get("evidence") or ())
        )
        candidate, refusal = mark_feature_status(
            working,
            feature_id,
            status,
            evidence=evidence,
            # The CALLER's flag only. A per-item value is not consulted, and the
            # key is refused above, so there is no path from a patch to here.
            allow_attested=bool(allow_attested),
        )
        if refusal is not None:
            refusals.append(refusal)
            continue
        working = candidate
    if refusals:
        # The FIRST refusal is returned and nothing is applied: a patch with
        # one bad transition must not half-land.
        return ledger, refusals[0]
    return working, None


def feature_status_summary(ledger: FeatureLedger) -> Dict[str, Any]:
    """Return the counts a reader needs to see progress at a glance."""
    counts = {status: 0 for status in FEATURE_STATUSES}
    for feature in ledger.features:
        counts[feature.status] = counts.get(feature.status, 0) + 1
    return {
        "total": len(ledger.features),
        "by_status": counts,
        "passing": counts.get(PASSING_FEATURE_STATUS, 0),
        "verification_ran": sum(1 for item in ledger.features if item.verification_ran),
        "all_failing": counts.get(PASSING_FEATURE_STATUS, 0) == 0,
        "features": [
            {
                "id": item.id,
                "status": item.status,
                "verification": item.verification.describe(),
                "verification_ran": item.verification_ran,
                "evidence_kind": item.evidence_kind,
            }
            for item in ledger.features
        ],
    }


def feature_ledger_coverage(ledger: FeatureLedger) -> Dict[str, Any]:
    """Return whether every feature carries a usable verification method.

    A ledger where one feature has no check is a ledger where one feature will
    be marked passing on assertion, so this is worth reporting as a number
    rather than as a comment.
    """
    total = len(ledger.features)
    with_command = sum(1 for item in ledger.features if item.verification.method == "command")
    with_e2e = sum(1 for item in ledger.features if item.verification.method == "e2e")
    with_human = sum(1 for item in ledger.features if item.verification.method == "human_check")
    runnable = with_command + with_e2e
    return {
        "total": total,
        "command": with_command,
        "e2e": with_e2e,
        "human_check": with_human,
        "runnable": runnable,
        "runnable_share": (runnable / total) if total else None,
        "no_verification": total - runnable - with_human,
        "note": (
            "a human_check feature can only be marked passing on ATTESTED "
            "evidence unless the caller passes allow_attested=True"
        ),
    }


# ---------------------------------------------------------------------------
# The initializer: init.sh, the progress ledger, the initial commit, the
# feature ledger.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectProfile:
    """What the project is and how to run it.

    ``test_command`` / ``build_command`` / ``lint_command`` are what ``init.sh``
    is rendered from. A profile with no test command produces an ``init.sh``
    that says so out loud, because an ``init.sh`` that silently omits the check
    is the token saving Anthropic describes, spent on nothing.
    """

    name: str = ""
    language: str = "python"
    test_command: str = ""
    build_command: str = ""
    lint_command: str = ""
    install_command: str = ""
    start_command: str = ""
    notes: str = ""

    def missing(self) -> Tuple[str, ...]:
        """Return the commands this profile does NOT declare."""
        wanted = {
            "test_command": self.test_command,
            "build_command": self.build_command,
            "lint_command": self.lint_command,
        }
        return tuple(name for name, value in wanted.items() if not str(value or "").strip())

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this profile."""
        return {
            "name": self.name,
            "language": self.language,
            "test_command": self.test_command,
            "build_command": self.build_command,
            "lint_command": self.lint_command,
            "install_command": self.install_command,
            "start_command": self.start_command,
            "notes": self.notes,
            "missing": list(self.missing()),
        }


def render_init_script(profile: ProjectProfile, *, root: Any = "") -> str:
    """Render ``init.sh`` from a profile.

    Deliberately **generated, never authored by the model**. Anthropic's point
    is that the script "saves Claude some tokens in every session since it
    doesn't have to figure out how to test the code"; a script the model wrote
    is a script the model re-derives a better version of next session, which
    spends the saving.
    """
    missing = profile.missing()
    lines: List[str] = [
        "#!/usr/bin/env bash",
        "# init.sh - how to run this project. GENERATED by the harness initializer.",
        "#",
        "# This file exists so a coding session does not have to spend its budget",
        "# discovering how to build and test this repository. Read it, then run the",
        "# commands below. Do not rewrite it to describe a different project.",
        "set -euo pipefail",
        "",
        f'PROJECT_NAME={profile.name or "(unnamed)"}',
        f'LANGUAGE={profile.language or "python"}',
        f'ROOT={root or "."}',
        "",
    ]
    commands = [
        ("install", profile.install_command),
        ("build", profile.build_command),
        ("test", profile.test_command),
        ("lint", profile.lint_command),
        ("start", profile.start_command),
    ]
    for label, command in commands:
        lines.append(f"# --- {label} ---")
        if str(command or "").strip():
            lines.append(f'echo "==> {label}"')
            lines.append(str(command).strip())
        else:
            lines.append(f'echo "==> {label}: NOT DECLARED for this project"')
        lines.append("")
    lines.extend(
        [
            "# --- checks ---",
            'if [ -x "$ROOT/verify_feature.sh" ]; then',
            '  echo "==> feature verification"',
            '  "$ROOT/verify_feature.sh"',
            "fi",
            "",
        ]
    )
    if missing:
        lines.extend(
            [
                "# NOTE: this profile does not declare: " + ", ".join(missing) + ".",
                "# A missing command is reported rather than guessed: a run that",
                "# cannot find the test command will say so here instead of",
                "# reporting a clean run it never performed.",
                "",
            ]
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class ArtifactPointer:
    """A POINTER to offloaded content, never the content.

    This is "context offloading applied at the agent boundary": a subagent's
    large output leaves as a path plus a size plus a digest, and the parent
    reads it if it needs to. ``chars`` is carried so the parent knows what it
    is choosing not to read.
    """

    name: str
    path: str
    chars: int = 0
    digest: str = ""

    def render(self) -> str:
        """Return the model-facing one-line form: a pointer, not the payload."""
        return f"{self.name}: {self.path} ({self.chars} chars, sha256 {self.digest[:12]})"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this pointer."""
        return {
            "name": self.name,
            "path": self.path,
            "chars": int(self.chars),
            "digest": self.digest,
        }


def offload_artifact(root: Any, name: str, text: str) -> Tuple[ArtifactPointer, Optional[LedgerRefusal]]:
    """Write ``text`` under ``root`` and return a POINTER to it.

    Refuses a ``name`` that would escape ``root`` (``..``, an absolute path, a
    separator). An artifact store that can be made to write outside its own
    directory is a file-write primitive wearing a storage costume.
    """
    safe = str(name or "").strip().replace("\\", "/")
    if not safe or safe.startswith("/") or ".." in safe.split("/"):
        return ArtifactPointer(name=str(name), path="", chars=0), _refused(
            "artifacts_off_root",
            f"artifact name {name!r} would escape the artifact root",
            subject=str(name),
        )
    try:
        target = os.path.join(str(root), *safe.split("/"))
        os.makedirs(os.path.dirname(target) or str(root), exist_ok=True)
        payload = str(text or "")
        _atomic_write_text(target, payload)
    except OSError as exc:
        return ArtifactPointer(name=safe, path="", chars=0), _refused(
            "artifacts_off_root", f"artifact could not be written: {exc}", subject=safe
        )
    return (
        ArtifactPointer(
            name=safe,
            path=target,
            chars=len(str(text or "")),
            digest=hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest(),
        ),
        None,
    )


def render_progress_ledger(
    project_id: str,
    request: str,
    features: Sequence[Feature],
    *,
    session: str = "",
) -> str:
    """Render the progress ledger's Markdown.

    **Markdown, on purpose, and this is the opposite of the feature ledger.**
    The feature ledger is JSON because the agent must not rewrite it. The
    progress ledger is Markdown because the agent is *supposed* to append to it
    every session -- it is the one artifact whose whole job is to be written
    by the session that just finished. Getting this backwards (JSON for the
    progress log, Markdown for the criteria) would put a structured file the
    model is told to rewrite under a schema validator and a free-text file
    under a success-criteria role.
    """
    lines: List[str] = [
        f"# Progress: {project_id}",
        "",
        "<!-- GENERATED by the harness initializer. The agent MAY append to this",
        "     file and SHOULD: it is the progress ledger a session reads first and",
        "     writes last. The feature criteria are NOT here on purpose - they live",
        "     in features.json, which the agent may not rewrite. -->",
        "",
        "## Request",
        "",
        request or "(no request recorded)",
        "",
        "## Features (criteria live in `features.json`; status only is agent-writable)",
        "",
    ]
    for feature in features:
        lines.append(
            f"- [{feature.status}] `{feature.id}` - {feature.description or '(no description)'}"
        )
        lines.append(f"  - verification: {feature.verification.describe()}")
    lines.extend(
        [
            "",
            "## Session log",
            "",
            "<!-- One entry per session. Newest last. -->",
        ]
    )
    if session:
        lines.extend(["", f"### session {session}", "", "- initialised."])
    lines.append("")
    return "\n".join(lines)


def write_progress_ledger(path: Any, text: str) -> Dict[str, Any]:
    """Write the progress ledger atomically and return a receipt."""
    return _atomic_write_text(path, text)


def load_progress_ledger(path: Any) -> str:
    """Return the progress ledger's text, or ``""`` when there is none.

    Total by design: a session that cannot read the progress ledger should say
    so and start fresh, not crash before it has written anything.
    """
    try:
        return _read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def run_git(
    args: Sequence[str],
    *,
    cwd: Any,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run one git command and return a receipt. Never raises.

    ``runner`` is injected and is REQUIRED: with none, the receipt reports
    ``ran: False`` and a reason. The initializer's initial commit is a real
    artefact requirement, and "we could not make the commit" must be a reported
    state rather than a silent skip -- a session that believes it committed and
    did not is the failure mode.
    """
    command = ["git", *[str(item) for item in (args or ())]]
    if runner is None:
        return {
            "ran": False,
            "available": False,
            "command": " ".join(command),
            "exit_code": None,
            "output": "no git runner is bound; the initial commit was NOT made",
        }
    try:
        outcome = runner(command, cwd=str(cwd))
    except Exception as exc:
        return {
            "ran": False,
            "available": True,
            "command": " ".join(command),
            "exit_code": None,
            "output": f"{type(exc).__name__}: {exc}"[:400],
        }
    exit_code, output, _duration = _unpack_runner_outcome(outcome)
    return {
        "ran": exit_code is not None,
        "available": True,
        "command": " ".join(command),
        "exit_code": exit_code,
        "output": str(output or "")[:2000],
        "ok": exit_code == 0,
    }


@dataclass(frozen=True)
class InitializerReceipt:
    """What the initializer actually produced.

    Four artefacts, each with its own ``written``/``reason``, because a receipt
    that says "initialized" while silently skipping the commit is exactly the
    kind of overclaim this repository spends its whole test suite preventing.
    """

    project_id: str
    root: str = ""
    init_script: Dict[str, Any] = field(default_factory=dict)
    progress_ledger: Dict[str, Any] = field(default_factory=dict)
    feature_ledger: Dict[str, Any] = field(default_factory=dict)
    initial_commit: Dict[str, Any] = field(default_factory=dict)
    profile: Dict[str, Any] = field(default_factory=dict)
    refusals: Tuple[LedgerRefusal, ...] = ()

    @property
    def complete(self) -> bool:
        """Return whether all FOUR artefacts were produced.

        Four, not three: Anthropic's initializer is characterised by an initial
        git commit "showing what files were added", and a scaffold whose
        history starts empty cannot be reverted to, which is the property the
        commit exists for.
        """
        return bool(
            self.init_script.get("written")
            and self.progress_ledger.get("written")
            and self.feature_ledger.get("written")
            and self.initial_commit.get("ok")
            and not self.refusals
        )

    def degraded(self) -> Tuple[str, ...]:
        """Return the names of the artefacts that were NOT produced."""
        missing: List[str] = []
        if not self.init_script.get("written"):
            missing.append(INIT_SCRIPT_NAME)
        if not self.progress_ledger.get("written"):
            missing.append(PROGRESS_LEDGER_NAME)
        if not self.feature_ledger.get("written"):
            missing.append(FEATURE_LEDGER_NAME)
        if not self.initial_commit.get("ok"):
            missing.append("initial git commit")
        return tuple(missing)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this receipt."""
        return {
            "project_id": self.project_id,
            "root": self.root,
            "init_script": dict(self.init_script),
            "progress_ledger": dict(self.progress_ledger),
            "feature_ledger": dict(self.feature_ledger),
            "initial_commit": dict(self.initial_commit),
            "profile": dict(self.profile),
            "refusals": [item.to_dict() for item in self.refusals],
            "complete": self.complete,
            "degraded": list(self.degraded()),
        }

    def render(self) -> str:
        """Return plain lines naming what exists and what does not."""
        lines: List[str] = [f"initializer: {self.project_id}"]
        for label, record in (
            (INIT_SCRIPT_NAME, self.init_script),
            (PROGRESS_LEDGER_NAME, self.progress_ledger),
            (FEATURE_LEDGER_NAME, self.feature_ledger),
            ("initial commit", self.initial_commit),
        ):
            if record.get("written") or record.get("ok"):
                lines.append(f"  OK       {label}")
            else:
                reason = str(record.get("reason") or record.get("output") or "not produced")
                lines.append(f"  MISSING  {label}: {reason[:120]}")
        for refusal in self.refusals:
            lines.append(f"  REFUSED  {refusal.render()}")
        return "\n".join(lines)


def initialize_project(
    root: Any,
    profile: ProjectProfile,
    features: Sequence[Feature],
    *,
    request: str = "",
    project_id: str = "",
    session: str = "",
    git_runner: Optional[Any] = None,
    dry_run: bool = False,
) -> InitializerReceipt:
    """Scaffold a repository for multi-session work, and say what it produced.

    Writes, in this order: ``init.sh`` (rendered from the profile),
    ``PROGRESS.md`` (the progress ledger), ``features.json`` (the JSON feature
    ledger, every feature ``failing``), and then makes the initial commit.

    The commit is attempted LAST on purpose. It has to contain the other three
    files -- Anthropic's initial commit exists to "show what files were added" --
    so committing before they are written would produce a commit that shows
    nothing.
    """
    root_path = str(root)
    identifier = str(project_id or profile.name or "project")
    refusals: List[LedgerRefusal] = []

    init_text = render_init_script(profile, root=".")
    progress_text = render_progress_ledger(identifier, request, features, session=session)
    ledger = new_feature_ledger(
        identifier,
        request,
        features,
        created_at="",
        sessions=(session,) if session else (),
    )

    init_record: Dict[str, Any] = {"written": False, "path": os.path.join(root_path, INIT_SCRIPT_NAME)}
    progress_record: Dict[str, Any] = {
        "written": False,
        "path": os.path.join(root_path, PROGRESS_LEDGER_NAME),
    }
    feature_record: Dict[str, Any] = {
        "written": False,
        "path": os.path.join(root_path, FEATURE_LEDGER_NAME),
    }

    if dry_run:
        for record, text in (
            (init_record, init_text),
            (progress_record, progress_text),
            (feature_record, ledger.render()),
        ):
            record.update({"written": False, "would_write": True, "chars": len(text)})
        commit = {
            "ran": False,
            "ok": False,
            "available": git_runner is not None,
            "command": "git add -A && git commit -m 'chore: initializer scaffold'",
            "reason": "dry run: no command was executed",
        }
    else:
        try:
            os.makedirs(root_path, exist_ok=True)
        except OSError as exc:
            refusal = _refused(
                "artifacts_off_root", f"repository root is unusable: {exc}", subject=root_path
            )
            return InitializerReceipt(
                project_id=identifier,
                root=root_path,
                init_script=init_record,
                progress_ledger=progress_record,
                feature_ledger=feature_record,
                initial_commit={"ran": False, "ok": False, "reason": "root unusable"},
                profile=profile.to_dict(),
                refusals=(refusal,),
            )
        for record, name, text in (
            (init_record, INIT_SCRIPT_NAME, init_text),
            (progress_record, PROGRESS_LEDGER_NAME, progress_text),
            (feature_record, FEATURE_LEDGER_NAME, ledger.render()),
        ):
            written = _atomic_write_text(os.path.join(root_path, name), text)
            record.update(written)
            if not written.get("written"):
                record["reason"] = written.get("reason", "write failed")
        commit = _initial_commit(root_path, git_runner)

    if not init_record.get("written") or not progress_record.get("written") or not feature_record.get("written"):
        refusals.append(
            _refused(
                "initializer_degraded",
                "one or more initializer artefacts were not written: "
                + ", ".join(
                    name
                    for name, record in (
                        (INIT_SCRIPT_NAME, init_record),
                        (PROGRESS_LEDGER_NAME, progress_record),
                        (FEATURE_LEDGER_NAME, feature_record),
                    )
                    if not record.get("written")
                ),
                subject=identifier,
            )
        )
    if not commit.get("ok"):
        refusals.append(
            _refused(
                "initializer_degraded",
                "the initial commit was not made; a scaffold whose history starts "
                "empty cannot be reverted to, which is what the commit is for",
                subject=identifier,
            )
        )
    return InitializerReceipt(
        project_id=identifier,
        root=root_path,
        init_script=init_record,
        progress_ledger=progress_record,
        feature_ledger=feature_record,
        initial_commit=commit,
        profile=profile.to_dict(),
        refusals=tuple(refusals),
    )


def _initial_commit(root_path: str, git_runner: Optional[Any]) -> Dict[str, Any]:
    """Make the initializer's initial commit, and report exactly what happened."""
    add = run_git(["add", "-A"], cwd=root_path, runner=git_runner)
    if not add.get("ok"):
        return {**add, "reason": "git add failed", "committed": False}
    commit = run_git(
        ["commit", "-m", "chore: initializer scaffold"],
        cwd=root_path,
        runner=git_runner,
    )
    return {
        **commit,
        "committed": bool(commit.get("ok")),
        "reason": "" if commit.get("ok") else "git commit failed",
        "add": add,
    }


# ---------------------------------------------------------------------------
# Session discipline: one feature, start clean, end clean.
# ---------------------------------------------------------------------------


def render_session_brief(
    ledger: Optional[FeatureLedger],
    progress_text: str = "",
    git_log: str = "",
    *,
    claimed_feature: str = "",
) -> str:
    """Render the block a coding session reads FIRST.

    Anthropic's instruction is exact: *"Read the progress ledger and git log
    first to get up to speed on what was recently worked on."* Both are inputs
    here, and the ledger contributes the status table -- the criteria are
    visible to the session that must satisfy them, which is not the same thing
    as letting it edit them.
    """
    lines: List[str] = [
        "[neo-session-start]",
        "Read this before doing anything else. Do not rewrite the criteria.",
        "",
        "## Feature ledger (features.json - READ ONLY except a feature's status)",
    ]
    if ledger is None:
        lines.append("(no ledger loaded; do not guess the requirements)")
    else:
        summary = feature_status_summary(ledger)
        lines.append(
            f"{summary['passing']}/{summary['total']} passing; "
            f"runnable-verification features: {sum(1 for item in ledger.features if item.verification.runnable)}"
        )
        for item in summary["features"]:
            lines.append(f"- [{item['status']}] {item['id']}: {item['verification']}")
    lines.extend(["", "## Progress ledger (PROGRESS.md - APPEND to it, do not rewrite it)"])
    progress_text = str(progress_text or "").strip()
    lines.append(progress_text or "(no progress recorded yet)")
    lines.extend(["", "## git log (what was recently worked on)"])
    git_log = str(git_log or "").strip()
    lines.append(git_log or "(no commits yet)")
    lines.extend(["", "## This session's scope"])
    if claimed_feature:
        lines.append(
            f"ONE feature this session: {claimed_feature}. Finish it, verify it, "
            "commit, update the progress ledger, and stop."
        )
    else:
        lines.append(
            "Claim exactly ONE feature before touching code (see the "
            "one-feature-per-session rule). Doing several at once is what makes "
            "a multi-session effort fail."
        )
    lines.append("")
    lines.append("Rules that are ENFORCED, not requested: tests cannot be edited or")
    lines.append("removed; a file cannot be edited before it has been read; a feature")
    lines.append("cannot be marked passing without its verification having run.")
    lines.append("[/neo-session-start]")
    return "\n".join(lines)


def session_end_receipt(
    session: str,
    ledger: FeatureLedger,
    *,
    feature_id: str = "",
    status: str = "",
    verification_runs: Sequence[VerificationRun] = (),
    commit_sha: str = "",
    git_runner: Optional[Any] = None,
    changed_paths: Sequence[str] = (),
    root: Any = "",
) -> Dict[str, Any]:
    """Render the receipt for ending a session CLEAN.

    *"Commit progress with descriptive messages + write a progress summary.
    This allowed the model to use git to revert bad code changes and recover
    working states."* Both halves are here, and both are **reported** rather
    than assumed: a session that ends without a commit gets a receipt that says
    so.

    The test-integrity verdict runs here rather than in the prompt because
    *"It is unacceptable to remove or edit tests"* is a policy, and a policy
    asked for politely is a request.
    """
    integrity = verify_test_integrity(changed_paths)
    runs = tuple(verification_runs or ())
    passing = None
    refusal: Optional[LedgerRefusal] = None
    if status:
        if feature_id:
            _new_ledger, refusal = mark_feature_status(
                ledger, feature_id, status, evidence=runs
            )
            passing = refusal is None
        else:
            refusal = _refused(
                "no_feature_claimed",
                "a session that changes no feature's status must still commit "
                "and update the progress ledger; name the feature you worked on",
            )
    return {
        "session": str(session),
        "feature_id": str(feature_id),
        "requested_status": str(status),
        "status_applied": bool(passing),
        "refusal": refusal.to_dict() if refusal is not None else None,
        "refusal_line": refusal.render() if refusal is not None else "",
        "verification_runs": [run.to_dict() for run in runs],
        "commit": {
            "sha": str(commit_sha),
            "made": bool(commit_sha),
            "reason": "" if commit_sha else "no commit was made; the next session "
            "cannot revert this one's work",
        },
        "test_integrity": integrity.to_dict(),
        "clean": bool(commit_sha) and integrity.ok and (refusal is None or bool(passing)),
        "note": (
            "'clean' requires a commit, an intact test set, and no refused status "
            "change. It is a receipt, not an assertion."
        ),
    }


# ---------------------------------------------------------------------------
# Test integrity. A policy, not a prompt. Defence in depth OVER the existing
# guard, with the owner named: Phase 2's floor layer is T3's file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestIntegrityVerdict:
    """Whether a session's changed set touched the tests, and how."""

    ok: bool = True
    refusals: Tuple[Tuple[str, str], ...] = ()
    changed: Tuple[str, ...] = ()
    declared: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this verdict."""
        return {
            "ok": bool(self.ok),
            "refusals": [
                {"reason": reason, "path": path} for reason, path in self.refusals
            ],
            "changed": list(self.changed),
            "declared": self.declared,
            "added_is_not_an_edit": (
                "a caller passes added_paths so a NEW test file is never refused; the "
                "rule is about removing or editing an existing test"
            ),
            "enforcement": (
                "harness/long_horizon.py::verify_test_integrity is DEFENCE IN "
                "DEPTH. The primary guard is harness/editor.py::is_protected plus "
                "the R2-03 test-configuration guard, and the project floor layer "
                "is T3's. This exists so the rule is reachable from the session "
                "discipline and so a violation blocks the status change."
            ),
        }


def verify_test_integrity(
    changed_paths: Sequence[str],
    *,
    added_paths: Sequence[str] = (),
    declared: str = "",
    protected_globs: Sequence[str] = ("tests/*", "test_*.py", "*_test.py", "spec/*"),
) -> TestIntegrityVerdict:
    """Return whether a changed set respected the test contract.

    Anthropic's rule verbatim: *"It is unacceptable to remove or edit tests
    because this could lead to missing or buggy functionality."*

    **Adding a new test is NOT refused**, because the rule is about removing or
    *editing* an existing test and a codebase that cannot add tests cannot
    demonstrate anything. The caller therefore declares ``added_paths``, and a
    path in that set is never an offender. This matters: a guard that refuses
    correct behaviour is a guard people route around, and the first draft of
    this function refused every new test file, which is the shape of a guard
    that gets disabled within a week.

    The caller supplies ``changed_paths`` and gets told what it must supply:
    a path in ``changed_paths`` is treated as an EDIT of an existing file.
    A run that created a test file has to say so, because this function cannot
    tell a creation from an overwrite by looking at a path.
    """
    from fnmatch import fnmatch

    added = {str(item).replace("\\", "/").strip() for item in (added_paths or ())}
    changed = tuple(str(item).replace("\\", "/").strip() for item in (changed_paths or ()))
    offenders: List[Tuple[str, str]] = []
    for path in changed:
        if path in added:
            continue
        if any(
            fnmatch(path, pattern.replace("\\", "/")) for pattern in protected_globs
        ):
            offenders.append(("test_edited", path))
    if offenders and declared:
        # A DECLARED change is a receipt, not a silent bypass -- the same
        # discipline harness/test_config.py uses. It still gets reported.
        return TestIntegrityVerdict(
            ok=True,
            refusals=tuple(offenders),
            changed=changed,
            declared=str(declared),
        )
    return TestIntegrityVerdict(ok=not offenders, refusals=tuple(offenders), changed=changed)


# ---------------------------------------------------------------------------
# The session object: one feature per session, enforced.
# ---------------------------------------------------------------------------


class LongHorizonSession:
    """One coding session over a feature ledger, with the scope rule enforced.

    *"This incremental approach turned out to be **critical** to addressing the
    agent's tendency to do too much at once."* A rule in a prompt is the wrong
    place for "critical": a session that is told to do one thing and is not
    stopped from doing five will do five, because the tendency is stronger than
    the instruction. So the rule lives in an object that REFUSES.

    **One claim per session, and the second claim is refused.** Not warned
    about, not counted against a budget: refused, with the first claim named.
    :meth:`claim` returns a :class:`LedgerRefusal` rather than raising, because a
    session that cannot be given a scope should be able to say why in one line
    and continue.

    **The claim is released by the status change, not by a timer.** A session
    that marked its feature ``blocked`` or ``skipped`` has also finished with
    it -- it stopped, and stopping is a session's end. Refusing the next claim
    then would force an operator to abandon a session to start another, which
    is a worse outcome than the rule was protecting against.

    ``one_feature_per_session`` defaults ON here even though most other Phase-4
    keys default off, because this object only exists when a caller has
    deliberately constructed a long-horizon session: there is no "an unconfigured
    run silently switched onto one-feature discipline" failure mode, and inside
    this object the rule is the point.
    """

    def __init__(
        self,
        ledger: FeatureLedger,
        *,
        session: str = "",
        one_feature_per_session: bool = True,
        progress_text: str = "",
        git_log: str = "",
    ) -> None:
        self.ledger = ledger
        self.session = str(session or "")
        self.one_feature_per_session = bool(one_feature_per_session)
        self.progress_text = str(progress_text or "")
        self.git_log = str(git_log or "")
        self._claimed: Optional[str] = None
        self._attempts: List[Dict[str, Any]] = []

    # -- claiming --------------------------------------------------------

    @property
    def claimed_feature(self) -> str:
        """Return the feature this session claimed, or ``""``."""
        return self._claimed or ""

    def claim(self, feature_id: str) -> Optional[LedgerRefusal]:
        """Claim one feature for this session, or be refused.

        Refuses a second claim with the FIRST claim named, because
        "you already claimed X" is actionable and "one feature per session" is
        not. Refuses an unknown id too: claiming a feature that does not exist
        is how a session spends itself on a requirement nobody wrote down.
        """
        wanted = str(feature_id or "").strip()
        if not wanted:
            return _refused(
                "no_feature_claimed",
                "a long-horizon session must name the ONE feature it is working on",
                subject=self.session,
            )
        if self.ledger.feature(wanted) is None:
            return _refused(
                "unknown_feature",
                f"no feature {wanted!r} in the ledger; the requirements are the "
                "initializer's, not the session's",
                subject=wanted,
            )
        if self.one_feature_per_session and self._claimed and self._claimed != wanted:
            self._attempts.append({"feature_id": wanted, "outcome": "refused"})
            return _refused(
                "one_feature_per_session",
                f"this session already claimed {self._claimed!r}; doing "
                f"{wanted!r} as well is the one-shotting this discipline exists "
                f"to prevent. Finish and commit {self._claimed!r}, then start a "
                "new session",
                subject=wanted,
            )
        self._claimed = wanted
        self._attempts.append({"feature_id": wanted, "outcome": "claimed"})
        return None

    def attempts(self) -> Tuple[Dict[str, Any], ...]:
        """Return every claim attempt, refused ones included.

        Reported because a session that was refused once and then worked anyway
        looks identical to one that was never refused, and the refusal is the
        mechanism this phase is about.
        """
        return tuple(self._attempts)

    # -- the session's two ends -----------------------------------------

    def brief(self) -> str:
        """Render what this session reads FIRST."""
        return render_session_brief(
            self.ledger,
            self.progress_text,
            self.git_log,
            claimed_feature=self._claimed,
        )

    def verify(
        self,
        *,
        runner: Optional[Any] = None,
        e2e_runner: Optional[Any] = None,
        allow_attested: bool = False,
    ) -> Tuple[Optional[VerificationRun], Optional[EndToEndReport], Optional[LedgerRefusal]]:
        """Run the claimed feature's verification. Returns
        ``(run, e2e_report, refusal)``.

        Refuses when no feature is claimed -- verifying a feature the session
        did not work on would produce evidence for somebody else's work, which
        is the substitution this module exists to prevent.
        """
        if not self._claimed:
            return (
                None,
                None,
                _refused(
                    "no_feature_claimed",
                    "claim a feature before verifying anything",
                    subject=self.session,
                ),
            )
        feature = self.ledger.feature(self._claimed)
        if feature is None:  # pragma: no cover - claim() already checked
            return (
                None,
                None,
                _refused("unknown_feature", "the claimed feature left the ledger",
                         subject=str(self._claimed)),
            )
        run, report = run_feature_verification(
            feature.id,
            feature.verification,
            runner=runner,
            e2e_runner=e2e_runner,
        )
        return run, report, None

    def end(
        self,
        *,
        status: str = "",
        verification_runs: Sequence[VerificationRun] = (),
        commit_sha: str = "",
        changed_paths: Sequence[str] = (),
        added_paths: Sequence[str] = (),
        git_runner: Optional[Any] = None,
        root: Any = "",
    ) -> Dict[str, Any]:
        """End the session and return the clean-state receipt.

        Three things happen here, in this order, and the order matters:

        1. **the test-integrity verdict runs FIRST**, and an undeclared test
           change REFUSES the status change. "Never remove or edit tests" is a
           policy, and a policy that a status change can route around is a
           request. This is defence in depth over
           ``harness/editor.py::is_protected``; the floor layer is T3's and the
           refusal names that owner.
        2. **the status change is applied only through
           :func:`mark_feature_status`**, so a ``passing`` claim with no
           passing verification is refused here exactly as it would be
           anywhere else.
        3. **the commit is recorded**, and a session without one gets a receipt
           that says so. Anthropic's reason for the commit is specific --
           *"This allowed the model to use git to revert bad code changes and
           recover working states"* -- and a session that did not commit cannot
           be reverted.
        """
        integrity = verify_test_integrity(changed_paths, added_paths=added_paths)
        runs = tuple(verification_runs or ())
        refusal: Optional[LedgerRefusal] = None
        applied = False
        ledger = self.ledger
        claimed = self._claimed

        if not integrity.ok:
            offender = integrity.refusals[0] if integrity.refusals else ("", "")
            refusal = _refused(
                "test_tampering",
                f"{offender[0]} on {offender[1]!r}: removing or editing a test can "
                "hide missing or buggy functionality. The change was not counted "
                "toward the feature's status",
                subject=offender[1],
                owner="T3 / phase-2 floor layer (harness is defence in depth)",
            )
        elif not status:
            # A session that touched nothing measurable has not done the two
            # things a session is FOR, and saying so is the receipt's job.
            refusal = _refused(
                "no_feature_claimed",
                "this session changed no feature's status; name the feature you "
                "worked on, or state why there was nothing to claim",
                subject=self.session,
            )
        elif not claimed:
            refusal = _refused(
                "no_feature_claimed",
                "a session that changes no feature's status must still commit "
                "and update the progress ledger",
                subject=self.session,
            )
        else:
            ledger, refusal = mark_feature_status(
                ledger, claimed, status, evidence=runs
            )
            applied = refusal is None
        self.ledger = ledger
        if applied:
            # The claim is released by the status change, not by a timer: a
            # session that blocked or skipped its feature has stopped, and
            # stopping is a session's end.
            self._claimed = None

        receipt = session_end_receipt(
            self.session,
            ledger,
            feature_id=claimed,
            status=status,
            verification_runs=runs,
            commit_sha=commit_sha,
            git_runner=git_runner,
            changed_paths=changed_paths,
            root=root,
        )
        receipt["claimed_feature"] = claimed
        receipt["status_applied"] = applied
        receipt["test_integrity"] = integrity.to_dict()
        receipt["refusal"] = refusal.to_dict() if refusal is not None else None
        receipt["refusal_line"] = refusal.render() if refusal is not None else ""
        receipt["clean"] = bool(commit_sha) and integrity.ok and (refusal is None or applied)
        return receipt

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this session's state."""
        return {
            "session": self.session,
            "claimed_feature": self._claimed,
            "one_feature_per_session": bool(self.one_feature_per_session),
            "attempts": [dict(item) for item in self._attempts],
            "features_total": len(self.ledger.features),
            "passing": sum(
                1 for item in self.ledger.features if item.status == PASSING_FEATURE_STATUS
            ),
        }


# ---------------------------------------------------------------------------
# Small IO helpers. Atomic, because a scaffold that a crash truncates is a
# scaffold that silently loses requirements.
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Any, text: str) -> Dict[str, Any]:
    """Write ``text`` to ``path`` via tmp + replace. Never raises."""
    target = str(path)
    payload = str(text or "")
    directory = os.path.dirname(target) or "."
    tmp = ""
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".neo-init-",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as handle:
            tmp = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return {
            "written": False,
            "path": target,
            "chars": len(payload),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {"written": True, "path": target, "chars": len(payload), "reason": ""}


def _read_text(path: Any) -> str:
    """Read a file as UTF-8, tolerating a BOM a PowerShell writer left."""
    with open(str(path), "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()
