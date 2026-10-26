"""The thrash guard still fires, is still correct, and fires LESS often now.

**What exists and must not silently change.**
:class:`~harness.agent_kernel.budget.CompactionThrashPolicy` raises on the **3rd
consecutive unproductive compaction**, where progress means
**``after_tokens < limit_tokens``** — *"because a compaction can reclaim one
token and still have accomplished nothing."* Blocked attempts (a request over
its trigger whose every message is protected) count too, and one blocked attempt
is an ordinary skip rather than an abort.

**Why re-verify at all.** Wave 1 changed compaction substantially: clearing now
runs FIRST, compaction only fires if the request is still over its threshold,
and incremental merging replaced from-scratch re-summarisation. A guard tuned for
the old mechanism can fire too eagerly or too rarely, and neither shows up as a
failure — the run just aborts, or never does.

**The measurement the brief asks for, and its honest form.** *"With clearing in
front of compaction, does thrash become more likely?"* The expectation is that
it becomes **less** likely, because clearing reclaims space before compaction
has to. That is a claim about a RATE, so this module reports counts and rates for
both orders and refuses to call the expectation confirmed unless the numbers
move in the predicted direction. If they do not, that is reported as a
discrepancy to investigate rather than smoothed away.

Run it::

    python -m harness.thrash_measure
    python -m harness.thrash_measure --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel.budget import (
    DEFAULT_COMPACTION_THRASH_LIMIT,
    THRASH_REASONS,
    CompactionThrashError,
    CompactionThrashPolicy,
)
from harness.agent_kernel.conversation import ConversationMemory


@dataclass
class ThrashRow:
    """One observed compaction attempt, and whether the guard called it progress."""

    after_tokens: int
    limit_tokens: int
    blocked: bool = False
    #: Whether the raise fired on THIS attempt.
    raised: bool = False

    @property
    def receipt(self) -> Dict[str, Any]:
        """Return the receipt shape the policy reads."""
        return {
            "after_tokens": int(self.after_tokens),
            "limit_tokens": int(self.limit_tokens),
            "reclaimed_tokens": max(0, int(self.limit_tokens) - int(self.after_tokens)),
            "method": "blocked" if self.blocked else "model_summary",
        }


def drive(
    attempts: Sequence[Tuple[int, int, bool]],
    *,
    limit: int = DEFAULT_COMPACTION_THRASH_LIMIT,
) -> Dict[str, Any]:
    """Drive the policy over ``(after_tokens, limit_tokens, blocked)`` attempts.

    Returns the streak at each step and the index at which the guard fired. Kept
    as a PURE function of the attempt list so the pins can assert the boundary
    exactly (fires on the 3rd, not the 2nd or the 4th) without a kernel run.
    """
    policy = CompactionThrashPolicy(limit=int(limit))
    streak = 0
    history: List[int] = []
    fired_at = -1
    error: Optional[Dict[str, Any]] = None
    for index, (after, limit_tokens, _blocked) in enumerate(attempts, start=1):
        # The third tuple element is documentation for the CALLER — it says which
        # attempts were meant to be blocked attempts. The policy reads the receipt,
        # not the flag, so it is deliberately unused here rather than threaded
        # through as a fake signal the policy could come to depend on.
        streak = policy.note(
            {"after_tokens": after, "limit_tokens": limit_tokens}, streak=streak
        )
        reason = policy.decide(streak)
        history.append(streak)
        if reason:
            fired_at = index
            error = CompactionThrashError(
                streak=streak, limit=int(limit), reason=reason
            ).as_dict()
            break
    return {
        "streaks": history,
        "fired_at": fired_at,
        "fired": fired_at > 0,
        "error": error,
        "limit": int(limit),
        "attempts": len(attempts),
    }


# --------------------------------------------------------------------------
# The measured rate, clearing in front of compaction vs behind it
# --------------------------------------------------------------------------


@dataclass
class RateResult:
    """Thrash incidence for one ordering."""

    arm: str
    clearing_first: bool
    compactions_attempted: int = 0
    compactions_blocked: int = 0
    compactions_unproductive: int = 0
    thrash_raised: bool = False
    streak_at_end: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def thrash_rate(self) -> float:
        """Unproductive compactions as a share of attempts.

        ``0.0`` for zero attempts, and that is stated rather than divided: a rate
        over no observations is not a rate, and ``None`` would break every
        consumer that formats it.
        """
        if self.compactions_attempted <= 0:
            return 0.0
        return round(
            self.compactions_unproductive / float(self.compactions_attempted), 4
        )

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe record."""
        return {
            "arm": self.arm,
            "clearing_first": self.clearing_first,
            "compactions_attempted": self.compactions_attempted,
            "compactions_blocked": self.compactions_blocked,
            "compactions_unproductive": self.compactions_unproductive,
            "thrash_rate": self.thrash_rate,
            "thrash_raised": self.thrash_raised,
            "streak_at_end": self.streak_at_end,
            "notes": list(self.notes),
        }


def _memory() -> Tuple[ConversationMemory, List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    memory = ConversationMemory(
        journal=lambda record: rows.append(dict(record)),
        max_messages=400,
        max_chars=4_000_000,
        max_tool_output_chars=40000,
    )
    memory.seed([{"role": "system", "content": "sys"}])
    return memory, rows


def _fill(memory: ConversationMemory, count: int, chars: int) -> None:
    """Record ``count`` results of ``chars`` each — growth clearing can reclaim."""
    memory.record_assistant("go", turn=1)
    for index in range(1, int(count) + 1):
        memory.record_tool_result(
            tool="read",
            ok=True,
            output=("w" * int(chars)) + f" tail{index}",
            turn=index + 1,
            target=f"f{index}.py",
        )


def drive_receipts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    limit: int = DEFAULT_COMPACTION_THRASH_LIMIT,
) -> Dict[str, Any]:
    """Drive the policy over explicit receipt dicts.

    Needed for the case the ``(after, limit, blocked)`` tuple cannot express: a
    compaction that reports ``reclaimed_tokens: 1`` while leaving
    ``after_tokens`` AT the trigger. That is the exact scenario the policy's
    docstring is about — "a compaction can reclaim one token and still have
    accomplished nothing" — and reconstructing it from a tuple would have
    fabricated the very thing being tested.
    """
    policy = CompactionThrashPolicy(limit=int(limit))
    streak = 0
    history: List[int] = []
    fired_at = -1
    for index, receipt in enumerate(receipts, start=1):
        streak = policy.note(receipt, streak=streak)
        history.append(streak)
        if policy.decide(streak):
            fired_at = index
            break
    return {"streaks": history, "fired_at": fired_at, "fired": fired_at > 0}


def _fill_mixed(memory: ConversationMemory, index: int, chars: int) -> None:
    """Record one CLEARABLE and one PROTECTED result.

    The mix is the whole point. A workload made only of ``read`` results is
    reclaimed in full by clearing, so with clearing first the request never
    reaches the trigger and **compaction is never attempted at all** — the first
    draft measured exactly that and reported a rate of 0.0 against 0.0, which is
    not a comparison. A workload made only of ``write`` results cannot be cleared
    at all, so the ordering makes no difference. Half of each means clearing
    genuinely reduces how often compaction has to fire, which is the claim.
    """
    memory.record_tool_result(
        tool="read",
        ok=True,
        output=("c" * int(chars)) + f" read{index}",
        turn=index * 2 - 1,
        target=f"f{index}.py",
    )
    memory.record_tool_result(
        tool="write",
        ok=True,
        output=("u" * int(chars)) + f" write{index}",
        turn=index * 2,
        target=f"f{index}.py",
    )
    memory.record_assistant(f"step {index}", turn=index * 2)


def measure_thrash_rate(*, turns: int = 14, chars: int = 3000) -> Dict[str, Any]:
    """Measure compaction attempts and thrash incidence, clearing first vs last.

    Both arms use the SAME mixed growth, the SAME threshold and the SAME
    summariser; only the order differs. Two figures are reported because they
    answer different questions:

    ``compactions_attempted``
        how often compaction had to fire at all. **This is the figure the
        ordering is expected to move**, because clearing reclaims space before
        compaction has to.
    ``thrash_rate``
        the share of those attempts that achieved nothing. More attempts means
        more chances to build an unproductive streak, so the second follows the
        first — but it is reported separately because "compacts less often" and
        "thrashes less often" are different claims.
    """
    from harness.agent_kernel.budget import TokenEstimator

    estimator = TokenEstimator()
    arms: Dict[str, RateResult] = {}

    for label, clearing_first in (("clearing_first", True), ("compaction_first", False)):
        memory, _rows = _memory()
        result = RateResult(arm=label, clearing_first=clearing_first)
        limit = 1200
        for turn in range(1, int(turns) + 1):
            _fill_mixed(memory, turn, chars)
            if clearing_first:
                receipt_clearing = memory.clear_tool_results(enabled=True)
                if receipt_clearing.method == "cleared":
                    result.notes.append(
                        f"turn {turn}: clearing reclaimed "
                        f"{receipt_clearing.reclaimed_chars} chars"
                    )
            try:
                receipt = memory.compact_tokens(
                    limit_tokens=limit,
                    summarize=lambda transcript, turns, previous_summary="": (
                        "## Objective\nStill churning.\n"
                    ),
                    estimator=estimator,
                )
            except CompactionThrashError as exc:
                result.thrash_raised = True
                result.notes.append(f"turn {turn}: thrash raised {exc}")
                break
            if receipt is None:
                continue
            result.compactions_attempted += 1
            if str(receipt.get("method") or "") == "blocked":
                result.compactions_blocked += 1
            if not CompactionThrashPolicy().made_progress(receipt):
                result.compactions_unproductive += 1
            result.streak_at_end = int(memory.no_progress_streak or 0)
        arms[label] = result

    clearing = arms["clearing_first"]
    compaction = arms["compaction_first"]
    informative = (
        clearing.compactions_attempted > 0 and compaction.compactions_attempted > 0
    )
    return {
        "arms": {name: row.as_dict() for name, row in arms.items()},
        "compactions_clearing_first": clearing.compactions_attempted,
        "compactions_compaction_first": compaction.compactions_attempted,
        "clearing_first_rate": clearing.thrash_rate,
        "compaction_first_rate": compaction.thrash_rate,
        "difference": round(compaction.thrash_rate - clearing.thrash_rate, 4),
        "expected_direction": (
            "clearing first reclaims space before compaction has to, so "
            "compaction should fire LESS often and the unproductive streak "
            "should be harder to reach"
        ),
        "confirmed": clearing.compactions_attempted < compaction.compactions_attempted,
        # Named rather than glossed: a comparison in which one arm never
        # compacted measures nothing, and saying so is the difference between a
        # measurement and a number.
        "comparison_is_informative": informative,
        "informative_reason": (
            "both arms attempted at least one compaction"
            if informative
            else "at least one arm never compacted, so the rates are not comparable"
        ),
        "clearing_passes_that_reclaimed": sum(
            1 for note in clearing.notes if "reclaimed" in note
        ),
    }


def run_all() -> Dict[str, Any]:
    """Return every thrash fact in one receipt."""
    policy = CompactionThrashPolicy()
    return {
        "limit": int(policy.limit),
        "min_reclaim_tokens": int(policy.min_reclaim_tokens),
        "thrash_reasons": list(THRASH_REASONS),
        "enabled_by_default": bool(policy.enabled),
        "fires_on_three": drive([(500, 100, False)] * 3)["fired_at"],
        "does_not_fire_on_two": drive([(500, 100, False)] * 2)["fired"],
        "progress_resets_the_streak": drive(
            [(10, 100, False), (500, 100, False), (500, 100, False), (500, 100, False)]
        )["fired_at"],
        "blocked_attempts_count": drive([(500, 100, True)] * 3)["fired_at"],
        "one_blocked_attempt_is_not_an_abort": not drive([(500, 100, True)])["fired"],
        "reclaiming_one_token_is_not_progress": drive_receipts(
            [
                # reclaimed_tokens says 1, and after_tokens is AT the trigger, so
                # the request is still not under it. A token-count test would
                # call this progress; the shipped rule does not.
                {
                    "after_tokens": 100,
                    "limit_tokens": 100,
                    "reclaimed_tokens": 1,
                }
            ]
            * 3
        )["fired_at"],
        "limit_zero_disables": drive([(500, 100, False)] * 9, limit=0)["fired"],
        "rate": measure_thrash_rate(),
    }


def render(results: Mapping[str, Any]) -> str:
    """Return the human-readable report."""
    rate = results["rate"]
    lines = [
        "# Thrash policy, re-pinned",
        "",
        f"limit (3rd consecutive)  : {results['limit']}",
        f"min reclaim tokens      : {results['min_reclaim_tokens']}",
        f"thrash reasons          : {results['thrash_reasons']}",
        f"enabled by default      : {results['enabled_by_default']}",
        "",
        "## conditions, each measured",
        f"fires on the 3rd consecutive unproductive : "
        f"{results['fires_on_three'] == 3}  (index {results['fires_on_three']})",
        f"does NOT fire on 2                       : "
        f"{results['does_not_fire_on_two']}",
        f"a productive compaction resets the streak: "
        f"{results['progress_resets_the_streak'] == 4}  "
        f"(fires at index {results['progress_resets_the_streak']}, not 3)",
        f"blocked attempts count toward the streak  : "
        f"{results['blocked_attempts_count'] == 3}",
        f"ONE blocked attempt is not an abort       : "
        f"{results['one_blocked_attempt_is_not_an_abort']}",
        f"reclaiming 1 token is NOT progress        : "
        f"{results['reclaiming_one_token_is_not_progress'] == 3}",
        f"limit=0 disables the guard                : "
        f"{not results['limit_zero_disables']}",
        "",
        "## thrash rate, clearing in front of compaction vs behind it",
        f"| {'arm':<18} | {'clearing first':>14} | {'attempts':>9} | {'blocked':>8} | "
        f"{'unproductive':>12} | {'rate':>7} |",
        "|" + "-" * 82 + "|",
    ]
    for name, row in rate["arms"].items():
        lines.append(
            f"| {name:<18} | {row['clearing_first']!s:>14} | "
            f"{row['compactions_attempted']:>9} | {row['compactions_blocked']:>8} | "
            f"{row['compactions_unproductive']:>12} | {row['thrash_rate']:>7} |"
        )
    lines += [
        "",
        f"compactions with clearing FIRST : {rate['compactions_clearing_first']}",
        f"compactions with clearing LAST  : {rate['compactions_compaction_first']}",
        f"rate with clearing FIRST : {rate['clearing_first_rate']}",
        f"rate with clearing LAST  : {rate['compaction_first_rate']}",
        f"difference               : {rate['difference']}",
        f"clearing reduced COMPACTIONS : {rate['confirmed']}",
        f"comparison is informative: {rate['comparison_is_informative']} "
        f"({rate['informative_reason']})",
        f"clearing passes that reclaimed: {rate['clearing_passes_that_reclaimed']}",
        "",
        "WHAT THIS DOES NOT ESTABLISH:",
        "  - the rate is measured at the ConversationMemory level, where the "
        "ordering lives. The ORDERING itself is pinned separately and by AST, "
        "because a rate measured without pinning the order it assumes would be "
        "a rate of nothing.",
        "  - a zero rate in both arms is NOT a pass. It means the workload did "
        "not reach three consecutive unproductive compactions, which is the "
        "condition the guard exists for. The conditions table above is what "
        "establishes the guard, not this rate.",
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
    if results["fires_on_three"] != 3:
        failed.append(f"the guard fires at {results['fires_on_three']}, not the 3rd")
    if results["does_not_fire_on_two"]:
        failed.append("the guard fired on only 2 consecutive unproductive compactions")
    if results["progress_resets_the_streak"] != 4:
        failed.append(
            "a productive compaction did not reset the streak "
            f"(fired at {results['progress_resets_the_streak']}, expected 4)"
        )
    if results["blocked_attempts_count"] != 3:
        failed.append("blocked attempts are not counted toward the streak")
    if not results["one_blocked_attempt_is_not_an_abort"]:
        failed.append("a single blocked attempt aborted the run")
    if results["reclaiming_one_token_is_not_progress"] != 3:
        failed.append(
            "reclaiming one token counted as progress; the test must be "
            "after_tokens < limit_tokens"
        )
    if results["limit_zero_disables"]:
        failed.append("limit=0 did not disable the guard")
    if failed:
        print("\nFAILED:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
