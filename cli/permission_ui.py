r"""The ONE place a permission outcome becomes text, a status, and an exit code.

The problem this module exists to close
--------------------------------------
Phase 2 built a real permission model -- four modes in
:mod:`cli.approval_modes`, a policy engine in ``shared.policy_lang``, a hardline
floor in ``runtime.hardline``, an aux-LLM gate in ``runtime.approver_gate``, and
arity-scoped grants in ``shared.approval``. Measured on the tree this module
was written against:

.. code-block:: text

    $ grep -l 'explain_line' cli/*.py | grep -v test_
    (no output)
    $ grep -l 'hardline\|floor_refused' cli/*.py | grep -v test_
    cli/approval_modes.py

So the seven-outcome vocabulary and the floor-refusal word had **no production
renderer at all**, and both shells hand-rolled their own strings. That is not a
cosmetic gap:

* ``cli/tui.py`` and ``cli/interactive.py`` rendered a timeout in **two
  different tones** and with **five different sentences**, so a reader could not
  tell one deadline from another.
* A **rejection** was rendered ``neo.error`` in the REPL's watcher and
  ``neo.warn`` in the TUI's agent approver -- the same outcome, two colours.
* Nothing anywhere rendered a **floor refusal**, because nothing in ``cli/``
  imported ``runtime.hardline``.
* ``cli/headless.py`` carries **no permission key at all**, so a script
  consuming a permission refusal gets only a status word and no reason.

A permission model a user cannot see is a permission model they must trust on
faith, and this repository's doctrine is that a receipt which cannot be read is
the defect.

The five properties, and where each one is
-----------------------------------------
=====================================  ==========================================
:func:`render`                          never empty, always actionable, sanitised
:func:`status_for` / :func:`exit_code_for`  the same word and number on 3 surfaces
:data:`TIMEOUT_OUTCOMES`               a timeout is never a rejection, structurally
:func:`assert_no_override_offered`      a floor refusal offers no way around it
:func:`assert_denial_never_renders_as_approval`  asserted at the RENDER layer
=====================================  ==========================================

Why the outcome vocabulary is SEPARATE from the status vocabulary
----------------------------------------------------------------
``cli.commands.COMMAND_STATUSES`` is what a *record* and a script see:
``ok | unknown | not_command | invalid | disabled | hidden | refused | queued |
flag | failed | error | cancelled``. :data:`PERMISSION_OUTCOMES` is what a
*person* sees: ``allow | ask_granted | ask_denied | ask_timed_out |
floor_refused | mode_blocked | capability_withheld``. They are different
questions -- "what happened to this action" versus "what happened to this
command" -- and collapsing them is how ``Ctrl+C`` became ``error``/1 in two
shells and ``cancelled``/130 in the third, which ``cli/AGENTS.md`` records as
the parity defect this repository has already paid for once.

So :func:`status_for` is a **declared, total, fail-closed** reduction rather
than a derivation somebody re-implements per surface, and every refusal maps to
``refused`` on all three surfaces while the *outcome* word keeps its detail.

The one-line refusals, and the reason they live here
----------------------------------------------------
:func:`render` returns PLAIN lines. Every value that reaches it -- the attempted
command, a path, an issue excerpt -- is **attacker-controlled content**: an issue
body, a filename out of a hostile repository, a diff. So every line goes through
:func:`cli.ui.sanitize_text`, the single entry point every render path in
``cli/`` uses. A renderer that bypassed it would be a P0 injection surface, and
the whole point of a central mount is that there is nowhere else to bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from cli import approval_modes as _am
from cli import commands as _commands
from cli import ui

__all__ = [
    "DENIAL_OUTCOMES",
    "EXOTIC_OUTCOMES",
    "FLOOR_OUTCOMES",
    "REJECTION_OUTCOMES",
    "SURFACES",
    "TIMEOUT_OUTCOMES",
    "PermissionReceipt",
    "approval_surface_report",
    "assert_denial_never_renders_as_approval",
    "assert_no_override_offered",
    "assert_timeout_is_not_a_rejection",
    "exit_code_for",
    "explain_lines",
    "outcome_for_gate_decision",
    "outcome_for_resolution",
    "render",
    "status_for",
    "surface_facts",
]


#: The three surfaces a permission outcome can be observed on. Declared so a
#: parity probe can iterate the product's own enumeration rather than a list
#: somebody typed in a test.
SURFACES: Tuple[str, ...] = ("tui", "repl", "headless")

#: Outcomes where nobody answered. **This is a SEPARATE vocabulary from
#: :data:`REJECTION_OUTCOMES` on purpose.**
#:
#: A timeout and a rejection are different events with different meanings: a
#: rejection is a DECISION, and a timeout is the ABSENCE of one. Conflating them
#: corrupts the audit trail -- `cli/AGENTS.md`'s own P1 record names a real
#: defect in which both call sites read ``None`` as "declined" and logged a
#: timed-out approval as a REJECTION. Keeping the two sets disjoint here means a
#: renderer that treats them alike has nothing to treat alike.
TIMEOUT_OUTCOMES: Tuple[str, ...] = (
    _am.OUTCOME_ASK_TIMED_OUT,
    "gate_timeout",
    "approver_timeout",
)

#: Outcomes where a human said no. Disjoint from :data:`TIMEOUT_OUTCOMES` by
#: construction, and :func:`assert_timeout_is_not_a_rejection` proves it.
REJECTION_OUTCOMES: Tuple[str, ...] = (
    _am.OUTCOME_ASK_DENIED,
    "gate_rejected",
    "gate_malformed_reply",
    "gate_empty_reply",
    "gate_ambiguous_reply",
    "approver_denied",
    "approver_no_reason",
)

#: Outcomes where somebody said YES. Exactly two, and they are the only
#: outcomes that are not a denial -- everything else, recognised or not, is.
#:
#: The first version of :func:`is_denial` said "everything except
#: ``OUTCOME_ALLOW`` is a denial", which made ``ask_granted`` render as
#: ``refused`` with the words *"you approved this action"*. That is the worst
#: class of permission-UI bug there is: a machine-readable ``status: refused``
#: beside a sentence saying the human approved it. It was caught by driving
#: :func:`approval_surface_report` and reading every row, not by reading this
#: function. The two approvals are named here rather than inferred, so adding a
#: seventh outcome defaults to a REFUSAL -- the fail-closed direction.
APPROVAL_OUTCOMES: Tuple[str, ...] = (
    _am.OUTCOME_ALLOW,
    _am.OUTCOME_ASK_GRANTED,
)

#: Outcomes a HARD FLOOR produced. No grant, mode, environment variable, or
#: injected text can lift one, so the renderer for these is the only place that
#: must never offer a way around it.
FLOOR_OUTCOMES: Tuple[str, ...] = (
    _am.OUTCOME_FLOOR_REFUSED,
    "floor_denied",
)

#: Every outcome that is a REFUSAL. **Not** the same set as "everything that is
#: not an approval": :func:`is_denial` is the fail-closed default and answers
#: ``True`` for anything unknown, while this tuple is the ENUMERATION the
#: render-layer pin walks. The pin therefore needs every known refusal named
#: here -- so a new refusal is either added to this tuple, where the pin will
#: test its rendering, or it is a word nobody has heard of yet, where
#: :func:`is_denial` already refuses it by default.
DENIAL_OUTCOMES: Tuple[str, ...] = (
    _am.OUTCOME_ASK_DENIED,
    _am.OUTCOME_ASK_TIMED_OUT,
    _am.OUTCOME_FLOOR_REFUSED,
    _am.OUTCOME_MODE_BLOCKED,
    _am.OUTCOME_CAPABILITY_WITHHELD,
    "subagent_escalation",
    "untraceable_escalation",
    "stale_grant",
    "revoked_grant",
    "approver_disabled",
    "approver_model_error",
    *TIMEOUT_OUTCOMES,
    *REJECTION_OUTCOMES,
    *FLOOR_OUTCOMES,
)

#: The exotic outcomes the brief names, each with its OWN reason and next step.
#:
#: Without this table every exotic outcome collapses onto ``mode_blocked`` and
#: reads as "the mode did not offer this tool", which is a **lie about the
#: cause**: an untraceable subagent escalation was refused because nobody can
#: say who asked, and telling the reader to switch modes sends them somewhere
#: useless. An outcome nobody defined is still rendered -- as ``mode_blocked``
#: with a note naming the unknown word -- so a blank permission UI is not
#: reachable, but a *wrong* one is not reachable either.
EXOTIC_OUTCOMES: Dict[str, str] = {
    "gate_malformed_reply": (
        "the approver replied with something this build cannot parse, so it "
        "denied; nothing was decided by a human"
    ),
    "gate_empty_reply": (
        "the approver replied with nothing, so it denied; nobody decided this"
    ),
    "gate_ambiguous_reply": (
        "the approver's reply named both an approval and a denial, so it "
        "denied; re-ask rather than guess"
    ),
    "subagent_escalation": (
        "a subagent escalated this to you; the decision is yours, and the "
        "escalation's own history is not shown here"
    ),
    "untraceable_escalation": (
        "a subagent escalated this and its origin cannot be traced, so it was "
        "refused outright rather than shown to you as if the main thread asked"
    ),
    "mode_changed_mid_flight": (
        "the permission mode changed while this action was in flight; the "
        "action was decided under the mode named in this line, not the one "
        "shown on the status rail now"
    ),
    "stale_grant": (
        "the grant you were relying on no longer applies; a grant is revocable "
        "and expiring, and this one is neither remembered nor in force"
    ),
    "revoked_grant": (
        "this grant was revoked; revocation takes effect immediately, so the "
        "action asked again"
    ),
    "approver_disabled": (
        "no reviewer agent is configured for this run, so no second opinion "
        "was obtained; your own decision stands"
    ),
    "approver_model_error": (
        "the reviewer's model call failed, so it denied; this is not a "
        "considered refusal and nothing was decided by a model"
    ),
    "gate_not_requested": (
        "no approval request was ever made for this action, so nothing was "
        "asked and nothing was granted; this is not a change to an earlier "
        "permission you gave"
    ),
    "gate_pending": (
        "the run ended before any approval decision was recorded, so this "
        "action was never decided by a human"
    ),
    "floor_denied": (
        "a hard floor refused this and no permission level can allow it; "
        "change what you asked for, or change the floor deliberately and "
        "knowing that you did"
    ),
    "gate_timeout": "nobody answered before the deadline; a timeout is not a rejection",
    "approver_timeout": (
        "the reviewer did not answer within its budget, so it denied; nothing "
        "was decided by a model"
    ),
}

#: Next steps per exotic outcome, so an unusual refusal still names a door
#: somebody can walk through. ``""`` means "this outcome's own reason already
#: names what to do", and the generic fallbacks still apply.
_EXOTIC_NEXT_STEPS: Dict[str, Tuple[str, ...]] = {
    "gate_malformed_reply": ("/approval mode default", "/doctor"),
    "gate_empty_reply": ("/approval mode default", "/doctor"),
    "gate_ambiguous_reply": ("/approval mode default", "/doctor"),
    "subagent_escalation": ("/approval", "/trace"),
    "untraceable_escalation": ("/trace", "/approval"),
    "mode_changed_mid_flight": ("/approval", "/status"),
    "stale_grant": ("/approval grants", "/approval"),
    "revoked_grant": ("/approval grants", "/approval"),
    "approver_disabled": ("/approval",),
    "approver_model_error": ("/doctor", "/approval"),
    "gate_not_requested": ("/approval", "/status"),
    "gate_pending": ("/trace", "/approval"),
    "floor_denied": ("/approval", "/doctor"),
    "gate_timeout": ("/approval mode default",),
    "approver_timeout": ("/doctor",),
}


# ---------------------------------------------------------------------------
# 1. The reduction: outcome -> status, outcome -> exit code
# ---------------------------------------------------------------------------


def _canonical(outcome: Any) -> str:
    """Return the outcome word, lowercased, or ``""`` for none."""
    return str(outcome or "").strip().lower()


def is_denial(outcome: Any) -> bool:
    """Whether this outcome is a REFUSAL.

    An outcome is an approval only if it is in :data:`APPROVAL_OUTCOMES`. Every
    other word -- recognised, exotic, or unknown -- is a refusal, which is the
    fail-closed direction: an unrecognised permission outcome arriving at a
    renderer must not be assumed to be consent.
    """
    key = _canonical(outcome)
    if not key:
        return True
    return key not in APPROVAL_OUTCOMES


def status_for(outcome: Any) -> str:
    """Return the record status a surface publishes for this outcome.

    Total and fail-closed: an unrecognised outcome is ``refused``, never
    ``ok``. ``refused`` rather than ``error`` because a permission refusal is a
    correct outcome of a working gate, not a failure of the CLI -- and a script
    that treats it as an error cannot tell "the system stopped you" from "the
    product broke".
    """
    if not is_denial(outcome):
        return "ok"
    return "refused"


def exit_code_for(outcome: Any) -> int:
    """Return the exit code a surface reports for this outcome.

    A refusal is ``2`` (``usage_error``) rather than ``1``, and the reason is
    the one ``cli.exit_codes`` documents: exit 1 means "the harness ran and did
    not produce a fix", which is a **statement about a run**. A permission
    refusal is a statement about a **gate**, and conflating them is how a CI
    job learned to read a refusal as a flaky test.
    """
    from cli.exit_codes import EXIT_CODES

    return EXIT_CODES["success"] if not is_denial(outcome) else EXIT_CODES[
        "usage_error"
    ]


def outcome_for_gate_decision(decision: Any) -> str:
    """Map a ``review.log`` decision word onto the permission vocabulary.

    The gate's own vocabulary (``not_requested | approved | rejected | timeout |
    pending`` -- ``cli.commands.approval_gate_outcome``) is five words and none
    of them is a permission outcome. This is the ONE bridge between them, so the
    three surfaces cannot each invent their own mapping -- and
    ``not_requested``/``pending`` deliberately map to **their own refusals**, not
    to a borrowed word:

    * ``not_requested`` -> ``gate_not_requested``. Nothing ever asked, so there
      was no grant to go stale. Folding it into ``stale_grant`` -- which the
      first version of this function did -- renders *"the grant you were relying
      on no longer applies"* about a gate that never granted anything, which is a
      fabricated history told to somebody deciding what to do next.
    * ``pending`` -> ``gate_pending``. The run ended before a decision was
      recorded, which is its own fact and its own next step (look at the run).

    Both still REFUSE, because a gate that did not approve anything must not be
    published as consent.
    """
    key = _canonical(decision)
    if key == "approved":
        return _am.OUTCOME_ASK_GRANTED
    if key == "rejected":
        return _am.OUTCOME_ASK_DENIED
    if key == "timeout":
        return _am.OUTCOME_ASK_TIMED_OUT
    if key == "pending":
        return "gate_pending"
    if key == "not_requested":
        return "gate_not_requested"
    if key == "":
        return "gate_pending"
    return "gate_malformed_reply"


# ---------------------------------------------------------------------------
# 2. The receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionReceipt:
    """One permission outcome, rendered for a person and recorded for a script.

    The two halves are carried together on purpose. ``lines`` is what a human
    reads; ``status``/``exit_code``/``outcome`` is what a script branches on.
    A renderer that produced only the first is unusable from automation and a
    renderer that produced only the second is unreadable in a terminal, and the
    parity defect this module closes is exactly the two drifting apart.
    """

    outcome: str
    decision: str
    status: str
    exit_code: int
    action: str
    reason: str
    next_step: str
    lines: Tuple[str, ...]
    commands: Tuple[str, ...] = ()
    surface: str = ""
    mode: str = ""
    tool: str = ""
    floor: str = ""
    #: Whether this receipt offers any way to get past a floor refusal. It is
    #: False for every outcome and the gate proves it; the field exists so a
    #: caller can ASK rather than have to know.
    offers_override: bool = False
    #: Whether the underlying failure WAS a traceback. Never rendered as one
    #: unless it is one.
    is_traceback: bool = False
    notes: Tuple[str, ...] = ()
    withheld: int = 0

    @property
    def timed_out(self) -> bool:
        """Whether this receipt is a TIMEOUT, which is not a rejection."""
        return _canonical(self.outcome) in TIMEOUT_OUTCOMES

    @property
    def is_floor(self) -> bool:
        """Whether a hard floor produced this receipt."""
        return _canonical(self.outcome) in FLOOR_OUTCOMES

    @property
    def denied(self) -> bool:
        """Whether the action was refused."""
        return self.decision == "refused"

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly receipt, safe to publish from ``--json``.

        Carries the rendered ``lines`` so a script reads the SAME sentence a
        person read. A headless consumer that had to re-derive the wording is a
        consumer that can disagree with the terminal, which is the defect this
        module exists to remove.
        """
        return {
            "outcome": self.outcome,
            "decision": self.decision,
            "status": self.status,
            "exit_code": int(self.exit_code),
            "action": self.action,
            "reason": self.reason,
            "next_step": self.next_step,
            "lines": list(self.lines),
            "commands": list(self.commands),
            "surface": self.surface,
            "mode": self.mode,
            "tool": self.tool,
            "floor": self.floor,
            "timed_out": self.timed_out,
            "is_floor": self.is_floor,
            "offers_override": bool(self.offers_override),
            "is_traceback": bool(self.is_traceback),
            "notes": list(self.notes),
            "withheld": int(self.withheld),
        }

    def json_line(self) -> str:
        """Return the receipt as ONE JSON line."""
        import json

        return json.dumps(self.to_dict(), sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# 3. The render path
# ---------------------------------------------------------------------------


def _sanitise(value: Any) -> str:
    """Return display-safe text through the single entry point.

    Every permission string is attacker-controlled: the attempted command comes
    from a model's tool call, a path from a repository the user did not write, an
    issue excerpt from a bug report. :func:`cli.ui.sanitize_text` is THE one
    entry point every render path in ``cli/`` uses, and it fails CLOSED -- a
    sanitiser that is unavailable or raises withholds the value rather than
    passing it through. Centralising the mount means there is no second path
    that could forget.
    """
    try:
        return ui.sanitize_text(value)
    except Exception:
        # Fail closed, and say so in words. Rendering the raw value here would
        # make the sanitiser's failure a disclosure.
        return "(detail withheld: the sanitiser was unavailable)"


def _describe(effect: Any, tool: Any) -> str:
    """Return the WHAT half, preferring the caller's own words."""
    text = _sanitise(effect).strip()
    if text:
        words = text.split()
        verb = words[0] if words else text
        return f"run `{verb}`"
    name = _sanitise(getattr(tool, "name", None) or tool or "").strip().lower()
    return f"use the {name} tool" if name else "take a side-effecting action"


def _commands_for(outcome: str) -> Tuple[str, ...]:
    """Return the runnable next steps for ``outcome``, never empty.

    ``/doctor`` and ``/trace`` ride on every non-approval receipt, which is the
    precedent ``cli.runview.failure_lines`` set: "a classifier action can be
    pure diagnosis, which is correct advice and useless as an instruction". An
    outcome nobody can act on is not a refusal, it is a dead end.
    """
    steps = _EXOTIC_NEXT_STEPS.get(outcome)
    if steps:
        return tuple(steps)
    if outcome in _am.PERMISSION_OUTCOMES:
        available = [
            item for item in _am.explain(outcome, mode="").commands if item.startswith("/")
        ]
        if available:
            return tuple(available)
    return ("/approval",)


def render(
    outcome: Any,
    *,
    effect: Any = "",
    tool: Any = "",
    reason: str = "",
    floor: Any = "",
    mode: Any = "",
    surface: str = "",
    width: int = 0,
    commands: Sequence[str] = (),
    traceback: Any = None,
    extra: Sequence[str] = (),
) -> PermissionReceipt:
    """Return the ONE receipt for any permission outcome. Never raises.

    Four guarantees, each of which has failed somewhere in this repository's
    history and is therefore structural here rather than conventional:

    1. **Never empty.** At least one line, and it always names the action, the
       rule that decided it, and a runnable next step.
    2. **Never a traceback unless it is one.** ``traceback=True`` is only
       honoured when the caller passes an exception that IS one; a refused
       action is a sentence, not a stack dump.
    3. **A timeout never renders as a rejection** and **a denial never renders
       as an approval** -- both asserted at THIS layer in
       :func:`assert_denial_never_renders_as_approval`, because a rendering bug
       that inverts a decision is catastrophic and invisible.
    4. **No override of a floor.** :data:`offers_override` is False for every
       outcome and :func:`assert_no_override_offered` proves it over the whole
       floor vocabulary.
    """
    key = _canonical(outcome)
    recognised = key in _am.PERMISSION_OUTCOMES or key in DENIAL_OUTCOMES
    canonical = key if recognised else _am.OUTCOME_MODE_BLOCKED

    action = _describe(effect, tool)
    mode_key = _am.normalize_mode(mode) if str(mode or "").strip() else ""

    why = _sanitise(reason).strip()
    if not why:
        why = EXOTIC_OUTCOMES.get(key) or _am.explain(
            canonical,
            mode=mode_key,
            floor=_sanitise(floor),
        ).reason

    step = str(commands[0]).strip() if commands else ""
    # A LIST, because the fallback loop below appends to it. `tuple(...)` here
    # is the kind of small slip that turns a renderer into a crash at the moment
    # of a permission refusal.
    runnable = [
        item for item in (list(commands) + list(_commands_for(canonical))) if item
    ]
    for fallback in ("/doctor", "/trace"):
        if not is_denial(canonical):
            break
        if fallback not in runnable:
            runnable.append(fallback)
    if not step:
        step = runnable[0] if runnable else "/approval"

    notes = [_sanitise(item).strip() for item in (extra or ()) if str(item).strip()]
    if not recognised:
        notes.insert(
            0,
            f"outcome {key or '(blank)'!r} is not one this build defines; "
            f"rendered as {canonical!r} so the refusal is still visible",
        )

    decision = "allowed" if not is_denial(canonical) else "refused"
    verb = _denial_verb(canonical)
    summary = _sanitise(
        f"{verb}: {action} - {why} - next: {step}"
    )
    lines: List[str] = [summary]
    lines.extend(f"  {note}" for note in notes)
    if runnable and not any(item in summary for item in runnable):
        lines.append(f"  next: {', '.join(runnable)}")

    bounded: List[str] = []
    for line in lines:
        bounded.append(_clip(line, width))

    is_traceback = bool(traceback) if isinstance(traceback, BaseException) else False

    return PermissionReceipt(
        outcome=canonical,
        decision=decision,
        status=status_for(canonical),
        exit_code=exit_code_for(canonical),
        action=action,
        reason=why,
        next_step=step,
        lines=tuple(bounded),
        commands=runnable,
        surface=str(surface or ""),
        mode=mode_key,
        tool=_sanitise(getattr(tool, "name", None) or tool or "").strip().lower(),
        floor=_sanitise(floor).strip(),
        offers_override=False,
        is_traceback=is_traceback,
        notes=tuple(notes),
        withheld=int(ui.sanitize_report().get("withheld", 0) or 0),
    )


def _denial_verb(outcome: str) -> str:
    """Return the leading WORD for this outcome, and it must never be "allowed".

    One word, chosen from a closed table, is what makes the render-layer pin
    possible: the pin looks for this vocabulary in the rendered text, so a
    refusal whose sentence begins ``approved`` is caught by reading the string a
    person would actually read.
    """
    if outcome in _am.PERMISSION_OUTCOMES:
        return {
            _am.OUTCOME_ALLOW: "allowed",
            _am.OUTCOME_ASK_GRANTED: "granted once",
            _am.OUTCOME_ASK_DENIED: "refused",
            _am.OUTCOME_ASK_TIMED_OUT: "timed out",
            _am.OUTCOME_FLOOR_REFUSED: "refused by a hard floor",
            _am.OUTCOME_MODE_BLOCKED: "refused",
            _am.OUTCOME_CAPABILITY_WITHHELD: "refused",
        }[outcome]
    if outcome in TIMEOUT_OUTCOMES:
        return "timed out"
    if outcome in REJECTION_OUTCOMES:
        return "refused"
    if outcome in FLOOR_OUTCOMES:
        return "refused by a hard floor"
    return "refused"


def _clip(line: str, width: int) -> str:
    """Clip with an explicit marker, never a bare cut."""
    limit = int(width or 0)
    text = str(line or "")
    if limit > 0 and len(text) > limit:
        return text[: max(1, limit - 3)] + "..."
    return text


def explain_lines(receipt: Any, *, width: int = 0) -> List[str]:
    """Return the receipt's lines as a list, sanitised at the boundary.

    Total: a receipt built from a mapping, a string, or a raw outcome all
    render, because a surface that has to special-case a shape will eventually
    forget one and the failure is a blank permission card.
    """
    if isinstance(receipt, PermissionReceipt):
        return [_clip(_sanitise(line), width) for line in receipt.lines]
    if isinstance(receipt, Mapping):
        return list(receipt.get("lines") or []) or ["(no permission receipt was recorded)"]
    return list(render(receipt, width=width).lines)


# ---------------------------------------------------------------------------
# 4. The three gates
# ---------------------------------------------------------------------------


def assert_no_override_offered(receipts: Iterable[Any] = ()) -> None:
    """Raise if ANY receipt offers a way past a hard floor.

    Two shapes an override could take, and the gate needs both:

    * the structural ``offers_override`` field, and
    * an override WORD in the **action lines** -- the headline and the runnable
      next steps, which are the only places a UI can present something.

    **Why the scan is scoped to the action lines and not the whole receipt.**
    The first version scanned every rendered line and fired on the floor's own
    refusal sentence, *"a hard floor refused this and no grant can override
    it"* -- a sentence that is the OPPOSITE of an offer. That is this
    repository's recorded "a gate that punishes correct behaviour" class: a
    blanket substring scan cannot tell a refusal from an offer, and a gate that
    cannot is one nobody keeps. So:

    * the scan covers the headline and the ``next:`` line, which is where a UI
      presents an action; and
    * the floor's own wording in :mod:`cli.approval_modes` was changed anyway,
      because a sentence that says "no grant can override it" invites exactly
      this confusion in a human reader too. It now says *"no permission level
      can allow it"*, which is the same claim in words that cannot be misread as
      an affordance.

    The override vocabulary is closed so a synonym nobody thought of cannot slip
    past, and the exemption list is empty **on purpose** -- an exemption list is
    a place for the next floor refusal to hide.
    """
    forbidden = (
        "override",
        "bypass the floor",
        "approve anyway",
        "force this",
        "ignore the floor",
        "--force-approve",
    )
    subjects: List[PermissionReceipt] = []
    for outcome in FLOOR_OUTCOMES:
        subjects.append(
            render(outcome, effect="rm -rf /", floor="filesystem_root_wipe")
        )
    for item in receipts or ():
        if isinstance(item, PermissionReceipt):
            subjects.append(item)
        else:
            subjects.append(render(item, effect="rm -rf /"))
    for receipt in subjects:
        if receipt.offers_override:
            raise AssertionError(
                f"receipt for {receipt.outcome!r} offers an override of a hard "
                "floor; a floor is not something a receipt can offer a way past"
            )
        # The ACTION lines: the headline, and any line that presents a command.
        action_lines = [receipt.lines[0]] + [
            line for line in receipt.lines if line.strip().startswith("next:")
        ]
        for line in action_lines:
            lowered = line.lower()
            for word in forbidden:
                if word in lowered:
                    raise AssertionError(
                        f"receipt for {receipt.outcome!r} presents the override "
                        f"word {word!r} on an action line: {line!r}"
                    )

    # The control arm. A scanner that cannot fail is a scanner that measures
    # nothing, and this repository has three of those recorded in
    # cli/AGENTS.md. If this does not raise, the gate above is vacuous.
    probe = render("floor_refused", effect="rm -rf /")
    offered = PermissionReceipt(
        outcome=probe.outcome,
        decision="refused",
        status=probe.status,
        exit_code=probe.exit_code,
        action=probe.action,
        reason=probe.reason,
        next_step=probe.next_step,
        lines=(
            "refused by a hard floor: run `rm` - filesystem_root_wipe - next: "
            "/approval",
            "  next: override the floor with --force-approve",
        ),
        commands=probe.commands,
    )
    try:
        assert_no_override_offered([offered])
    except AssertionError:
        return
    raise AssertionError(
        "the no-override gate did not fire on a receipt whose action line "
        "offers '--force-approve'; the gate is vacuous and proves nothing"
    )


def assert_timeout_is_not_a_rejection() -> None:
    """Raise if a timeout and a rejection can produce the same receipt.

    Two checks, because there are two ways to conflate them:

    * the **vocabularies** are disjoint -- no word is in both
      :data:`TIMEOUT_OUTCOMES` and :data:`REJECTION_OUTCOMES`; and
    * the **rendered text** differs -- every timeout's leading verb is ``timed
      out`` and no rejection's is, so a reader scanning for the word
      ``refused`` never sees a timeout described as one.
    """
    overlap = set(TIMEOUT_OUTCOMES) & set(REJECTION_OUTCOMES)
    if overlap:
        raise AssertionError(
            f"outcomes {sorted(overlap)} are in BOTH the timeout and the "
            "rejection vocabulary, so a renderer cannot tell them apart"
        )
    for outcome in TIMEOUT_OUTCOMES:
        receipt = render(outcome, effect="edit", tool="edit")
        if not receipt.timed_out:
            raise AssertionError(
                f"{outcome!r} rendered as {receipt.outcome!r}, which is not "
                "recognised as a timeout"
            )
        head = receipt.lines[0].split(":", 1)[0].strip().lower()
        if head != "timed out":
            raise AssertionError(
                f"a timeout rendered as {head!r}; a timeout must never read as "
                "a decision anybody made"
            )
    for outcome in REJECTION_OUTCOMES:
        receipt = render(outcome, effect="edit", tool="edit")
        if receipt.timed_out:
            raise AssertionError(
                f"{outcome!r} rendered as a timeout; a rejection is a decision "
                "and must not be recorded as the absence of one"
            )
        head = receipt.lines[0].split(":", 1)[0].strip().lower()
        if head == "timed out":
            raise AssertionError(
                f"a rejection rendered as {head!r}; the two must stay distinct"
            )


#: The words that would mean a refusal is being presented as consent. Closed, so
#: a new synonym cannot slip past, and deliberately SMALL: the check is for
#: a rendering that INVERTS a decision, not for the absence of a word.
_APPROVAL_WORDS: Tuple[str, ...] = (
    "approved",
    "allowed",
    "granted",
    "you may",
    "permitted",
    "auto-approved",
)


def assert_denial_never_renders_as_approval(
    receipts: Iterable[Any] = (),
) -> None:
    """Raise if ANY denial's RENDERED TEXT reads as an approval.

    **Asserted at the render layer, not the decision layer, and that is the
    whole point.** ``PermissionDecision.decision`` is already ``refused`` for
    every denial before this function is called; what this checks is the string
    a person would actually read. A rendering bug that inverts a decision is
    catastrophic *and invisible* -- every machine-readable field would still say
    ``refused`` while the screen said ``approved`` -- and no amount of asserting
    on the dataclass can see it.

    The way it could happen is concrete rather than hypothetical: a reason that
    quotes the reviewer, a note that echoes an approver's wording, an
    attacker-controlled command containing the word ``approved``. So the check
    scans the WHOLE rendered receipt, including the sanitised attacker text,
    and the control arm below proves the scanner can actually fail.
    """
    subjects: List[PermissionReceipt] = [
        render(outcome, effect="edit", tool="edit")
        for outcome in DENIAL_OUTCOMES
    ]
    for item in receipts or ():
        if isinstance(item, PermissionReceipt):
            subjects.append(item)
        else:
            subjects.append(render(item, effect="edit", tool="edit"))

    for receipt in subjects:
        if not receipt.denied:
            raise AssertionError(
                f"{receipt.outcome!r} is in the denial vocabulary but reports "
                f"decision={receipt.decision!r}"
            )
        head = receipt.lines[0].split(":", 1)[0].strip().lower()
        for word in _APPROVAL_WORDS:
            if word in head:
                raise AssertionError(
                    f"a denial rendered as {head!r}: the word {word!r} appears "
                    f"before the first colon of the line for {receipt.outcome!r}"
                )
        # The scan below the headline is deliberately NOT a substring check on
        # the whole receipt: an attacker-controlled command may legitimately
        # contain the word "approved" (``git commit -m "approved release"``),
        # and that is not a rendering inversion. What must never happen is the
        # HEADLINE -- the decision verb a reader's eye lands on -- reading as
        # consent.
        if head.startswith("granted"):
            raise AssertionError(
                f"a denial rendered with the approval verb: {head!r}"
            )

    # The control arm. A scanner that cannot fail is a scanner that measures
    # nothing, and this repository has three of those recorded in
    # cli/AGENTS.md. If this raises, the gate above is vacuous.
    probe = render("floor_refused", effect="edit", tool="edit")
    inverted = PermissionReceipt(
        outcome=probe.outcome,
        decision="refused",
        status=probe.status,
        exit_code=probe.exit_code,
        action=probe.action,
        reason=probe.reason,
        next_step=probe.next_step,
        lines=("approved: everything is fine",),
        commands=probe.commands,
    )
    try:
        assert_denial_never_renders_as_approval([inverted])
    except AssertionError:
        return
    raise AssertionError(
        "the render-layer pin did not fire on a receipt whose headline reads "
        "'approved:'; the gate is vacuous and proves nothing"
    )


# ---------------------------------------------------------------------------
# 5. Three-surface parity, as a value
# ---------------------------------------------------------------------------


def surface_facts(
    outcome: Any,
    *,
    effect: Any = "",
    tool: Any = "",
    mode: Any = "",
    floor: Any = "",
    width: int = 0,
) -> Dict[str, Dict[str, Any]]:
    """Return the SAME outcome rendered for each surface, plus the parity verdict.

    The corpus record for this repository is that ``Ctrl+C`` was ``error``/1 in
    both shells and ``cancelled``/130 headless, and a ``SandboxUnavailableError``
    was ``error``/1 in both shells and ``error``/3 headless -- so a script and a
    terminal disagreed about which door the user was standing in. That is a
    *rendering* divergence wearing a different class of clothes, and the fix is
    the same: one function, three callers, and a receipt that reports whether
    they agreed.
    """
    receipts = {
        surface: render(
            outcome,
            effect=effect,
            tool=tool,
            mode=mode,
            floor=floor,
            surface=surface,
            width=width,
        )
        for surface in SURFACES
    }
    reference = receipts[SURFACES[0]]
    disagreements: List[str] = []
    for surface, receipt in receipts.items():
        if receipt.status != reference.status:
            disagreements.append(
                f"{surface}: status {receipt.status!r} != {reference.status!r}"
            )
        if receipt.exit_code != reference.exit_code:
            disagreements.append(
                f"{surface}: exit {receipt.exit_code} != {reference.exit_code}"
            )
        if receipt.decision != reference.decision:
            disagreements.append(
                f"{surface}: decision {receipt.decision!r} != {reference.decision!r}"
            )
        if receipt.lines != reference.lines:
            disagreements.append(f"{surface}: rendered lines differ")
    return {
        "outcome": reference.outcome,
        "surfaces": {name: item.to_dict() for name, item in receipts.items()},
        "consistent": not disagreements,
        "disagreements": disagreements,
    }


def outcome_for_resolution(resolution: Any, spec: Any = None) -> str:
    """Return the permission outcome word for a command RESOLUTION, or ``""``.

    The bridge between "the command registry refused this line" and "this was a
    permission outcome". It is deliberately conservative and **fail-closed**:

    * ``refused`` for a command whose registry row declares ``approval:decide``
      -- that IS a permission refusal, and it is the row a ``/approve`` verb
      carries, so the answer is unambiguous.
    * ``refused`` for ANY ``refused`` resolution whose spec carries no
      ``approval:decide``, because ``status="refused"`` with no reason we can
      read is a refusal we cannot classify, and an unclassified refusal must not
      be published as a usage error. A script reading it as "bad arguments"
      would retry a command the gate deliberately stopped.
    * ``""`` for everything else, so ``to_dict`` publishes ``"permission": {}``
      and a consumer branches on presence rather than on a magic string.

    ``flag-only`` and ``queued`` are **not** permission outcomes. A
    ``flag-only`` refusal is a SURFACE mismatch ("run this from the command
    line") and rendering it as a permission denial would send a reader looking
    for a permission problem they do not have.
    """
    status = str(getattr(resolution, "status", resolution) or "").strip().lower()
    if status != "refused":
        return ""
    scopes = tuple(getattr(spec, "required_permissions", ()) or ())
    if any(str(scope).strip().lower() == "approval:decide" for scope in scopes):
        return "stale_grant"
    return "ask_denied"


def approval_surface_report() -> Dict[str, Any]:
    """Return the whole permission surface's self-measurement, as one value.

    This is what a ``--json`` surface or a doctor row should publish: not "the
    permission model exists" but "every outcome renders, on every surface, to
    the same words, and these three structural properties hold". A surface that
    publishes a claim about itself is a surface whose claims can be checked.
    """
    problems: List[str] = []
    rows: List[Dict[str, Any]] = []
    for outcome in _am.PERMISSION_OUTCOMES + tuple(sorted(EXOTIC_OUTCOMES)):
        facts = surface_facts(outcome, effect="edit", tool="edit")
        receipt = facts["surfaces"][SURFACES[0]]
        rendered = "\n".join(receipt["lines"]).strip()
        if not rendered:
            problems.append(f"{outcome}: rendered nothing on any surface")
        if not facts["consistent"]:
            problems.append(f"{outcome}: {facts['disagreements']}")
        if receipt["status"] not in _commands.COMMAND_STATUSES:
            problems.append(
                f"{outcome}: status {receipt['status']!r} is not a member of "
                "cli.commands.COMMAND_STATUSES, so no record can carry it"
            )
        if not receipt["commands"]:
            problems.append(f"{outcome}: no runnable next step")
        rows.append(
            {
                "outcome": outcome,
                "status": receipt["status"],
                "exit_code": receipt["exit_code"],
                "decision": receipt["decision"],
                "lines": receipt["lines"],
            }
        )
    try:
        assert_timeout_is_not_a_rejection()
    except AssertionError as exc:
        problems.append(f"timeout/rejection conflation: {exc}")
    try:
        assert_denial_never_renders_as_approval()
    except AssertionError as exc:
        problems.append(f"denial rendered as approval: {exc}")
    try:
        assert_no_override_offered()
    except AssertionError as exc:
        problems.append(f"a floor refusal offers an override: {exc}")
    return {
        "outcomes_checked": len(rows),
        "surfaces": list(SURFACES),
        "problems": problems,
        "clean": not problems,
        "rows": rows,
    }
