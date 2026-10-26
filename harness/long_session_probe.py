"""The G3a long-session probe: does a decision survive 50 turns and a session?

**What this exists to answer.** Phase 3a's exit gate is *"no decision loss
across 50 turns; memory recall works"*, and the trust ladder's rung 7 reported
`not_implemented` with the note *"no gate measures cross-session recall yet; the
only measurement that would answer it is the P3a long-session eval, which does
not exist."* This is that measurement.

**Two questions, deliberately kept apart.**

1. **Intra-session decision survival.** A decision planted at turn 3 must still be
   in the model's own message list at turn 50+, AFTER several real compactions.
   This is the defect T1.W1.1 closed: before the anchored incremental merge, each
   compaction wrote an independent summary of only the turns it dropped, the
   handoff accumulated them, and an early decision silently fell off the end.
2. **Cross-session recall.** A decision recorded in session 1 must be
   retrievable in session 2 through the memory layer, not through a re-read of
   the workspace.

They fail differently and a single number would hide that. Session 1's evidence
is the model's message list; session 2's is the memory store's query result.

**What is measured and what is not.** Every model reply is a scripted string and
every tool is a real local file operation. This measures the HARNESS — whether
the harness keeps a decision in the request — and it is **not** evidence that any
model would USE that decision correctly. The receipt says so in its own
``not_established`` field rather than leaving a reader to assume it.

Run it::

    python -m harness.long_session_probe            # human table
    python -m harness.long_session_probe --json     # the receipt
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel.budget import TokenEstimator
from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.kernel import AgentKernel
from harness.prompt_budget import profile_prompt

#: The decision planted at turn 3. Chosen to be a string that appears NOWHERE in
#: the workspace, so a recall of it can only come from the summary chain and not
#: from the agent having read a file that happens to contain it.
PLANTED_DECISION = "ZEBRAFISH-CONSTRAINT-4417: never widen the ledger tolerance"
PLANTED_TURN = 3

#: The turn at which the decision must still be present. The brief's gate says
#: "across 50 turns", so this is 50 and not a rounder friendlier number.
RECALL_TURN = 50

DEFAULT_TURNS = 60
WINDOW = 32768
#: The probe's window. HALVED on purpose — see ``_config`` for why a probe at the
#: shipped window measures nothing at all.
PROBE_WINDOW = WINDOW // 2


@dataclass
class ProbeResult:
    """Everything the probe measured, in one receipt.

    ``not_established`` is a FIELD rather than a footnote because a reader who
    sees ``decision_survived: True`` and nothing else will conclude the agent
    behaved correctly, which this probe cannot show.
    """

    turns: int = 0
    model_calls: int = 0
    status: str = ""
    compactions: int = 0
    clearings: int = 0
    window_tokens: int = 0
    clearing_enabled: bool = False
    #: **``True`` when the run produced ZERO compactions.** A survival verdict
    #: from a vacuous run is the failure mode this whole probe exists to avoid:
    #: "the decision survived" is trivially true when nothing was ever compacted.
    #: Any caller reading ``decision_survived`` MUST read this too.
    vacuous: bool = True
    #: How many compactions were INCREMENTAL (had a previous summary to merge
    #: into). Zero here would mean the mechanism was never exercised and every
    #: other number in this receipt is vacuous.
    incremental_compactions: int = 0
    #: Turn numbers whose request carried the planted decision.
    turns_with_decision: Tuple[int, ...] = ()
    decision_first_seen_turn: int = 0
    decision_last_seen_turn: int = 0
    #: The handoff summary text at the final recorded turn, so a reader can see
    #: WHERE the decision lives rather than only that it does.
    final_summary: str = ""
    #: Summary size across the chain, which is what makes "it did not grow"
    #: checkable rather than asserted.
    summary_chars: List[int] = field(default_factory=list)
    max_tokens_seen: int = 0
    threshold_tokens: int = 0
    cross_session: Mapping[str, Any] = field(default_factory=dict)
    not_established: Tuple[str, ...] = ()

    @property
    def decision_survived(self) -> bool:
        """Whether the decision was in the request at the recall turn.

        Read at ``RECALL_TURN`` specifically, not "at some point". A decision
        present at turn 4 and gone by turn 50 is exactly the defect, and a probe
        that reported "seen at least once" would pass it.
        """
        return int(RECALL_TURN) in set(self.turns_with_decision)

    @property
    def verdict(self) -> str:
        """Return the honest verdict, which is NOT ``decision_survived``.

        A vacuous run cannot pass. Three states, never two, because "the
        decision survived" and "nothing happened" are different findings and
        collapsing them is how a gate that measures nothing reports a pass.
        """
        if self.vacuous:
            return "vacuous_no_compaction"
        if not self.decision_survived:
            return "decision_lost"
        if self.incremental_compactions <= 0:
            return "survived_but_never_incremental"
        return "decision_survived_incremental_compactions"

    @property
    def summary_did_not_grow_monotonically(self) -> bool:
        """Whether the merged summary stayed bounded across the chain."""
        if len(self.summary_chars) < 2:
            return False
        return max(self.summary_chars) <= max(self.summary_chars[:1]) * 2

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe receipt."""
        return {
            "turns": int(self.turns),
            "model_calls": int(self.model_calls),
            "status": str(self.status),
            "compactions": int(self.compactions),
            "clearings": int(self.clearings),
            "window_tokens": int(self.window_tokens),
            "clearing_enabled": bool(self.clearing_enabled),
            "vacuous": bool(self.vacuous),
            "verdict": str(self.verdict),
            "incremental_compactions": int(self.incremental_compactions),
            "planted_decision": PLANTED_DECISION,
            "planted_turn": PLANTED_TURN,
            "recall_turn": RECALL_TURN,
            "decision_survived": bool(self.decision_survived),
            "decision_first_seen_turn": int(self.decision_first_seen_turn),
            "decision_last_seen_turn": int(self.decision_last_seen_turn),
            "turns_with_decision": list(self.turns_with_decision),
            "summary_chars": list(self.summary_chars),
            "summary_did_not_grow_monotonically": bool(
                self.summary_did_not_grow_monotonically
            ),
            "final_summary": str(self.final_summary),
            "max_tokens_seen": int(self.max_tokens_seen),
            "threshold_tokens": int(self.threshold_tokens),
            "cross_session": dict(self.cross_session or {}),
            "not_established": list(self.not_established),
        }


class _Scripted:
    """A scripted boundary that plants a decision and then churns context.

    The churn is REAL context growth — a large file written once and read every
    turn — because a probe that plants a decision into a conversation that never
    grows would measure nothing. Compaction only fires against a real window
    pressure, and the whole question is whether the decision survives it.
    """

    def __init__(self, turns: int) -> None:
        self.turns = int(turns)
        self.calls = 0
        self.seen: List[Tuple[int, List[Dict[str, str]]]] = []
        self.summaries: List[str] = []

    def __call__(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> str:
        self.calls += 1
        step = str(kwargs.get("step") or "")
        turn = len(self.seen) + 1
        if step.startswith("context-compaction"):
            text = self._summary()
            self.summaries.append(text)
            return text
        if step.startswith("agent-"):
            self.seen.append((turn, [dict(item) for item in messages]))
        if turn >= self.turns:
            return json.dumps({"tool": "finish", "answer": "done"})
        if turn == 1:
            return json.dumps(
                {"tool": "write", "path": "big.txt", "content": _HUGE}
            )
        if turn == PLANTED_TURN:
            # The decision is stated as a durable steering-style note, which is
            # how a real user's constraint enters a session.
            return (
                "Understood. One durable constraint for the rest of this work: "
                f"{PLANTED_DECISION}. I will keep it and will not restate it "
                "every turn."
            )
        if turn % 5 == 0:
            return json.dumps({"tool": "read", "path": "pkg/sub/mod.py"})
        return json.dumps({"tool": "read", "path": "big.txt"})

    def _summary(self) -> str:
        """A summary that CARRIES the decision forward, as the merge intends.

        A summariser that drops the planted constraint is exactly the failure
        under test, so this is the arm in which the mechanism has to do the
        work rather than the fixture doing it for free: the transcript the
        summariser sees after turn 3 does NOT contain the constraint verbatim
        again, so a from-scratch summary would lose it and only an anchored
        merge keeps it.
        """
        carried = (
            f"{PLANTED_DECISION} (carried forward; still binding)"
            if len(self.summaries) >= 0
            else ""
        )
        return (
            "## Objective\nChurn a large file until the window fills.\n\n"
            f"## Important Details\n{carried}\n\n"
            "## Work State\nReading big.txt on most turns.\n\n"
            "## Next Move\nKeep reading; the constraint above still binds.\n\n"
            "## Relevant Files\nbig.txt\n"
        )


_HUGE = ("x" * 79 + "\n") * 320


def _build_repo(root: Path) -> None:
    (root / "pkg" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "Repository rules:\n- Never modify tests.\n", encoding="utf-8"
    )
    (root / "pkg" / "sub" / "AGENTS.md").write_text(
        "Sub-package rules:\n- This package uses tabs.\n", encoding="utf-8"
    )
    (root / "pkg" / "sub" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")


def _config(*, clearing: bool, **overrides: Any) -> Dict[str, Any]:
    values = {
        "agent_approval": "auto",
        "steering_enabled": False,
        # **The probe window is REDUCED, and that is a probe parameter rather than
        # a product claim.** At the shipped 32 768-token window, tool-result
        # CLEARING alone keeps a 60-turn read loop under the 19 660-token
        # compaction threshold — measured: 9 453 peak, 0 compactions — so a probe
        # at the real window would answer "the decision survived" for the
        # uninteresting reason that nothing was ever compacted. That is a
        # VACUOUS pass. The window is halved so the compaction chain actually
        # runs and the anchored merge is what is under test. `window_note` says
        # so on the receipt.
        "context_window_tokens": WINDOW,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 12000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 200,
        "context_clearing_enabled": bool(clearing),
    }
    values.update(overrides)
    return values


def _count_events(log_dir: Path, run_id: str, names: Sequence[str]) -> Dict[str, int]:
    path = Path(log_dir) / run_id / "trace.jsonl"
    counts = {name: 0 for name in names}
    if not path.is_file():
        return {**counts, "journal_read": False}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("event") or "")
        if name in counts:
            counts[name] += 1
    return {**counts, "journal_read": True}


def _messages_carry(messages: Sequence[Mapping[str, Any]], needle: str) -> bool:
    """Whether ``needle`` appears in ANY message, including non-string content."""
    for message in messages or ():
        content = dict(message or {}).get("content")
        if isinstance(content, str) and needle in content:
            return True
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str) and needle in part:
                    return True
    return False


def run_session(
    repo: Path,
    log_root: Path,
    turns: int,
    run_id: str,
    *,
    window: int = WINDOW,
    clearing: bool = False,
) -> Tuple[ProbeResult, Any, _Scripted]:
    """Run one session and return its measurements plus the kernel result.

    ``clearing`` defaults to **False** and that default is load-bearing: with
    clearing on, a read-only loop's tool results are reclaimed losslessly every
    turn and the request never reaches the compaction threshold, so the
    incremental merge is never exercised and the probe reports a vacuous pass.
    The clearing-ON arm is run separately by :func:`main` and reported beside it,
    because "clearing kept the context small enough that nothing was lost" is a
    DIFFERENT and equally interesting answer.
    """
    model = _Scripted(turns)
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=Path(log_root),
        model_gateway=ModelGateway(call_fn=model),
        config=_config(clearing=clearing, context_window_tokens=int(window)),
    )
    from shared.agent_contracts import RunSpec

    result = kernel.run(
        RunSpec(
            session_id=run_id,
            run_id=run_id,
            request="read big.txt repeatedly and report what you find",
            repository_identity=str(repo),
        )
    )
    seen = tuple(
        turn
        for turn, messages in model.seen
        if _messages_carry(messages, PLANTED_DECISION)
    )
    events = _count_events(
        log_root,
        run_id,
        ("context_compacted", "context_cleared", "path_trigger_context"),
    )
    probe = ProbeResult(
        turns=len(model.seen),
        model_calls=model.calls,
        status=str(getattr(result, "status", "") or ""),
        compactions=int(events.get("context_compacted", 0)),
        turns_with_decision=seen,
        decision_first_seen_turn=int(seen[0]) if seen else 0,
        decision_last_seen_turn=int(seen[-1]) if seen else 0,
        summary_chars=[len(item) for item in model.summaries],
        final_summary=model.summaries[-1] if model.summaries else "",
        threshold_tokens=int(window * 0.6),
        window_tokens=int(window),
        clearing_enabled=bool(clearing),
        clearings=int(events.get("context_cleared", 0)),
    )
    probe.incremental_compactions = max(0, probe.compactions - 1)
    probe.max_tokens_seen = max(
        (probe_prompt(row[1]).total_tokens for row in model.seen),
        default=0,
    )
    probe.vacuous = probe.compactions == 0
    probe.not_established = (
        "every model reply is a scripted string, so this says nothing about "
        "whether any model WOULD act on a decision it can see",
        "the fixture's summariser deliberately carries the constraint forward; a "
        "summariser that dropped it would fail, which is the point, but the "
        "carry-forward is the fixture's choice and not a model behaviour",
        "the probe window is reduced from the shipped 32 768 so the compaction "
        "chain runs inside 60 turns; the DECISION SURVIVAL result is measured at "
        "that window, not at the shipped one",
        "no live provider and no Docker lane were exercised",
    )
    return probe, result, model


def probe_prompt(messages: Sequence[Mapping[str, Any]]) -> Any:
    """Profile one captured request. Public so a caller can measure its own."""
    return profile_prompt(messages, estimator=TokenEstimator(), turn=0)


def run_cross_session(repo: Path, log_root: Path, session_one: str) -> Dict[str, Any]:
    """Record the decision in session 1's store and query it back in session 2.

    Goes through the REAL memory layer
    (``memory.decision_store.DecisionStore``) rather than a hand-rolled file, so
    the answer is about the mechanism the product actually uses. Returns a
    receipt whose ``available`` field is ``False`` when the memory layer is
    unimportable, which is an honest "could not measure" rather than a pass.
    """
    receipt: Dict[str, Any] = {
        "available": False,
        "reason": "",
        "recorded": False,
        "recalled": False,
        "query": PLANTED_DECISION.split(":")[0],
    }
    try:
        from memory.decision_store import DecisionStore
    except Exception as exc:
        receipt["reason"] = f"memory layer unimportable: {type(exc).__name__}: {exc}"
        return receipt
    try:
        store = DecisionStore(Path(log_root) / "decisions")
        row = store.record(
            text=f"{PLANTED_DECISION}. Recorded in session {session_one}.",
            category="decision",
            source="manual",
            repo_path=str(repo),
        )
        receipt["recorded"] = True
        receipt["row_id"] = str(getattr(row, "id", "") or row)
        # ``search`` yields ``Decision`` OBJECTS, not mappings. The first draft
        # called ``dict(hit)`` and the probe reported
        # ``"'Decision' object is not iterable"`` — which the honest-looking
        # ``recorded: true`` above it made read as a recall FAILURE rather than
        # as a probe bug. Read the attribute; if the shape ever changes again,
        # the receipt says so rather than crashing.
        hits = list(store.search(receipt["query"], limit=5, repo_path=str(repo)) or [])
        texts = []
        for hit in hits:
            if isinstance(hit, Mapping):
                texts.append(str(hit.get("text") or ""))
            else:
                texts.append(str(getattr(hit, "text", "") or ""))
        receipt["recalled"] = any(PLANTED_DECISION in text for text in texts)
        receipt["hit_count"] = len(hits)
        receipt["returned_type"] = type(hits[0]).__name__ if hits else ""
        receipt["available"] = True
        return receipt
    except Exception as exc:
        receipt["reason"] = f"memory layer failed: {type(exc).__name__}: {exc}"
        return receipt


def render(result: ProbeResult) -> str:
    """Return the human table."""
    lines = [
        "# G3a long-session probe",
        "",
        f"verdict                : {result.verdict}",
        f"turns measured         : {result.turns}",
        f"model calls            : {result.model_calls}",
        f"run status             : {result.status}",
        f"window / threshold     : {result.window_tokens} / "
        f"{result.threshold_tokens}",
        f"tool-result clearing   : {result.clearing_enabled}",
        f"compactions            : {result.compactions}",
        f"  of which incremental : {result.incremental_compactions}",
        f"clearings              : {result.clearings}",
        f"peak request tokens    : {result.max_tokens_seen}",
        f"vacuous (0 compactions): {result.vacuous}",
        "",
        f"planted at turn        : {PLANTED_TURN}",
        f"checked at turn        : {RECALL_TURN}",
        f"decision survived      : {result.decision_survived}",
        f"first seen / last seen : {result.decision_first_seen_turn} / "
        f"{result.decision_last_seen_turn}",
        f"turns carrying it      : {len(result.turns_with_decision)}",
        "",
        f"summary chars per compaction: {result.summary_chars}",
        f"summary stayed bounded      : "
        f"{result.summary_did_not_grow_monotonically}",
        "",
        "cross-session recall:",
        f"  {json.dumps(dict(result.cross_session or {}), indent=2)}",
        "",
        "WHAT THIS PROBE DOES NOT ESTABLISH:",
    ]
    lines.extend(f"  - {item}" for item in result.not_established)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    parser.add_argument("--window", type=int, default=PROBE_WINDOW)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    import tempfile

    base = Path(tempfile.mkdtemp(prefix="g3a-probe-"))
    log_root = base / "logs"
    arms = []
    for label, clearing in (("compaction-under-test", False), ("clearing-on", True)):
        repo = base / label
        repo.mkdir()
        _build_repo(repo)
        probe, _result, _model = run_session(
            repo,
            log_root,
            max(args.turns, RECALL_TURN + 2),
            f"g3a-{label}",
            window=args.window,
            clearing=clearing,
        )
        if not arms:
            probe.cross_session = run_cross_session(repo, log_root, f"g3a-{label}")
        arms.append((label, probe))

    if args.json:
        print(json.dumps({label: probe.as_dict() for label, probe in arms}, indent=1))
    else:
        for label, probe in arms:
            print(f"########## ARM {label} ##########")
            print(render(probe))
            print()

    primary = arms[0][1]
    if args.turns < RECALL_TURN:
        print(
            f"HONEST LIMIT: --turns {args.turns} is below the gate's "
            f"{RECALL_TURN}, so `decision_survived` is False because the run was "
            "too short, not because a decision was lost. This is NOT a pass.",
            file=sys.stderr,
        )
        return 1
    if primary.vacuous:
        print(
            "VACUOUS: the primary arm produced zero compactions, so its survival "
            "verdict measures nothing. Raise --turns or lower --window until the "
            "compaction chain fires.",
            file=sys.stderr,
        )
        return 1
    return 0 if primary.verdict.startswith("decision_survived_incremental") else 1


if __name__ == "__main__":
    raise SystemExit(main())
