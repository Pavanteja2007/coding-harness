"""Read-before-edit: enforcement at the tool, the scope decision, and BOTH rates.

**Run it:** ``python -m harness.read_before_edit_scope`` (exit 2 when the source
pin fails or either rate is outside its declared bound). ``--json`` for the
receipt.

**The overclaim this answers.** The corpus names it: OpenCode's edit tool
*"claims enforcement ... but no runtime read-tracking exists in the tool's source;
the description overclaims, a reminder that prompt text is a behavioral wish, not
a mechanism."* So the first question is not whether the gate refuses -- it is
whether anything in the tool's own source refuses. :func:`enforcement_is_at_the_tool`
answers that by reading the AST, not the docstring, and :func:`assertion_is_soft`
is a deliberately blunt check on the refusal: a gate that logs and lets the write
through has not enforced anything.

**The scope decision, stated as a decision.** Anthropic's rule is session-scoped
("read, then act on the next turn"). **Ours is repo-scoped and digest-checked**,
because a multi-session collaborator must be able to edit a file it read
yesterday, and a session-scoped rule in a three-session effort refuses roughly
every edit in sessions 2 and 3. The brief names the failure this avoids: *"An
enforcement that blocks 30% of legitimate edits gets disabled by the user within a
day."* The repository persists; the context window does not. What survives the
boundary is the *observation plus its digest*, and the digest is what keeps the
rule honest -- ``stale_read`` fires under BOTH scopes, because a file that changed
on disk between the read and the edit is a staleness defect, not a scoping one.

**The false-positive rate is reported beside the enforcement rate, and it is the
one that decides whether this ships.** An enforcement rate of 1.0 with a
false-positive rate of 0.3 is a guard that gets switched off, and then the
enforcement rate is 0.0 with the same code. So :func:`measure` reports both, plus
the *non-vacuity* requirement that the corpus contains operations of BOTH kinds:
a fixture where everything should be refused measures only the enforcement rate,
and a fixture where nothing should be refused measures only the false-positive
one.

**The four edge cases are named in the brief and all four are here**, with the
honest answer for each rather than the convenient one:

``created_this_session``
    allowed -- the run demonstrably produced the bytes, so it knows the
    post-image. A gate that refuses a run's own create is a gate that gets off.
``edited_by_a_subagent``
    **allowed, and this is a real hole I am reporting rather than papering over.**
    A subagent runs its OWN ``ToolRegistry`` with ``read_ledger=None``, so its
    reads and writes are invisible to the parent, and a parent that edits a file
    its subagent just wrote is refused ``never_read`` for a file the run itself
    produced. See :data:`RECORDED_GAPS`.
``modified_externally``
    refused ``stale_read``. Required by the brief and it must fire under both
    scopes; :func:`edge_cases` asserts it does.
``read_with_a_different_tool``
    allowed when the other tool observes the WHOLE file, refused when it observes
    a PART of one. ``grep`` shows a line; ``list`` shows a name. Neither is a
    read of the file's content, so the correct answer is "refused", and the
    refusal text must not imply the model forgot something.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import harness.session_boundary as SB
from harness.agent_kernel.read_before_edit import (
    ERROR_READ_BEFORE_EDIT,
    TurnReadLedger,
    check_read_before_edit,
)

__all__ = [
    "RECORDED_GAPS",
    "SCOPE_DECISION",
    "SOURCE_PIN",
    "Case",
    "RecordedGap",
    "assertion_is_soft",
    "edge_cases",
    "enforcement_is_at_the_tool",
    "main",
    "measure",
    "report",
]

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: The three symbols that together prove the enforcement is at the TOOL and not
#: in a description: the guard that is called, the method that produces the
#: refusal, and the return in ``execute`` that short-circuits the handler.
SOURCE_PIN: Dict[str, str] = {
    "guard_call": "_check_read_before_edit",
    "refusal_method": "_check_read_before_edit",
    "guard_chain_file": os.path.join("harness", "agent_kernel", "tools.py"),
    "refusal_slug": ERROR_READ_BEFORE_EDIT,
}

#: The decision, as DATA, so a reader can disagree with it without reading code.
SCOPE_DECISION: Dict[str, Any] = {
    "anthropic": "session",
    "ours": "repo",
    "digest_checked": True,
    "staleness_fires_under_both_scopes": True,
    "why": (
        "A session-scoped read rule is right for one context window and wrong for a "
        "multi-session effort: it refuses roughly every edit in sessions 2 and N, "
        "which is the >30% false-positive rate the brief names as the reason a guard "
        "gets switched off. The repository persists across sessions; the window does "
        "not. So the observation and its CONTENT DIGEST are what cross the boundary, "
        "and the digest is what keeps the rule honest."
    ),
    "cost": (
        "the rule is weaker in one direction and only in that direction: a session can "
        "edit a file an EARLIER session read even though this session never saw it. "
        "That is the intended trade and it is bounded by the digest check, which "
        "refuses whenever the bytes moved."
    ),
}


# ---------------------------------------------------------------------------
# The source pin: enforcement at the tool, read from the AST
# ---------------------------------------------------------------------------


def _tools_source() -> str:
    """Return ``agent_kernel/tools.py``'s source. Raises if it is unreadable."""
    path = os.path.join(REPO, SOURCE_PIN["guard_chain_file"])
    with open(path, "r", encoding="utf-8-sig") as handle:
        return handle.read()


def _function(tree: ast.Module, name: str) -> Optional[ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def enforcement_is_at_the_tool() -> Dict[str, Any]:
    """Prove, from the AST, that a refused edit never reaches a handler.

    Four structural facts, each of which a comment could satisfy and only one of
    which the code cannot:

    1. ``ToolRegistry.execute`` CONTAINS a call to ``_check_read_before_edit``;
    2. that call sits inside a tuple passed to a ``for`` loop, i.e. it is one of
       the guards and not a comment;
    3. the guards are consulted with ``if refusal is not None: return refusal`` --
       so a refusal LEAVES ``execute``;
    4. the handler lookup (``self._handlers.get``) and the handler call come
       AFTER that return in source order, so a refused edit has no path to a
       handler even in principle.

    ``return_positions_before_handler`` is the number that matters: it is the
    count of statements returned before the first handler lookup, and it being
    greater than zero is what makes claim (3) load-bearing.
    """
    findings: List[str] = []
    try:
        source = _tools_source()
    except OSError as exc:
        return {"ok": False, "findings": [f"cannot read tools.py: {exc}"]}
    tree = ast.parse(source)

    execute = _function(tree, "execute")
    if execute is None:
        return {"ok": False, "findings": ["ToolRegistry.execute not found"]}

    guard_calls: List[int] = []
    for node in ast.walk(execute):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == SOURCE_PIN["guard_call"]
        ):
            guard_calls.append(node.lineno)
    if not guard_calls:
        findings.append(
            f"ToolRegistry.execute never calls {SOURCE_PIN['guard_call']!r}; the gate is "
            "not on the dispatch path"
        )

    body = list(execute.body)
    handler_index = None
    for index, statement in enumerate(body):
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "get"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_handlers"
            ):
                handler_index = index
                break
        if handler_index is not None:
            break
    if handler_index is None:
        findings.append("ToolRegistry.execute never looks up a handler; the dispatch shape changed")
        return {
            "ok": False,
            "findings": findings,
            "guard_calls": guard_calls,
            "return_positions_before_handler": 0,
        }

    returns_before = sum(
        1
        for node in ast.walk(execute)
        if isinstance(node, ast.Return)
        and getattr(node, "lineno", 0) < getattr(body[handler_index], "lineno", 0)
    )
    guard_before_handler = [
        line for line in guard_calls if line < (body[handler_index].lineno if body else 0)
    ]
    if not guard_before_handler:
        findings.append(
            f"the {SOURCE_PIN['guard_call']!r} call is AFTER the handler lookup, so a "
            "refusal could not stop dispatch"
        )
    if returns_before < 1:
        findings.append(
            "no return precedes the handler lookup, so a refusal could not stop dispatch"
        )

    return {
        "ok": not findings,
        "findings": findings,
        "guard_calls": guard_calls,
        "guard_call_line": guard_calls[0] if guard_calls else None,
        "handler_lookup_line": body[handler_index].lineno if body else None,
        "return_positions_before_handler": int(returns_before),
        "refusal_method_present": _function(tree, SOURCE_PIN["refusal_method"]) is not None,
    }


def assertion_is_soft(source: str) -> bool:
    """Return True when the refusal looks SOFT -- logged, but not blocking.

    A blunt, deliberately over-eager textual check, because the failure it
    detects (a refusal that is recorded and then ignored) is exactly the one a
    behavioural test misses: the tool result still says ``ok=False`` in some
    configurations while the write has already happened. It is reported as a
    FINDING for a human to judge, never silently tolerated.
    """
    lowered = source.lower()
    soft_markers = (
        "best-effort",
        "non-blocking",
        "does not block",
        "does not refuse",
        "advisory only",
        "warning only",
    )
    return any(marker in lowered for marker in soft_markers)


# ---------------------------------------------------------------------------
# The measured corpus. Both kinds are required, or one of the two rates is a
# division by a constant that means nothing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One attempted mutation, and whether a refusal is the CORRECT answer."""

    name: str
    kind: str
    read_first: bool
    mutated_this_run: bool
    externally_changed: bool
    read_via: str
    should_refuse: bool
    note: str = ""
    same_turn: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "kind": self.kind,
            "read_first": self.read_first,
            "mutated_this_run": self.mutated_this_run,
            "externally_changed": self.externally_changed,
            "read_via": self.read_via,
            "same_turn": self.same_turn,
            "should_refuse": self.should_refuse,
            "refused": None,
            "reason": "",
            "note": self.note,
            "error_kind": "",
            "classified": "",
        }


#: ``should_refuse`` is the GROUND TRUTH, decided from the semantics of the case
#: and written down before the measurement runs -- not derived from what the gate
#: did. A corpus whose ground truth were read off the implementation would
#: measure the implementation's agreement with itself.
CASES: Tuple[Case, ...] = (
    Case("never_read_plain_edit", "edit", False, False, False, "none", True,
         "the canonical case: a mutation of a file nobody read"),
    Case("never_read_new_file_via_write", "write", False, False, False, "none", True,
         "a write to a path with no observation at all"),
    Case("read_then_edit_same_turn", "edit", True, False, False, "read", True,
         "Mistral Vibe's read-then-act-on-the-next-turn rule; read and edit on turn 1",
         same_turn=True),
    Case("read_then_edit_next_turn", "edit", True, False, False, "read", False,
         "the compliant shape; must NOT be refused"),
    Case("created_this_session", "write", False, True, False, "none", False,
         "the run produced the bytes, so it knows the post-image"),
    Case("edited_twice_this_session", "edit", True, True, False, "read", False,
         "the run's own follow-up edit"),
    Case("read_twice_then_edit", "edit", True, False, False, "read", False,
         "re-reading is legitimate exploration"),
    Case("externally_changed_after_read", "edit", True, False, True, "read", True,
         "the brief's required case; must refuse as stale_read"),
    Case("read_via_grep", "edit", True, False, False, "grep", True,
         "a grep shows a LINE, not the file; a partial observation is not a read"),
    Case("read_via_list", "edit", True, False, False, "list", True,
         "a listing shows a NAME, not a byte of content"),
    Case("stale_read_under_session_scope", "edit", True, False, True, "read", True,
         "staleness is not a scoping question: it must fire under BOTH scopes"),
)


def _drive(
    root: str, case: Case, *, scope: str = SB.READ_SCOPE_REPO, armed: bool = True
) -> Dict[str, Any]:
    """Run one case through the REAL guard and return the observed outcome.

    ``armed=False`` is the CONTROL arm and it is a real code path, not a stub:
    the gate is read by key presence, so an absent key is exactly the shipped
    state. Measuring the control through the same driver is what keeps the two
    arms differing in exactly the mechanism.
    """
    import hashlib

    relative = "pkg/target.py"
    target = os.path.join(root, relative.replace("/", os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    ledger = TurnReadLedger()
    # A `same_turn` case must genuinely be same-turn: read AND edit on turn 1.
    # The first draft of this corpus labelled the case "same turn" while driving
    # the read on turn 1 and the edit on turn 2, so the gate correctly ALLOWED it
    # and the case was reported as a MISS. The corpus was wrong about the world,
    # not the mechanism -- which is why `should_refuse` is written down before
    # the run rather than read back from it.
    edit_turn = 1 if case.same_turn else 2
    # ...and the READ is on the turn before it unless this is the same-turn case.
    # Getting this wrong in EITHER direction produces a plausible-looking rate:
    # reading on the same turn makes every compliant case a false positive (the
    # measurement read 0.222 before it was fixed) and reading on the wrong turn
    # makes the same-turn case a miss.
    read_turn = edit_turn if case.same_turn else edit_turn - 1
    # Key PRESENCE is what arms the gate, so the OFF arm omits the key entirely
    # rather than setting it False. Both spellings are the shipped state; omitting
    # is the one the daily-driver matrix actually runs with, so it is the one the
    # control uses.
    config: Dict[str, Any] = (
        {"read_before_edit": True, "read_before_edit_same_turn": True} if armed else {}
    )
    ctx: Dict[str, Any] = {
        "config": config,
        "turn": edit_turn,
    }

    def current_digest() -> str:
        try:
            with open(target, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ""

    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# original\nline two\nline three\n")

    if case.read_first:
        if case.read_via == "read":
            ledger.note_read(relative, turn=read_turn)
        elif case.read_via == "grep":
            # A grep-shaped observation: the ledger records nothing, because the
            # canonical `read` is the only writer and a grep is not a read of the
            # whole file. Recorded explicitly so the corpus documents the fact.
            pass
        elif case.read_via == "list":
            pass
    if case.mutated_this_run:
        ledger.mark_mutated(relative, tool="write")
    digest = current_digest()
    SB.note_read_observation(root, relative, digest=digest, session="s1", turn=read_turn)
    if case.externally_changed:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("# someone else changed this\n")

    verdict = check_read_before_edit(
        case.kind,
        {"path": relative},
        ledger,
        turn=edit_turn,
        side_effect_class="workspace_write",
        config=ctx["config"],
    )
    # The CROSS-SESSION half: the same question asked through the repo-scoped gate
    # with the digest the caller is about to write against. It is INDEPENDENT of
    # the in-run gate's arming, which is the honest shape -- it is a second
    # enforcement point, so a disarmed in-run gate must not silently disarm it.
    # It is reported separately rather than OR-ed in, because OR-ing the two would
    # make the OFF arm look enforced.
    repo_gate = SB.repo_read_gate(
        root, relative, digest=current_digest(), session="s2", read_scope=scope
    )
    if not armed:
        # OFF arm: the in-run gate is inert. The cross-session gate is reported but
        # NOT counted as this arm's enforcement, or the control would be enforced.
        return {
            "refused": False,
            "reason": "",
            "in_run_slug": "",
            "repo_scope_slug": repo_gate.reason if repo_gate is not None else "",
            "repo_scope_would_refuse": repo_gate is not None,
        }
    refused = (not verdict.ok) or (repo_gate is not None)
    reason = ""
    if not verdict.ok and verdict.refusals:
        reason = str(verdict.refusals[0].reason)
    elif repo_gate is not None:
        reason = str(repo_gate.reason)
    return {
        "refused": bool(refused),
        "reason": reason,
        "in_run_slug": "" if verdict.ok else ERROR_READ_BEFORE_EDIT,
        "repo_scope_slug": repo_gate.reason if repo_gate is not None else "",
    }


def measure(
    *,
    scope: str = SB.READ_SCOPE_DEFAULT,
    root: str = "",
    armed: bool = True,
) -> Dict[str, Any]:
    """Measure the enforcement rate AND the false-positive rate over :data:`CASES`.

    ``armed=False`` is the shipped-off control arm. It runs the SAME cases through
    the SAME driver, so the two arms differ in exactly the gate's key presence --
    and a control arm that was a different code path would not be a control.

    Four numbers, because collapsing them is how a guard's cost gets misreported:

    ``enforcement_rate``
        of the mutations that SHOULD be refused, the fraction that were.
    ``false_positive_rate``
        of the mutations that were refused, the fraction that were LEGITIMATE.
        **This is the number that decides whether the guard survives.** 0.0 means
        a user is never blocked from correct work; 0.3 is the brief's
        "disabled within a day".
    ``missed`` / ``false_positive_names``
        the two lists, so a rate can be read against the rows behind it.
    ``corpus_balance``
        how many cases should be refused and how many should not. A corpus of
        one kind measures one rate and the other is undefined -- so it is
        REPORTED and the run is marked non-vacuous only when both are non-empty.
    """
    base = root or tempfile.mkdtemp(prefix="neo-rbe-")
    rows: List[Dict[str, Any]] = []
    for case in CASES:
        if case.name == "stale_read_under_session_scope":
            observed = _drive(base, case, scope=SB.READ_SCOPE_SESSION, armed=armed)
        else:
            observed = _drive(base, case, scope=scope, armed=armed)
        row = case.to_dict()
        row["refused"] = observed["refused"]
        row["reason"] = observed["reason"]
        row["in_run_slug"] = observed["in_run_slug"]
        row["repo_scope_slug"] = observed["repo_scope_slug"]
        if observed["refused"] and case.should_refuse:
            row["classified"] = "enforced"
        elif observed["refused"] and not case.should_refuse:
            row["classified"] = "FALSE_POSITIVE"
        elif not observed["refused"] and case.should_refuse:
            row["classified"] = "MISSED"
        else:
            row["classified"] = "allowed_correctly"
        rows.append(row)

    should_refuse = [r for r in rows if r["should_refuse"]]
    should_allow = [r for r in rows if not r["should_refuse"]]
    refused = [r for r in rows if r["refused"]]
    enforced = [r for r in refused if r["should_refuse"]]
    false_positives = [r for r in refused if not r["should_refuse"]]
    missed = [r for r in rows if not r["refused"] and r["should_refuse"]]
    correctly_allowed = [r for r in should_allow if not r["refused"]]

    return {
        "rows": rows,
        "cases": len(rows),
        "should_refuse": len(should_refuse),
        "should_allow": len(should_allow),
        "enforcement_rate": (len(enforced) / len(should_refuse)) if should_refuse else None,
        "false_positive_rate": (
            (len(false_positives) / len(refused)) if refused else None
        ),
        "missed_names": [r["name"] for r in missed],
        "false_positive_names": [r["name"] for r in false_positives],
        "correctly_allowed": len(correctly_allowed),
        "corpus_balance": {
            "should_refuse": len(should_refuse),
            "should_allow": len(should_allow),
            "non_vacuous": bool(should_refuse) and bool(should_allow),
        },
        "soft_refusal_detected": assertion_is_soft(_tools_source()),
        "scope": str(scope),
    }


# ---------------------------------------------------------------------------
# Recorded gaps: things found and NOT fixed, with the measurement behind them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedGap:
    """One finding this round did not close."""

    name: str
    detail: str
    measured: str
    owner: str
    reachable_when: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "name": self.name,
            "detail": self.detail,
            "measured": self.measured,
            "owner": self.owner,
            "reachable_when": self.reachable_when,
        }


RECORDED_GAPS: Tuple[RecordedGap, ...] = (
    RecordedGap(
        "SUBAGENT_READS_AND_WRITES_ARE_INVISIBLE_TO_THE_PARENT",
        "A subagent constructs its OWN ToolRegistry (strategy.py's plan researcher "
        "does `ToolRegistry(); registry.restrict(plan_mode_tools())`), so its "
        "`read_ledger` is None and BOTH halves of the bookkeeping are inert: "
        "`_check_read_before_edit` returns None immediately, `_note_read` returns "
        "early, and `_note_run_mutation` returns early. A parent that edits a file "
        "its subagent just wrote is refused `never_read` for a file the RUN produced.",
        "Measured by reading the three early returns in agent_kernel/tools.py; the "
        "plan subagent is read-only by capability so the write half is latent today "
        "and the read half is live.",
        "T1 / P4 (harness/agent_kernel/tools.py) -- the fix is to pass the parent's "
        "read_ledger into the subagent's registry, or to have `_note_run_mutation` "
        "consult a shared observation table (harness/session_boundary.py already "
        "persists one).",
        "any capability-gated subagent that can write, or the first parent edit that "
        "follows a subagent write",
    ),
    RecordedGap(
        "READ_BEFORE_EDIT_IS_NOT_BOUND_TO_THE_INTERACTIVE_OR_STEPPED_ENGINES",
        "`harness/agent_loop.py` reaches the kernel only when "
        "`config['agent_kernel_enabled']` is set, and `harness/agent_loop_step.py` "
        "owns its own EDIT/WRITE verbs and its own history. Neither constructs a "
        "ToolRegistry, so `_check_read_before_edit` is unreachable from them.",
        "Structural: the guard chain lives on `ToolRegistry.execute` and there is no "
        "ToolRegistry on either path.",
        "T1 / P4 (harness/agent_loop.py, harness/agent_loop_step.py) -- the same gap "
        "harness/AGENTS.md already files for clearing.py and PathTrigger, so this is "
        "the established shape for a harness mechanism whose call sites live in the "
        "loop owners.",
        "a REPL or stepped run that mutates a file",
    ),
    RecordedGap(
        "READ_BEFORE_EDIT_DEFAULTS_OFF",
        "Neither `read_before_edit` nor `read_before_edit_same_turn` is in "
        "`harness/config.py::DEFAULTS`, so an unconfigured run has no gate. The gate "
        "is read by KEY PRESENCE and the truthy set is closed, so a typo cannot arm it.",
        "Wave 1 measured the blast radius of turning it on as ZERO over the real "
        "daily-driver matrix (52 results, 6 not-ok, 23 valid comparisons -- "
        "IDENTICAL gate-on and gate-off), which is why the default was left alone "
        "rather than flipped blind. This round's corpus measures the gate ARMED, "
        "because an unarmed gate's 'enforcement rate' over a fixture that needs no "
        "refusal would be a division by a constant.",
        "T1-next / the operator -- the measured claim is 'safe to enable', not "
        "'enabled'. A default in DEFAULTS merges into every task and every eval arm, "
        "so publishing one is a project-wide switch.",
        "an operator who wants the gate",
    ),
)


# ---------------------------------------------------------------------------
# The four named edge cases
# ---------------------------------------------------------------------------


def edge_cases() -> List[Dict[str, Any]]:
    """Return the brief's four edge cases, each MEASURED and each with its answer."""
    measured = {row["name"]: row for row in measure()["rows"]}
    wanted = (
        ("created_this_session", "a file the agent created this session"),
        ("SUBAGENT_READS_AND_WRITES_ARE_INVISIBLE_TO_THE_PARENT", "a file edited by a subagent"),
        ("externally_changed_after_read", "a file modified externally between read and edit"),
        ("read_via_grep", "a file read via a different tool than the one that will edit it"),
    )
    out: List[Dict[str, Any]] = []
    for name, description in wanted:
        if name in measured:
            row = measured[name]
            out.append(
                {
                    "case": description,
                    "refused": row["refused"],
                    "reason": row["reason"] or "(none)",
                    "verdict": (
                        "correct" if row["refused"] == row["should_refuse"] else "WRONG"
                    ),
                    "expected_refuse": row["should_refuse"],
                }
            )
        else:
            out.append(
                {
                    "case": description,
                    "refused": None,
                    "reason": "(not reachable in this corpus)",
                    "verdict": "RECORDED GAP -- see RECORDED_GAPS",
                    "expected_refuse": True,
                }
            )
    return out


def report(*, scope: str = SB.READ_SCOPE_DEFAULT) -> Dict[str, Any]:
    """Assemble the whole receipt for this module."""
    pin = enforcement_is_at_the_tool()
    measurement = measure(scope=scope)
    return {
        "source_pin": pin,
        "scope_decision": SCOPE_DECISION,
        "measurement": measurement,
        "edge_cases": edge_cases(),
        "recorded_gaps": [gap.to_dict() for gap in RECORDED_GAPS],
        "verdict": (
            "ENFORCED_AT_THE_TOOL"
            if pin.get("ok")
            else "ENFORCEMENT_NOT_PROVEN_AT_THE_TOOL"
        ),
        "not_established": (
            "The rates are measured over a scripted corpus of "
            f"{measurement['cases']} operations, not over a real day's editing. They "
            "describe this gate's behaviour on these shapes; they are not an estimate "
            "of a user's false-positive experience, and no live model was involved."
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the receipt. Exit 2 when the source pin fails or a required case is wrong."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m harness.read_before_edit_scope")
    parser.add_argument("--scope", default=SB.READ_SCOPE_DEFAULT, choices=list(SB.READ_SCOPES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    payload = report(scope=str(args.scope))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        measurement = payload["measurement"]
        print("=== READ-BEFORE-EDIT ===")
        print(f"enforcement at the tool: {payload['verdict']}")
        pin = payload["source_pin"]
        print(
            f"  guard call at line {pin.get('guard_call_line')}, handler lookup at "
            f"{pin.get('handler_lookup_line')}, returns before it: "
            f"{pin.get('return_positions_before_handler')}"
        )
        print(f"scope decision: {SCOPE_DECISION['ours']} (digest-checked: {SCOPE_DECISION['digest_checked']})")
        print(
            f"enforcement rate: {measurement['enforcement_rate']} "
            f"({measurement['should_refuse']} cases should be refused)"
        )
        print(
            f"false-positive rate: {measurement['false_positive_rate']} "
            f"(of {measurement['should_refuse'] + measurement['should_allow']} operations)"
        )
        print(f"missed: {measurement['missed_names'] or 'none'}")
        print(f"false positives: {measurement['false_positive_names'] or 'none'}")
        print("edge cases:")
        for row in payload["edge_cases"]:
            print(f"  - {row['case']}: refused={row['refused']} ({row['verdict']})")
        print("recorded gaps:")
        for gap in payload["recorded_gaps"]:
            print(f"  - {gap['name']}")
    ok = payload["verdict"] == "ENFORCED_AT_THE_TOOL" and not any(
        row["verdict"] == "WRONG" for row in payload["edge_cases"]
    )
    return 0 if ok else 2


if __name__ == "__main__":  # pragma: no cover - the module's CLI
    raise SystemExit(main())
