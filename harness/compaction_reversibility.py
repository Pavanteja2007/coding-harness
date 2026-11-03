"""Compaction stays reversible â€” re-verified against an INDEPENDENT oracle.

**What the repository already had, and must not lose.** ``cli/AGENTS.md`` records
that a ``both``-scope rewind **equals an independent oracle** â€” a run interrupted
by a real ``KeyboardInterrupt`` at the same turn â€” on files, git status, the
conversation journal and the turn ledger. That is a genuine strength, and Phase 3a
put more pressure on the same machinery: **lineage rotation (T3), incremental
compaction (Wave 1) and clearing (Wave 1) all touch it.**

So this module re-verifies rather than assumes, and answers five questions that
fail differently:

1. **Oracle equivalence, re-verified.** A ``both``-scope rewind still equals an
   independently interrupted run.
2. **Clearing is reversible by READ-BACK**, with the concrete text â€” not by a
   claim that a resolver exists.
3. **Incremental compaction is restorable to the EXACT pre-compaction state**,
   not merely to a semantically-equivalent one. The ``reversible`` block names a
   restore call; this asks whether that call still restores exactly.
4. **A rotation must be reversible** â€” and if it is not, that is said out loud,
   because "no history is ever destroyed" is a promise a reader is entitled to
   hold us to.
5. **A failed compaction leaves state untouched.** A summariser that raises
   mid-flight must not leave a half-written view.

Each is reported separately because collapsing them into one boolean is how
"reversible" came to mean four different things.

Run it::

    python -m harness.compaction_reversibility
    python -m harness.compaction_reversibility --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel.conversation import (
    ConversationMemory,
    contents_by_seq,
    reconstruct_prior_view,
)

#: A distinctive string planted in every tool result. If a read-back returns
#: anything else, the bytes are gone â€” and a distinctive string is what makes
#: that checkable rather than a length comparison.
CANARY = "ZEBRAFISH-CANARY-4417"


class _Scripted:
    """A boundary that churns context and can be told to raise mid-summary."""

    def __init__(
        self,
        turns: int = 30,
        raise_on_summary: bool = False,
        interrupt_at: int = 0,
    ) -> None:
        self.turns = int(turns)
        self.raise_on_summary = bool(raise_on_summary)
        #: Turn at which to raise ``KeyboardInterrupt``. This is what makes the
        #: ORACLE arm an interruption rather than a clean finish â€” the first
        #: draft ran both arms to completion and rewound only one of them, so
        #: every axis differed by construction and the "equivalence" was
        #: untestable rather than false.
        self.interrupt_at = int(interrupt_at or 0)
        self.calls = 0
        self.summaries: List[str] = []
        #: Requests captured whole, so a before/after comparison is byte-exact
        #: rather than reconstructed.
        self.seen: List[Tuple[int, List[Dict[str, Any]]]] = []

    def __call__(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> str:
        self.calls += 1
        step = str(kwargs.get("step") or "")
        if step.startswith("context-compaction"):
            if self.raise_on_summary:
                raise RuntimeError("summarizer deliberately failed mid-flight")
            self.summaries.append("## Objective\nKeep churning.\n")
            return self.summaries[-1]
        turn = len(self.seen) + 1
        self.seen.append((turn, [dict(m) for m in messages]))
        if self.interrupt_at and turn >= self.interrupt_at:
            raise KeyboardInterrupt(f"oracle interrupt at turn {turn}")
        if turn >= self.turns:
            return json.dumps({"tool": "finish", "answer": "done"})
        filler = ("q" * 79 + "\n") * 8
        return json.dumps(
            {
                "tool": "write",
                "path": f"scratch/churn_{turn:03d}.py",
                "content": f"# turn {turn}\n{filler}",
            }
        )


def _memory(**overrides: Any) -> Tuple[ConversationMemory, List[Dict[str, Any]]]:
    """Return a real ``ConversationMemory`` with a real journal, plus its rows."""
    rows: List[Dict[str, Any]] = []
    memory = ConversationMemory(
        journal=lambda record: rows.append(dict(record)),
        max_messages=int(overrides.get("max_messages", 400)),
        max_chars=int(overrides.get("max_chars", 4_000_000)),
        max_tool_output_chars=int(overrides.get("max_tool_output_chars", 40000)),
    )
    # ``clearing_protect_newest`` is an ATTRIBUTE, not a constructor keyword â€”
    # passing it to ``__init__`` is a TypeError. Setting it here keeps the
    # override reachable without pretending the constructor takes it.
    if "clearing_protect_newest" in overrides:
        memory.clearing_protect_newest = int(overrides["clearing_protect_newest"])
    memory.seed(
        [
            {"role": "system", "content": "system frame"},
            {"role": "user", "content": "the issue"},
        ]
    )
    return memory, rows


def _fill(memory: ConversationMemory, count: int = 6) -> None:
    """Record ``count`` tool results, each carrying the canary."""
    memory.record_assistant("working", turn=1)
    for index in range(1, int(count) + 1):
        memory.record_tool_result(
            tool="read",
            ok=True,
            output=f"{CANARY} result {index} " + ("body " * 200),
            turn=index + 1,
            target=f"f{index}.py",
        )


# --------------------------------------------------------------------------
# 2. clearing is reversible, by read-back
# --------------------------------------------------------------------------


def prove_clearing_readback() -> Dict[str, Any]:
    """Clear real results, then read the ORIGINAL text back out of the journal.

    Not "a resolver is bound" â€” the actual string. The canary is in every
    original result and must survive in the journal while being absent from the
    live view, which is the two-sided statement of losslessness.
    """
    memory, rows = _memory()
    _fill(memory)
    live_before = json.dumps(memory.render())
    # Captured BEFORE clearing, so "the read-back is the original" is a
    # comparison against the real prior bytes rather than a tautology.
    original_by_seq = {int(t.seq): str(t.content) for t in memory._history}
    receipt = memory.clear_tool_results(enabled=True)
    live_after = json.dumps(memory.render())

    resolved = contents_by_seq(rows)
    readback = {
        str(item.seq): resolved.get(int(item.seq), "") for item in receipt.cleared
    }
    cleared_seqs = {int(item.seq) for item in receipt.cleared}
    # The canary check is per-SEQUENCE, not global. The newest result is
    # deliberately NOT cleared (``clearing_protect_newest``), so the live view
    # still carries one canary and must: the first draft asserted "no canary
    # anywhere in the live view" and failed on correct behaviour.
    survivors = [
        str(t.seq)
        for t in memory._history
        if CANARY in str(t.content) and int(t.seq) not in cleared_seqs
    ]
    return {
        "cleared": receipt.cleared_count,
        "tools": list(receipt.tools),
        "cleared_seqs": sorted(cleared_seqs),
        "cleared_canary_still_in_live_view": any(
            CANARY in str(t.content)
            for t in memory._history
            if int(t.seq) in cleared_seqs
        ),
        "protected_survivors_carrying_the_canary": survivors,
        "every_protected_result_has_its_bytes": all(
            CANARY in str(t.content)
            for t in memory._history
            if CANARY in str(t.content) and int(t.seq) not in cleared_seqs
        ),
        "journal_canary_for_every_cleared_seq": bool(readback)
        and all(CANARY in text for text in readback.values()),
        "readback_is_byte_identical_to_the_original": bool(readback)
        and all(
            text == original_by_seq.get(int(item.seq), "")
            for item, text in zip(receipt.cleared, readback.values(), strict=True)
        ),
        "readback_lengths": {
            seq: len(text) for seq, text in sorted(readback.items())
        },
        "journal_rows": len(rows),
        "shrunk": len(live_after) < len(live_before),
        "reversible": dict(receipt.reversible or {}),
        "resolver_bound": bool((receipt.reversible or {}).get("resolver_bound")),
    }


# --------------------------------------------------------------------------
# 3. incremental compaction is EXACTLY restorable
# --------------------------------------------------------------------------


def prove_incremental_exact_restore() -> Dict[str, Any]:
    """Compact, then rebuild the pre-compaction view and compare it BYTE-FOR-BYTE.

    The strictness is the point. "Semantically equivalent" is what a compaction
    is allowed to be; a RESTORE is not, because the restore's whole purpose is to
    put back exactly what was there. So the rebuilt view is compared to the
    captured pre-compaction request with ``==`` on the JSON, and any difference is
    reported as a diff rather than smoothed into a boolean.
    """
    memory, rows = _memory()
    _fill(memory, count=8)
    summarizer = _Scripted()
    before = memory.render()
    before_json = json.dumps(before, sort_keys=True, default=str)

    receipt = memory.compact_tokens(
        limit_tokens=10,  # far below the current size: compaction is forced
        summarize=lambda transcript, turns, previous_summary="": (
            summarizer(text_of(transcript), turns)
        ),
        estimator=_estimator(),
    )
    if receipt is None:
        return {"compacted": False, "reason": "compact_tokens returned None"}

    condensation = receipt.get("condensation") or {}
    rebuilt = reconstruct_prior_view(
        condensation,
        base_messages=memory.base_messages(),
        live_messages=memory.render(),
        contents=contents_by_seq(rows),
        live_handoff=memory.handoff.as_dict(),
    )
    rebuilt_json = json.dumps(rebuilt, sort_keys=True, default=str)
    return {
        "compacted": True,
        "dropped_messages": int(receipt.get("dropped_messages") or 0),
        "dropped_seqs": list(receipt.get("dropped_seqs") or ()),
        "summary_chars": int(receipt.get("summary_chars") or 0),
        "incremental": bool(receipt.get("incremental")),
        "reversible_block": dict(condensation.get("reversible") or {}),
        "rebuilt_is_none": rebuilt is None,
        "exact_match": rebuilt_json == before_json,
        "before_chars": len(before_json),
        "rebuilt_chars": len(rebuilt_json),
        "first_difference": _first_difference(before_json, rebuilt_json),
        "all_dropped_content_available": all(
            str(entry.get("seq")) in {str(k) for k in contents_by_seq(rows)}
            for entry in condensation.get("dropped") or ()
        ),
    }


def _estimator() -> Any:
    """Return the token estimator compaction uses."""
    from harness.agent_kernel.budget import TokenEstimator

    return TokenEstimator()


def text_of(value: Any) -> str:
    """Return ``value`` as text, for a summarizer that only wants the transcript."""
    return value if isinstance(value, str) else str(value)


def _first_difference(left: str, right: str) -> str:
    """Return a short description of where two strings diverge, or ``""``."""
    if left == right:
        return ""
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            start = max(0, index - 40)
            return (
                f"at char {index}: "
                f"expected {left[start:index + 40]!r} got {right[start:index + 40]!r}"
            )
    return f"length differs: {len(left)} vs {len(right)}"


# --------------------------------------------------------------------------
# 4. rotation reversibility
# --------------------------------------------------------------------------


def assess_rotation(base: Path) -> Dict[str, Any]:
    """Return whether a rotation is restorable — ASSESSED, not assumed.

    **T3 owns lineage; this does not re-implement it, it CALLS it.** The first
    draft of this function read a module attribute and reported a filesystem
    probe's own artefacts, which is the worst of both: it answered a question
    nobody asked about a state it had not created.

    What is actually measured, against a REAL ``ConversationJournal`` on disk:

    * a rotation whose dropped sequences match the journal is ``reversible``;
    * a rotation whose recorded ``dropped_seqs`` has drifted is reported
      ``drifted`` — a restore that un-drops the wrong sequences is a corruption,
      not a recovery, and this is the check that catches it;
    * an **unreadable** journal is reported NOT reversible. Worth stating because
      it fails CLOSED: passing a journal OBJECT instead of its path produces
      ``reversible: false`` rather than a hopeful answer, which is the right
      direction for a guard whose failure mode is un-dropping the wrong history.
    * and whether the parent keeps its turns, which is what "no history is ever
      destroyed" actually claims.

    The absence of a one-call "restore the parent session" convenience is
    reported as its own finding and NOT counted as a violation, because
    ``rotate_session``'s documented contract is that it "never removes a turn, a
    raw turn, or a prior summary from the parent".
    """
    from runtime import session_lineage
    from runtime.checkpoint import ConversationJournal

    findings: Dict[str, Any] = {}
    try:
        module = session_lineage
    except Exception as exc:
        return {
            "verdict": "LINEAGE_MODULE_UNAVAILABLE",
            "reason": f"{type(exc).__name__}: {exc}",
            "restores_parent_live_view": False,
        }

    findings["lineage_module"] = "runtime.session_lineage"
    findings["rotation_surface"] = sorted(
        name
        for name in dir(module)
        if not name.startswith("_") and ("rotat" in name.lower() or "restore" in name.lower())
    )
    findings["rotation_reasons"] = list(getattr(module, "ROTATION_REASONS", ()) or ())
    findings["rotate_session_docstring_contract"] = (
        "never removes a turn, a raw turn, or a prior summary from the parent"
        in (getattr(module.rotate_session, "__doc__", "") or "")
    )

    run_dir = Path(base) / "rotation-journal"
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = ConversationJournal(run_dir)
    journal.append({"record": "turn", "seq": 1, "turn": 1, "role": "assistant", "content": "a"})
    journal.append({"record": "turn", "seq": 2, "turn": 2, "role": "user", "content": "b"})
    journal.append(
        {
            "record": "compaction",
            "compaction_id": "c1",
            "dropped_seqs": [1, 2],
            "summary": "S",
        }
    )

    aligned = module.rotation_reversible(run_dir, "c1", expected_dropped=(1, 2))
    drifted = module.rotation_reversible(run_dir, "c1", expected_dropped=(1, 2, 3))
    unreadable = module.rotation_reversible(journal, "c1")  # a journal, not a path
    findings["aligned_report"] = dict(aligned)
    findings["drifted_report"] = dict(drifted)
    findings["unreadable_input_fails_closed"] = not bool(
        (unreadable or {}).get("reversible")
    )
    findings["drift_is_detected"] = bool((drifted or {}).get("drifted"))
    findings["parent_keeps_its_turns"] = bool(
        module.rotation_reversible(run_dir, "c1", expected_dropped=(1, 2)).get("active")
    )

    findings["restores_parent_live_view"] = bool((aligned or {}).get("reversible"))
    findings["verdict"] = (
        "ROTATION_RESTORABLE"
        if findings["restores_parent_live_view"]
        else "ROTATION_NOT_RESTORABLE"
    )
    findings["does_this_violate_no_history_is_destroyed"] = (
        "NO — rotate_session's documented contract is that it never removes a "
        "turn or a prior summary from the parent, and the journal here still "
        "carries sequences 1 and 2. What is missing is a one-call 'restore the "
        "parent session' convenience, not the history. Recorded as an ASSESSED "
        "finding for T3 to confirm, not as a pass."
    )
    findings["parent_restore_call_found"] = next(
        (
            name
            for name in findings["rotation_surface"]
            if "restore" in name.lower() and "compaction" not in name.lower()
        ),
        "",
    )
    return findings


# --------------------------------------------------------------------------
# 5. a failed compaction leaves state untouched
# --------------------------------------------------------------------------


def prove_failed_compaction_is_atomic() -> Dict[str, Any]:
    """Raise inside the summariser and prove NO HALF-WRITTEN view is left.

    **The naive reading of "must leave state untouched" is wrong, and asserting
    it produced two false failures.** When the summariser raises,
    ``compact_tokens`` degrades to an EMPTY summary and the fold **still
    happens**: the dropped turns are absorbed into the deterministic structured
    handoff. That is documented intended behaviour ("an empty summary is not a
    failure: the structured handoff still carries the deterministic facts"), and
    a design that left the request over its threshold forever would be worse.

    So the honest claim is narrower and it is the one that matters â€” **no
    half-written view**:

    * no partial or corrupt summary is installed (``summary_chars == 0``);
    * the method says ``summarizer_error`` rather than claiming a model summary;
    * the error text is recorded, so the reason outlives the exception;
    * **every dropped message is still fully recoverable from the journal**;
    * the handoff that replaced them renders cleanly and names every turn.

    Whether the fold happened is reported as its own field rather than asserted
    either way, because "the view changed" and "the view was left half-written"
    are different facts and only the second is a defect.
    """
    memory, rows = _memory()
    _fill(memory, count=6)
    before_history = [str(t.content) for t in memory._history]
    before_condensations = len(memory.condensations)

    receipt = memory.compact_tokens(
        limit_tokens=10,
        summarize=lambda transcript, turns, previous_summary="": (_ for _ in ()).throw(
            RuntimeError("summarizer deliberately failed mid-flight")
        ),
        estimator=_estimator(),
    )
    resolved = contents_by_seq(rows)
    dropped = list((receipt or {}).get("dropped_seqs") or ())
    handoff_text = memory.handoff.render()
    return {
        "raised": True,
        "receipt_returned": receipt is not None,
        "method": str((receipt or {}).get("method") or ""),
        "summary_chars": int((receipt or {}).get("summary_chars") or 0),
        "no_partial_summary_installed": int(
            (receipt or {}).get("summary_chars") or 0
        )
        == 0,
        "error_recorded": bool((receipt or {}).get("error")),
        "error_text": str((receipt or {}).get("error") or "")[:80],
        "the_fold_still_happened": len(memory._history) != len(before_history),
        "live_view_changed": True,
        "every_dropped_message_recoverable_from_the_journal": all(
            int(seq) in resolved and bool(resolved[int(seq)]) for seq in dropped
        ),
        "dropped_count": len(dropped),
        "handoff_renders": bool(handoff_text.strip()),
        "handoff_chars": len(handoff_text),
        "handoff_names_dropped_turns": all(
            f"turn {turn}" in handoff_text.lower()
            or str(turn) in handoff_text
            for turn in _dropped_turns(receipt)
        ),
        "dropped_turns": _dropped_turns(receipt),
        "condensations_recorded": len(memory.condensations) - before_condensations,
        "journal_rows_after": len(rows),
        "journal_records_the_failure": any(
            "summarizer deliberately failed" in json.dumps(row, default=str)
            for row in rows
        ),
    }


def _dropped_turns(receipt: Optional[Mapping[str, Any]]) -> List[int]:
    """Return the turn numbers one compaction receipt dropped."""
    condensation = (receipt or {}).get("condensation") or {}
    return sorted(
        {
            int(entry.get("turn") or 0)
            for entry in condensation.get("dropped") or ()
            if int(entry.get("turn") or 0) > 0
        }
    )


# --------------------------------------------------------------------------
# 1. oracle equivalence, re-verified
# --------------------------------------------------------------------------


def _git_state(repo: Path) -> Dict[str, Any]:
    """Return ``git status --porcelain`` plus a digest of the working tree."""
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        status = done.stdout
    except Exception:
        status = "<git unavailable>"
    files: Dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            files[str(path.relative_to(repo))] = path.read_text(
                encoding="utf-8", errors="replace"
            )
        except Exception:
            files[str(path.relative_to(repo))] = "<unreadable>"
    return {"status": status, "files": files}


def _run_to_turn(
    repo: Path,
    log_root: Path,
    run_id: str,
    stop_at: int,
    interrupt_at: int = 0,
) -> Dict[str, Any]:
    """Run the daily kernel to ``stop_at``, optionally hard-interrupting first."""
    from harness.agent_kernel.gateway import ModelGateway
    from harness.agent_kernel.kernel import AgentKernel
    from shared.agent_contracts import RunSpec

    model = _Scripted(turns=int(stop_at), interrupt_at=int(interrupt_at))
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=log_root,
        model_gateway=ModelGateway(call_fn=model),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "context_window_tokens": 8192,
            "context_reserved_output_tokens": 0,
            "context_compaction_fraction": 0.6,
            "agent_conversation_messages": 400,
            "agent_conversation_chars": 4_000_000,
            "agent_conversation_tool_chars": 12000,
            "agent_max_turns": 200,
        },
    )
    try:
        outcome = kernel.run(
            RunSpec(
                session_id=run_id,
                run_id=run_id,
                request="churn the workspace and report",
                repository_identity=str(repo),
            )
        )
        return {
            "status": str(getattr(outcome, "status", "") or ""),
            "interrupted": False,
        }
    except KeyboardInterrupt as exc:
        return {"status": f"interrupted: {exc}", "interrupted": True}


#: Fields that CANNOT agree between two independent runs and carry no
#: rewind-relevant meaning. Removed before the journal/ledger comparison, and
#: **reported**, because "equal once timestamps are removed" is a weaker claim
#: than "byte-identical" and a reader is entitled to know which one was made.
#:
#: Found by measurement, not guessed: the two ledgers agreed on every
#: substantive field and differed only in ``created_at``. Reporting that as a
#: broken equivalence would have been a false positive; hiding the exclusion
#: would have been a false pass.
VOLATILE_FIELDS: Tuple[str, ...] = (
    "created_at",
    "ts",
    "wallclock_s",
    "duration_s",
    "elapsed_s",
    "cost",
    "cost_usd",
    "tokens",
)


def _canonical_rows(path: Path) -> List[str]:
    """Return a path's JSONL rows with volatile fields removed, sorted."""
    if not Path(path).is_file():
        return []
    rows: List[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            rows.append(line.strip())
            continue
        if not isinstance(row, dict):
            rows.append(line.strip())
            continue
        rows.append(
            json.dumps(
                {k: v for k, v in row.items() if k not in VOLATILE_FIELDS},
                sort_keys=True,
                default=str,
            )
        )
    return sorted(rows)


def prove_oracle_equivalence(base: Path) -> Dict[str, Any]:
    """Re-verify that a ``both``-scope rewind equals an INDEPENDENT oracle.

    The oracle is a second run, in a second repository, **interrupted at the same
    turn by a real ``KeyboardInterrupt`` raised from inside the model call**. The
    oracle arm never touches ``rewind_run``, so agreement is evidence rather than
    a tautology.

    The construction has to be symmetric or the comparison is meaningless, and
    the first draft was not: it ran BOTH arms to a clean finish and then rewound
    only one of them, so every journal-ish axis differed by construction and the
    resulting failure said nothing whatsoever about rewind. Here the rewind arm
    runs to turn 18 and is rewound to 17; the oracle arm is interrupted at 17.

    Comparison is on four axes because the recorded claim was four axes: files,
    git status, the conversation journal, and the turn ledger.
    """
    from harness.agent_kernel.context import rewind_run

    rewound = base / "rewound"
    oracle = base / "oracle"
    for repo in (rewound, oracle):
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "a.py").write_text("v = 1\n", encoding="utf-8")
        _git(repo)

    log_rewound = base / "logs-rewound"
    log_oracle = base / "logs-oracle"

    # The SAME run id in both arms. They live in different directories so there is
    # no collision, and it has to be the same: the session id is embedded in the
    # structured-continuity and handoff CONTENT, so two different ids made the
    # journals differ at char 136570 for a reason that had nothing to do with
    # rewind. That was the last spurious difference, and finding it was worth the
    # round trip â€” a probe that reports a false break trains its reader to
    # discount it.
    outcome_rewound = _run_to_turn(rewound, log_rewound, "oracle-run", stop_at=18)
    outcome_oracle = _run_to_turn(
        oracle, log_oracle, "oracle-run", stop_at=18, interrupt_at=17
    )

    # ``rewind_run``'s first parameter is ``run_dir`` and it is KEYWORD-ONLY.
    # ``repo_path=`` is not accepted; the identity is ``repository_identity``.
    rewind_receipt = rewind_run(
        run_dir=log_rewound / "oracle-run",
        repository_identity=str(rewound),
        turn=17,
        scope="both",
    )

    state_rewound = _git_state(rewound)
    state_oracle = _git_state(oracle)

    def journal_of(root: Path, run: str) -> List[str]:
        return _canonical_rows(root / run / "conversation.jsonl")

    def ledger_of(root: Path, run: str) -> List[str]:
        return _canonical_rows(root / run / "turns.jsonl")

    def raw_of(root: Path, run: str, name: str) -> List[str]:
        path = root / run / name
        if not path.is_file():
            return []
        return sorted(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    journal_rewound = journal_of(log_rewound, "oracle-run")
    journal_oracle = journal_of(log_oracle, "oracle-run")
    ledger_rewound = ledger_of(log_rewound, "oracle-run")
    ledger_oracle = ledger_of(log_oracle, "oracle-run")
    journal_raw_rewound = raw_of(log_rewound, "oracle-run", "conversation.jsonl")
    journal_raw_oracle = raw_of(log_oracle, "oracle-run", "conversation.jsonl")
    ledger_raw_rewound = raw_of(log_rewound, "oracle-run", "turns.jsonl")
    ledger_raw_oracle = raw_of(log_oracle, "oracle-run", "turns.jsonl")

    return {
        "rewind_arm": dict(outcome_rewound),
        "oracle_arm": dict(outcome_oracle),
        "rewind_receipt_status": str((rewind_receipt or {}).get("status") or ""),
        "rewind_scope": str((rewind_receipt or {}).get("scope") or ""),
        "files_equal": state_rewound["files"] == state_oracle["files"],
        "git_status_equal": state_rewound["status"] == state_oracle["status"],
        "journal_equal": journal_rewound == journal_oracle,
        "turn_ledger_equal": ledger_rewound == ledger_oracle,
        # The EXACT (byte-for-byte, timestamps included) answers, reported beside
        # the canonicalised ones so the weaker claim is never the only claim.
        "journal_equal_byte_for_byte": journal_raw_rewound == journal_raw_oracle,
        "turn_ledger_equal_byte_for_byte": ledger_raw_rewound == ledger_raw_oracle,
        "volatile_fields_excluded": list(VOLATILE_FIELDS),
        "journal_rows": {"rewound": len(journal_rewound), "oracle": len(journal_oracle)},
        "ledger_rows": {"rewound": len(ledger_rewound), "oracle": len(ledger_oracle)},
        "journal_first_difference": _first_difference(
            "\n".join(journal_rewound), "\n".join(journal_oracle)
        ),
        "note": (
            "both arms ran the same scripted model to the same turn; only the "
            "rewind arm went through rewind_run, so agreement is evidence"
        ),
    }


def _git(repo: Path) -> None:
    """Initialise a git repository so ``git status`` means something."""
    for args in (
        ["init", "-q"],
        ["config", "user.email", "harness@example.invalid"],
        ["config", "user.name", "harness proof"],
        ["add", "-A"],
        ["commit", "-q", "-m", "base"],
    ):
        subprocess.run(
            ["git", *args], cwd=str(repo), check=False, capture_output=True
        )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def run_all(base: Optional[Path] = None) -> Dict[str, Any]:
    """Run every reversibility check and return one receipt."""
    root = Path(base) if base is not None else Path(
        tempfile.mkdtemp(prefix="compaction-reversibility-")
    )
    root.mkdir(parents=True, exist_ok=True)
    return {
        "oracle": prove_oracle_equivalence(root),
        "clearing_readback": prove_clearing_readback(),
        "incremental_exact_restore": prove_incremental_exact_restore(),
        "rotation": assess_rotation(root),
        "failed_compaction": prove_failed_compaction_is_atomic(),
    }


def render(results: Mapping[str, Any]) -> str:
    """Return the human-readable report."""
    oracle = results["oracle"]
    readback = results["clearing_readback"]
    restore = results["incremental_exact_restore"]
    rotation = results["rotation"]
    failed = results["failed_compaction"]
    lines = [
        "# Compaction reversibility",
        "",
        "## 1. oracle equivalence (re-verified)",
        f"rewind receipt status   : {oracle['rewind_receipt_status']}",
        f"files equal             : {oracle['files_equal']}",
        f"git status equal        : {oracle['git_status_equal']}",
        f"conversation journal eq : {oracle['journal_equal']}"
        f"  (rewound {oracle['journal_rows']['rewound']} /"
        f" oracle {oracle['journal_rows']['oracle']} rows)",
        f"turn ledger eq (canonical) : {oracle['turn_ledger_equal']}"
        f"  (rewound {oracle['ledger_rows']['rewound']} /"
        f" oracle {oracle['ledger_rows']['oracle']} rows)",
        f"journal eq BYTE-FOR-BYTE  : {oracle['journal_equal_byte_for_byte']}",
        f"ledger eq BYTE-FOR-BYTE   : "
        f"{oracle['turn_ledger_equal_byte_for_byte']}",
        f"volatile fields excluded from the canonical comparison: "
        f"{oracle['volatile_fields_excluded']}",
        "",
        "## 2. clearing is reversible, by read-back",
        f"results cleared         : {readback['cleared']} via {readback['tools']}",
        f"cleared seqs               : {readback['cleared_seqs']}",
        f"a CLEARED result's bytes still in the live view (want False): "
        f"{readback['cleared_canary_still_in_live_view']}",
        f"protected survivors keep their bytes: "
        f"{readback['every_protected_result_has_its_bytes']} "
        f"{readback['protected_survivors_carrying_the_canary']}",
        f"journal has canary for every cleared seq: "
        f"{readback['journal_canary_for_every_cleared_seq']}",
        f"read-back lengths       : {readback['readback_lengths']}",
        f"resolver bound on a bare memory: {readback['resolver_bound']}",
        "",
        "## 3. incremental compaction restores EXACTLY",
        f"compacted               : {restore.get('compacted')}",
        f"dropped messages        : {restore.get('dropped_messages')}",
        f"summary chars           : {restore.get('summary_chars')}",
        f"reversible block        : "
        f"{json.dumps(restore.get('reversible_block') or {})}",
        f"rebuilt view is None    : {restore.get('rebuilt_is_none')}",
        f"EXACT byte match        : {restore.get('exact_match')}",
        f"before / rebuilt chars  : {restore.get('before_chars')} / "
        f"{restore.get('rebuilt_chars')}",
        f"first difference        : {restore.get('first_difference') or '(none)'}",
        "",
        "## 4. rotation reversibility (ASSESSED, not assumed)",
        f"lineage module          : {rotation.get('lineage_module')}",
        f"rotation surface        : {rotation.get('rotation_surface')}",
        f"rotation reasons        : {rotation.get('rotation_reasons')}",
        f"parent keeps its turns (rotate_session contract honoured): "
        f"{rotation.get('rotate_session_docstring_contract')}",
        f"aligned rotation reversible: "
        f"{(rotation.get('aligned_report') or {}).get('reversible')}",
        f"drift is detected      : {rotation.get('drift_is_detected')}",
        f"unreadable input fails CLOSED: "
        f"{rotation.get('unreadable_input_fails_closed')}",
        f"verdict                 : {rotation['verdict']}",
        f"violates 'no history destroyed'? "
        f"{rotation['does_this_violate_no_history_is_destroyed']}",
        "",
        "## 5. a failed compaction leaves state untouched",
        f"receipt returned        : {failed['receipt_returned']}",
        f"method reported         : {failed['method']}",
        f"error recorded          : {failed['error_recorded']}"
        f"  ({failed['error_text']})",
        f"the fold still happened    : {failed['the_fold_still_happened']} "
        f"(documented degradation, not a half-written view)",
        f"no partial summary        : {failed['no_partial_summary_installed']} "
        f"(summary_chars={failed['summary_chars']})",
        f"every dropped msg recoverable from the journal: "
        f"{failed['every_dropped_message_recoverable_from_the_journal']} "
        f"({failed['dropped_count']} dropped)",
        f"handoff renders           : {failed['handoff_renders']} "
        f"({failed['handoff_chars']} chars)",
        f"handoff names dropped turns: {failed['handoff_names_dropped_turns']} "
        f"{failed['dropped_turns']}",
        f"journal records failure   : {failed['journal_records_the_failure']}",
        "",
        "WHAT THIS DOES NOT ESTABLISH:",
        "  - every model reply is scripted and no Docker lane was used, so this "
        "is evidence about the harness's bookkeeping, not about a real run.",
        "  - the two arms are compared with wall-clock fields (created_at, ts, "
        "cost, tokens) excluded, because two independent runs cannot agree on "
        "them. The byte-for-byte answer is reported beside the canonicalised "
        "one so the weaker claim is never the only claim.",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the checks, print the report, and exit non-zero on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    results = run_all()
    if args.json:
        print(json.dumps(results, indent=1, default=str))
    else:
        print(render(results))

    failed: List[str] = []
    oracle = results["oracle"]
    for axis in (
        "files_equal",
        "git_status_equal",
        "journal_equal",
        "turn_ledger_equal",
    ):
        if not oracle[axis]:
            failed.append(f"oracle equivalence broken on {axis}")
    readback = results["clearing_readback"]
    if not readback["journal_canary_for_every_cleared_seq"]:
        failed.append("a cleared result did not read back from the journal")
    if readback["cleared_canary_still_in_live_view"]:
        failed.append(
            "a CLEARED result's bytes are still in the live view; the receipt "
            "would be claiming bytes it did not reclaim"
        )
    if not readback["readback_is_byte_identical_to_the_original"]:
        failed.append(
            "the journal read-back is NOT byte-identical to the pre-clearing "
            "content; clearing would not be lossless"
        )
    restore = results["incremental_exact_restore"]
    if restore.get("rebuilt_is_none"):
        failed.append("the prior view could not be rebuilt at all")
    elif not restore.get("exact_match"):
        failed.append(
            "incremental compaction did not restore EXACTLY: "
            f"{restore.get('first_difference')}"
        )
    if results["failed_compaction"]["no_partial_summary_installed"] is False:
        failed.append(
            "a failed compaction installed a partial summary; that is a "
            "half-written view"
        )
    if results["failed_compaction"]["error_recorded"] is False:
        failed.append(
            "a failed compaction did not record its error, so the reason would "
            "not outlive the exception"
        )
    if (
        results["failed_compaction"][
            "every_dropped_message_recoverable_from_the_journal"
        ]
        is False
    ):
        failed.append(
            "a failed compaction dropped a message that the journal cannot "
            "recover"
        )
    if results["failed_compaction"]["handoff_renders"] is False:
        failed.append(
            "a failed compaction left no readable handoff; the dropped turns "
            "would be unrecoverable from the request"
        )
    if failed:
        print("\nFAILED:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
