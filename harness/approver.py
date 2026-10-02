"""AGT-07: the approver agent -- a cheap second opinion, which fails closed.

A human approval prompt is slow, it is easy to wave through, and it is the
only thing standing between a model and a privileged action. This module puts
a *second, cheap* model in front of that prompt -- never behind it. It judges
**only actions that already require a human**; it can refuse one, and it can
never create, widen, or silently satisfy one.

Four properties are the whole design, and each of them is the opposite of the
obvious convenience:

1. **It sees only already-privileged actions.** :func:`review` consults
   ``shared.approval.approver_admissible`` *before* it builds a prompt, and an
   inadmissible request never reaches a model at all. A reviewer that could be
   consulted about ordinary work would be a new gate, and a cheap-model gate is
   not a human gate.
2. **It fails closed.** An unparseable reply, an ambiguous reply, an
   approval with no stated reason, a timeout, a provider exception, a disabled
   agent, and an untraceable escalation are all **denials**, each with its own
   ``failure`` value so a receipt can distinguish "a human-style refusal" from
   "we could not obtain a usable answer". There is no key that turns this off
   -- an approver that fails open is not an approver, and an opt-out would be
   an opt-out from the approval itself.
3. **It names the originating thread/subagent.** Every prompt, every verdict
   and every journal row carries an :class:`~shared.approval.ApprovalOrigin`,
   so a subagent's escalation is labelled as that subagent's escalation and
   can never read as a decision made in the main thread a human is watching.
4. **It is journalled and redacted.** Each decision is appended as one
   redacted JSONL row carrying the reasoning, so a refusal can be reconstructed
   later without the journal re-publishing a secret the command happened to
   contain.

The verdict itself -- what an approval MEANS, and the refusal direction of
every unparseable shape -- is owned by ``shared.approval``
(:func:`~shared.approval.parse_approver_reply`). This module owns the
question; it does not own the answer's grammar, so a TUI, a CLI approver and
this model cannot drift on what "approve" means.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from shared.approval import (
    ApprovalOrigin,
    ApproverReply,
    ApproverRequest,
    approver_admissible,
    canonical_effect,
    describe_axes,
    parse_approver_reply,
)
from shared.security import redact_secrets, redact_text

__all__ = [
    "APPROVER_ENABLED_KEY",
    "APPROVER_MAX_CHARS_KEY",
    "APPROVER_MODEL_KEY",
    "APPROVER_TIMEOUT_KEY",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT_S",
    "ApproverAgent",
    "ApproverJournalEntry",
    "journal_path_for",
    "render_approver_prompt",
    "review_privileged_action",
]

#: Config keys. Only ``APPROVER_ENABLED_KEY`` changes behaviour, and only on
#: key presence plus a truthy value: an approver model call on every privileged
#: action is not something a value merged into every task should switch on by
#: accident.
APPROVER_ENABLED_KEY = "approver_agent"
APPROVER_MODEL_KEY = "approver_model"
APPROVER_TIMEOUT_KEY = "approver_timeout_s"
APPROVER_MAX_CHARS_KEY = "approver_max_chars"

#: Bounded defaults live HERE, not in ``harness/config.py::DEFAULTS`` -- a
#: default merged into every task and every eval arm would switch every run.
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_CHARS = 4000
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 300.0

#: The difficulty hint handed to the Boundary-2 boundary. "A second, CHEAP
#: model" is expressed as the router's cheap tier rather than a hardcoded model
#: name, so the approver is cheap on every provider instead of cheap on the
#: one this file was written against.
APPROVER_DIFFICULTY_HINT = "easy"

_APPROVER_SYSTEM = (
    "You review one action that a human has ALREADY been asked to approve. "
    "You do not decide whether to ask; that is already decided. Judge only "
    "whether this exact effect matches what a careful engineer would have "
    "meant, and whether its side effect is reversible.\n"
    "Reply with one JSON object and nothing else:\n"
    '{"decision": "approve" | "deny", "reason": "<one sentence a human reads>"}\n'
    "Both fields are required. A reply that is not a decision, or a decision "
    "with no reason, is treated as a denial."
)


def _bounded_number(value: Any, default: float, floor: float, ceiling: float) -> float:
    """Return ``value`` as a bounded float, or ``default`` when unusable."""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(floor, min(ceiling, number))


def render_approver_prompt(
    effect: Any,
    *,
    origin: Optional[ApprovalOrigin] = None,
    containment: Optional[Mapping[str, Any]] = None,
    privileged_reason: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Render the ONE prompt an approver model ever sees.

    The prompt carries the exact effect -- the canonical argv render, the
    working directory, the declared side-effect class, the paths, and the
    environment overrides -- plus WHO is asking and what the sandbox would
    permit. Those are the same fields the human is shown, derived from the
    same canonical effect, so a reviewer and an operator cannot be looking at
    two different actions.

    Two things are deliberately NOT in it. There is no way to approve a
    *class* of command (an approver that could say "yes to pytest forever"
    would be a grant, and grants belong to the trust ledger), and there is no
    instruction derived from containment that could make a contained action
    acceptable on its own -- containment is stated as a fact for the reviewer
    to weigh, in the shared receipt's own wording.

    Redacted and length-capped; a command that contained a secret is shown
    with the secret redacted rather than passed to a second provider.
    """
    canonical = effect
    lines: List[str] = [_APPROVER_SYSTEM, ""]
    lines.append("## Who is asking")
    lines.append((origin or ApprovalOrigin()).describe())
    if privileged_reason:
        lines.append(
            f"already requires a human because: {redact_text(privileged_reason)}"
        )
    lines.append("")
    lines.append("## The exact effect a human must approve")
    if hasattr(canonical, "render"):
        lines.append(f"tool: {canonical.tool}")
        lines.append(f"side effect: {canonical.side_effect_class}")
        lines.append(
            f"command: {canonical.render or ' '.join(canonical.argv) or '(none)'}"
        )
        if canonical.working_directory:
            lines.append(f"working directory: {canonical.working_directory}")
        if canonical.target:
            lines.append(f"target: {canonical.target}")
        if canonical.environment:
            lines.append(
                "environment overrides: "
                + ", ".join(f"{key}={value}" for key, value in canonical.environment)
            )
        lines.append(f"effect digest: {canonical.digest}")
    else:
        lines.append(redact_text(json.dumps(canonical, sort_keys=True, default=str)))
    lines.append("")
    lines.append("## What the sandbox would permit (context, not justification)")
    lines.append(
        describe_axes(
            containment=containment,
            containment_known=bool(containment),
            decision="ask",
        )
    )
    text = "\n".join(lines)
    limit = max(400, int(max_chars or DEFAULT_MAX_CHARS))
    if len(text) > limit:
        text = text[:limit] + "\n[... approver prompt truncated ...]"
    return text


@dataclass(frozen=True)
class ApproverJournalEntry:
    """One redacted, append-only record of one approver decision."""

    verdict: ApproverReply
    tool: str = ""
    render: str = ""
    effect_digest: str = ""
    task_id: str = ""
    at: float = 0.0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSONL row: the verdict, its reasoning, and its origin."""
        return {
            "at": round(float(self.at or 0.0), 3),
            "task_id": redact_text(self.task_id),
            "tool": redact_text(self.tool),
            "render": redact_text(self.render),
            "effect_digest": redact_text(self.effect_digest),
            "verdict": self.verdict.to_dict(),
            "extra": redact_secrets(dict(self.extra or {})),
        }


def journal_path_for(log_dir: Any, task_id: str = "") -> str:
    """Return the approver journal path for one run.

    A separate file from ``trace.jsonl`` for the same reason the trust receipt
    is: the trace is the kernel's append-only record with contiguous sequence
    allocation, and a second writer guessing at its sequence would corrupt
    ``replay_run``. Every line is one JSON object, which is all this journal
    needs.
    """
    base = Path(str(log_dir or ".")).expanduser()
    safe = "".join(
        char if (char.isalnum() or char in "-_") else "-" for char in str(task_id or "")
    ).strip("-")
    return str(base / "approvals" / f"{(safe or 'run')}-approver.jsonl")


class ApproverAgent:
    """A cheap reviewer that only ever refuses, and never fails open."""

    def __init__(
        self,
        *,
        call_fn: Optional[Callable[..., Any]] = None,
        model: str = "",
        provider: str = "",
        api_key: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_chars: int = DEFAULT_MAX_CHARS,
        enabled: bool = True,
        journal_path: str = "",
        task_id: str = "",
    ) -> None:
        """Build an approver. ``call_fn`` is the Boundary-2 model boundary.

        ``call_fn`` is called as ``call_fn(messages, difficulty_hint=...,
        model=...)`` with only the keyword arguments its own signature
        declares, so a scripted double, the real router, and a bare
        ``str -> str`` callable all work unchanged. With no ``call_fn`` the
        Boundary-2 boundary is resolved lazily from ``harness.deps`` on the
        first review, so constructing an agent never dials a provider.
        """
        self._call_fn = call_fn
        self.model = str(model or "")
        self.provider = str(provider or "")
        self.api_key = str(api_key or "")
        self.timeout_s = _bounded_number(
            timeout_s, DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S
        )
        self.max_chars = int(max_chars or DEFAULT_MAX_CHARS)
        self.enabled = bool(enabled)
        self.journal_path = str(journal_path or "")
        self.task_id = str(task_id or "")
        #: Every verdict this agent produced, in order. Kept so a run can
        #: render "what did the approver think" without reading the journal.
        self.decisions: List[ApproverReply] = []
        #: Why a review was refused before any model call, when it was.
        self.refusals: List[str] = []

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]] = None,
        *,
        call_fn: Optional[Callable[..., Any]] = None,
        journal_path: str = "",
        task_id: str = "",
    ) -> "ApproverAgent":
        """Build an agent from a resolved config, opt-in by key presence.

        ``approver_agent`` must be PRESENT and truthy. Absent, ``None`` and
        ``False`` all mean "no approver model in this run", which is the
        historical behaviour: an agent that silently started judging actions
        would add a model call to every privileged action of every run.
        """
        values = dict(config or {})
        raw = values.get(APPROVER_ENABLED_KEY)
        enabled = False
        if raw is not None and not isinstance(raw, bool):
            enabled = str(raw).strip().casefold() in {"true", "yes", "on", "1"}
        else:
            enabled = bool(raw)
        return cls(
            call_fn=call_fn,
            model=str(values.get(APPROVER_MODEL_KEY) or ""),
            timeout_s=_bounded_number(
                values.get(APPROVER_TIMEOUT_KEY),
                DEFAULT_TIMEOUT_S,
                MIN_TIMEOUT_S,
                MAX_TIMEOUT_S,
            ),
            max_chars=int(values.get(APPROVER_MAX_CHARS_KEY) or DEFAULT_MAX_CHARS),
            enabled=enabled,
            journal_path=journal_path,
            task_id=task_id,
        )

    # -- the model boundary ------------------------------------------------

    def _resolve_call_fn(self) -> Callable[..., Any]:
        if self._call_fn is not None:
            return self._call_fn
        from harness.deps import get_call_model

        return get_call_model()

    def _call(self, prompt: str) -> str:
        """Invoke the model boundary with only the kwargs it accepts."""
        call_fn = self._resolve_call_fn()
        messages = [{"role": "user", "content": prompt}]
        try:
            import inspect

            parameters = inspect.signature(call_fn).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs: Dict[str, Any] = {}
        if accepts_kwargs or "difficulty_hint" in parameters:
            kwargs["difficulty_hint"] = APPROVER_DIFFICULTY_HINT
        if self.model and (accepts_kwargs or "model" in parameters):
            kwargs["model"] = self.model
        if self.provider and (accepts_kwargs or "provider" in parameters):
            kwargs["provider"] = self.provider
        supported = {
            name: value
            for name, value in kwargs.items()
            if accepts_kwargs or name in parameters
        }
        if supported or accepts_kwargs or not parameters:
            reply = call_fn(messages, **supported)
        else:
            # The boundary declared no keyword at all (a bare `messages ->
            # str` double). Asking it anyway would raise, and a TypeError
            # from the model boundary must read as a denial, not as a crash.
            reply = call_fn(messages)
        return "" if reply is None else str(reply)

    def _call_with_deadline(self, prompt: str) -> Tuple[str, str]:
        """Return ``(reply, failure)``; ``failure`` is "" or the refusal.

        The reviewer runs on a daemon thread and the wait is bounded. A
        thread cannot be killed, so a timeout leaves the call running; the
        point is that the DECISION is not waiting for it. That is the whole
        fail-closed property: an unbounded wait would eventually produce
        whatever the slow model said, and a slow answer is indistinguishable
        from a hung one at the boundary that matters.
        """
        holder: Dict[str, Any] = {"reply": "", "failure": ""}

        def _run() -> None:
            try:
                holder["reply"] = self._call(prompt)
            except BaseException as exc:
                holder["failure"] = f"model_error: {type(exc).__name__}: {exc}"

        worker = threading.Thread(target=_run, name="neo-approver", daemon=True)
        worker.start()
        worker.join(self.timeout_s)
        if worker.is_alive():
            return "", "timeout"
        if holder["failure"]:
            return "", str(holder["failure"])
        return str(holder["reply"]), ""

    # -- the decision ------------------------------------------------------

    def review(
        self,
        effect: Any,
        *,
        origin: Optional[ApprovalOrigin] = None,
        requires_human: Optional[bool] = None,
        privileged_reason: str = "",
        containment: Optional[Mapping[str, Any]] = None,
    ) -> ApproverReply:
        """Judge one already-privileged action. Returns a verdict; never raises.

        The order of the checks IS the security property, so it is worth
        stating: admissibility first (so an unprivileged action cannot reach a
        model at all), then the enable switch, then the bounded call, then the
        fail-closed parse. Nothing after the parse can turn a denial into an
        approval.

        ``requires_human`` may be supplied by a caller that already evaluated
        the policy (a :class:`~harness.agent_kernel.policy.SafetyReceipt`
        derives it). When it is omitted the reviewer is not asked to infer it
        from the effect, because a cheap model inferring "is this privileged"
        is exactly the widening this module exists to prevent.
        """
        who = origin or ApprovalOrigin()
        try:
            canonical = (
                effect
                if hasattr(effect, "digest")
                else canonical_effect("unknown", str(effect or ""))
            )
            request = ApproverRequest(
                effect=canonical,
                origin=who,
                requires_human=bool(requires_human),
                privileged_reason=privileged_reason,
                containment=dict(containment or {}),
            )
            admissible, why = approver_admissible(request)
            if not admissible:
                self.refusals.append(why)
                return self._record(
                    ApproverReply.failed_verdict(
                        "not_privileged", raw=why, model=self.model
                    ),
                    request,
                )
            if not self.enabled:
                return self._record(
                    ApproverReply.failed_verdict(
                        "disabled",
                        raw=f"{APPROVER_ENABLED_KEY} is not set for this run",
                        model=self.model,
                    ),
                    request,
                )
            prompt = render_approver_prompt(
                canonical,
                origin=who,
                containment=containment,
                privileged_reason=privileged_reason,
                max_chars=self.max_chars,
            )
            started = time.monotonic()
            reply, failure = self._call_with_deadline(prompt)
            elapsed = time.monotonic() - started
            if failure == "timeout":
                verdict = ApproverReply.failed_verdict(
                    "timeout",
                    raw=f"the reviewer did not answer within {self.timeout_s}s",
                    model=self.model,
                    elapsed_s=elapsed,
                )
            elif failure:
                verdict = ApproverReply.failed_verdict(
                    "model_error", raw=failure, model=self.model, elapsed_s=elapsed
                )
            else:
                parsed = parse_approver_reply(reply)
                verdict = ApproverReply(
                    decision=parsed.decision,
                    reason=parsed.reason,
                    failure=parsed.failure,
                    raw=parsed.raw,
                    model=self.model,
                    elapsed_s=elapsed,
                )
            return self._record(verdict, request)
        except BaseException as exc:
            verdict = ApproverReply.failed_verdict(
                "model_error", raw=f"{type(exc).__name__}: {exc}", model=self.model
            )
            self.decisions.append(verdict)
            self._journal(verdict, tool="", effect_digest="")
            return verdict.with_origin(who)

    def _record(
        self, verdict: ApproverReply, request: ApproverRequest
    ) -> ApproverReply:
        """Attach the origin, remember the verdict, and journal it."""
        labelled = verdict.with_origin(request.origin)
        self.decisions.append(labelled)
        self._journal(
            labelled,
            tool=request.effect.tool,
            render=request.effect.render,
            effect_digest=request.effect.digest,
        )
        return labelled

    def _journal(
        self,
        verdict: ApproverReply,
        *,
        tool: str = "",
        render: str = "",
        effect_digest: str = "",
    ) -> None:
        """Append one redacted row; a write failure is a note, never a crash."""
        if not self.journal_path:
            return
        entry = ApproverJournalEntry(
            verdict=verdict,
            tool=tool,
            render=render,
            effect_digest=effect_digest,
            task_id=self.task_id,
            at=time.time(),
        )
        append_journal(self.journal_path, entry)

    def report(self) -> Dict[str, Any]:
        """Return a receipt of this agent's run: counts, model, refusals."""
        approved = [item for item in self.decisions if item.approved]
        failed = [item for item in self.decisions if item.failed]
        return {
            "enabled": self.enabled,
            "model": self.model or "(router default, easy tier)",
            "timeout_s": self.timeout_s,
            "fail_closed": True,
            "reviews": len(self.decisions),
            "approved": len(approved),
            "denied": len(self.decisions) - len(approved),
            "failed_closed": len(failed),
            "failures": sorted({item.failure for item in failed if item.failure}),
            "not_privileged": sum(
                1 for item in self.decisions if item.failure == "not_privileged"
            ),
            "inadmissible": list(self.refusals),
            "journal_path": self.journal_path,
        }

    def summary(self) -> str:
        """Return the one quotable line a human reads after a run."""
        report = self.report()
        if not self.decisions:
            return "approver agent: no privileged action was reviewed"
        return (
            f"approver agent ({report['model']}): {report['approved']} approved, "
            f"{report['denied']} denied of {report['reviews']} reviewed "
            f"(fail-closed: {report['failed_closed']}"
            + (
                f", failures: {', '.join(report['failures'])}"
                if report["failures"]
                else ""
            )
            + ")"
        )


def append_journal(path: Any, entry: ApproverJournalEntry) -> Optional[str]:
    """Append one redacted approver row to ``path``; return it, or None.

    Best-effort by contract: an unwritable journal must not end a run. The
    failure is not silent either -- ``None`` is returned instead of a path, so
    a caller that cares can report that the decision went unrecorded.
    """
    target = Path(str(path)).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=False)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, ValueError):
        return None
    return str(target)


def review_privileged_action(
    effect: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
    origin: Optional[ApprovalOrigin] = None,
    requires_human: Optional[bool] = None,
    privileged_reason: str = "",
    containment: Optional[Mapping[str, Any]] = None,
    call_fn: Optional[Callable[..., Any]] = None,
    journal_path: str = "",
    task_id: str = "",
) -> ApproverReply:
    """One-call review: build the agent from config, judge, return the verdict.

    The convenience form for a call site that has no other use for the agent.
    It cannot do anything the two-step form cannot, and it does not change
    the ORDER of the checks -- the agent is still the thing that decides
    admissibility before any model call.
    """
    agent = ApproverAgent.from_config(
        config, call_fn=call_fn, journal_path=journal_path, task_id=task_id
    )
    return agent.review(
        effect,
        origin=origin,
        requires_human=requires_human,
        privileged_reason=privileged_reason,
        containment=containment,
    )
