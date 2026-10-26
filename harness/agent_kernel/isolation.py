"""Subagent isolation, generalized from the one receipt that already proved it.

**What exists and why it is worth generalizing.** Phase 0's audit found
`PlanResearchSubagent` whose result type has **no field capable of holding a
transcript** -- only a plan, bounded citations, and counters -- while
separately reporting `transcript_messages` / `transcript_chars` /
`transcript_digest`. The isolation there is *measurable rather than asserted*,
which is the only kind worth having. The audit's complaint was that the pattern
existed for exactly one subagent and would silently not exist for the next one.

So this module is the pattern, extracted, plus the three things the single
instance could not express:

1. **The split-decision rule**, encoded. Cognition's warning is the constraint
   that matters: *"every action an agent takes carries an implicit decision,
   and when sub-agents act in parallel without seeing each other's full traces,
   those decisions conflict."* The corpus's own test is the answer to encode:
   *"the question to ask before splitting a task is whether each piece can
   succeed knowing only its own slice. When the answer is yes, give it its own
   window. When it is no, keep it in the room."* :func:`split_decision`
   applies that question and RECORDS which way it went and why, because a rule
   nobody can read the outcome of is a rule nobody follows.

2. **Context offloading at the boundary.** Anthropic's number: sub-agents
   explore with tens of thousands of tokens and return **1,000-2,000 token
   summaries**, *"which is where the 90.2% improvement comes from."* A large
   artifact therefore leaves as a :class:`ArtifactPointer` -- a path, a size and
   a digest -- never as bytes. The size is carried so the parent knows what it
   is choosing not to read.

3. **The untrusted fence.** A subagent explores the repository, and the
   repository is untrusted: it may contain prompt injection. So anything a
   subagent reports upward is routed through
   ``shared.security.review_untrusted_source`` with source
   ``repository_instructions`` -- the same boundary Phase 2 built, and the same
   one repository instruction files go through. An un-fenced subagent is a
   privilege escalation with extra steps, because its report is written by
   whatever the repository said.

**The non-negotiable.** A subagent's trace never returns to its parent.
:func:`assert_isolated` is the check, and it is designed so that the obvious
future edit -- adding a ``transcript`` field to a result -- fails it rather
than passing quietly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.long_horizon import ArtifactPointer

#: Anthropic's measured return band for a subagent summary: *"return 1,000-2,000
#: token summaries"*. Declared here as the DEFAULT band so a caller does not
#: have to know it, and published so a reviewer can see the number the budget
#: is derived from. The divisor is this repository's own estimator constant, not
#: a second literal.
SUMMARY_MIN_CHARS = 1000 * 4
SUMMARY_MAX_CHARS = 2000 * 4

#: The hard ceiling on what may cross the boundary. Above the band is a
#: refusal-worthy amount: it means the subagent pasted its work.
SUMMARY_HARD_CAP_CHARS = SUMMARY_MAX_CHARS

#: The two ways a split can go. A closed set, because "it depends" is not an
#: answer a receipt can carry.
SPLIT_WAYS: Tuple[str, ...] = ("own_window", "in_the_room")

#: The compression floor. A return at least this much smaller than the
#: transcript it came from is what "the exploration did not land in the parent"
#: means numerically. Below it, the receipt says the compression was weak
#: rather than claiming isolation it did not achieve.
MIN_COMPRESSION_RATIO = 0.25

#: The four isolation dimensions, so a receipt can name WHICH property holds
#: rather than asserting the word.
ISOLATION_DIMENSIONS: Tuple[str, ...] = (
    "own_window",  # the exploration happened in a different message list
    "compressed_return",  # what came back is smaller than what went in
    "no_trace_return",  # nothing in the return can hold the working notes
    "artifacts_offloaded",  # large content left as pointers
    "content_fenced",  # untrusted content was reviewed before it came up
)

__all__ = [
    "ISOLATION_DIMENSIONS",
    "MIN_COMPRESSION_RATIO",
    "SPLIT_WAYS",
    "SUMMARY_HARD_CAP_CHARS",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_MIN_CHARS",
    "ContentFence",
    "IsolationProblem",
    "IsolationReceipt",
    "SplitDecision",
    "assert_isolated",
    "compress_summary",
    "fence_subagent_content",
    "isolation_report",
    "plan_research_result_isolated",
    "split_decision",
]


def _chars_to_tokens(chars: int) -> int:
    """Convert characters to tokens using the one estimator the tree has.

    Delegates to ``harness.agent_kernel.budget.CHARS_PER_TOKEN`` rather than
    carrying a second divisor. A second literal here would drift, and a drift
    in this number changes the token figure in a receipt nobody could check.
    """
    try:
        from harness.agent_kernel.budget import CHARS_PER_TOKEN

        divisor = float(CHARS_PER_TOKEN) or 4.0
    except Exception:
        divisor = 4.0
    return round(float(chars or 0) / divisor)


@dataclass(frozen=True)
class ContentFence:
    """The verdict on content a subagent read and is reporting upward.

    ``reviewed`` False means the boundary was NOT reached, and the text is
    withheld. That is the Phase-2 discipline: a fence nobody can reach must
    withhold rather than pass through, and a receipt that read "clean" because
    no review ran is the exact failure the fence exists to prevent.
    """

    reviewed: bool = False
    allowed: bool = False
    blocked: bool = True
    tainted: bool = False
    severity: str = "none"
    findings: Tuple[str, ...] = ()
    text: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection -- findings only, never content."""
        return {
            "reviewed": bool(self.reviewed),
            "allowed": bool(self.allowed),
            "blocked": bool(self.blocked),
            "tainted": bool(self.tainted),
            "severity": self.severity,
            "findings": list(self.findings),
            "chars": len(self.text),
            "reason": self.reason,
        }


def fence_subagent_content(
    text: Any,
    *,
    source: str = "repository_instructions",
    mode: Optional[str] = None,
) -> ContentFence:
    """Review a subagent's reported content at the Phase-2 boundary.

    Routes through ``shared.security.review_untrusted_source`` -- the ONE
    boundary -- rather than a second scanner. It is the same call
    repository instruction files get, and deliberately so: a subagent's report
    is repository text laundered through a summariser, and laundering does not
    change the trust level.

    An unavailable boundary **withholds the text**. ``reviewed: False`` with
    ``text: ""`` is a distinct state from a clean review, and a caller that
    only checks ``allowed`` will refuse both -- the safe direction, because the
    alternative is an unreviewed report reading as a clean one.
    """
    try:
        from shared.security import review_untrusted_source
    except Exception as exc:  # pragma: no cover - the boundary is importable
        return ContentFence(
            reviewed=False,
            allowed=False,
            blocked=True,
            text="",
            reason=f"the untrusted-content boundary is unreachable: {exc}",
        )
    try:
        review = review_untrusted_source(text, source=source, mode=mode)
    except Exception as exc:
        return ContentFence(
            reviewed=False,
            allowed=False,
            blocked=True,
            text="",
            reason=f"the untrusted-content boundary refused to answer: {exc}",
        )
    return ContentFence(
        reviewed=True,
        allowed=bool(getattr(review, "allowed", False)),
        blocked=bool(getattr(review, "blocked", False)),
        tainted=bool(getattr(review, "tainted", False)),
        severity=str(getattr(review, "severity", "none") or "none"),
        findings=tuple(str(item) for item in getattr(review, "findings", ()) or ()),
        text=str(getattr(review, "text", "") or ""),
        reason="" if getattr(review, "reviewed", True) else "review unavailable",
    )


def compress_summary(
    text: Any,
    *,
    max_chars: int = SUMMARY_MAX_CHARS,
    min_chars: int = 0,
) -> Tuple[str, Dict[str, Any]]:
    """Bound a subagent's return and report exactly what was dropped.

    **A subagent that returns its whole exploration is the failure this whole
    phase is about**, so the cap is applied here, on the way out, rather than
    trusted to the subagent's discipline.

    The omission marker is MEASURED, not guessed, for the reason
    ``subagents._cap_text`` already records: a fixed-size marker overflows on
    a six-figure omission count, and a "capped" result that is over the cap is
    the failure a cap exists to prevent.
    """
    body = str(text or "")
    limit = max(0, int(max_chars))
    floor = max(0, int(min_chars))
    stats: Dict[str, Any] = {
        "input_chars": len(body),
        "limit": limit,
        "truncated": False,
        "dropped_chars": 0,
        "input_tokens": _chars_to_tokens(len(body)),
        "returned_tokens": _chars_to_tokens(len(body)),
        "min_chars": floor,
    }
    if limit <= 0:
        stats.update(
            {
                "truncated": bool(body),
                "dropped_chars": len(body),
                "returned_tokens": 0,
            }
        )
        return "", stats
    if len(body) <= limit:
        return body, stats
    head = max(limit // 2, 1)
    for _ in range(4):
        tail = max(limit - head - 1, 1)
        omitted = len(body) - head - tail
        marker = f"\n\n[... {omitted} chars omitted from the subagent's return ...]\n\n"
        room = limit - len(marker)
        if room <= 0:
            return (marker + body)[:limit], {
                **stats,
                "truncated": True,
                "dropped_chars": len(body) - limit,
                "returned_tokens": _chars_to_tokens(limit),
            }
        head = max(min(head, room - 1), 1)
        tail = max(room - head, 1)
        omitted = len(body) - head - tail
        marker = f"\n\n[... {omitted} chars omitted from the subagent's return ...]\n\n"
        if head + tail + len(marker) <= limit:
            return (
                body[:head] + marker + body[-tail:],
                {
                    **stats,
                    "truncated": True,
                    "dropped_chars": len(body) - (head + tail),
                    "returned_tokens": _chars_to_tokens(
                        len(body[:head] + marker + body[-tail:])
                    ),
                },
            )
    return body[:limit], {
        **stats,
        "truncated": True,
        "dropped_chars": len(body) - limit,
        "returned_tokens": _chars_to_tokens(limit),
    }


@dataclass(frozen=True)
class SplitDecision:
    """Which way the split went, and why. Recorded, not inferred.

    ``way`` is ``own_window`` or ``in_the_room``. ``reason`` is the sentence a
    reader needs in order to disagree with the call, and ``unresolved`` names
    what the decision could NOT see -- a slice the caller did not describe is a
    slice the rule cannot judge, and a rule that quietly treats an undescribed
    slice as separable is how two agents end up editing one file.
    """

    way: str
    reason: str = ""
    slice_count: int = 0
    self_contained: Tuple[bool, ...] = ()
    unresolved: Tuple[str, ...] = ()
    rule: str = (
        "can each piece succeed knowing only its own slice? yes -> give it its "
        "own window; no -> keep it in the room"
    )

    @property
    def split(self) -> bool:
        """Return whether the task was actually split."""
        return self.way == "own_window"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this decision."""
        return {
            "way": self.way,
            "split": bool(self.split),
            "reason": self.reason,
            "slice_count": int(self.slice_count),
            "self_contained": [bool(item) for item in self.self_contained],
            "unresolved": list(self.unresolved),
            "rule": self.rule,
        }

    def render(self) -> str:
        """Return the one line a caller reports."""
        return f"split={self.way}: {self.reason}"


def split_decision(
    slices: Sequence[Any],
    *,
    shared_context: Sequence[str] = (),
) -> SplitDecision:
    """Return whether this task may be split, per the corpus's own test.

    Each slice is any mapping carrying at least ``name`` and ``goal``. Three
    signals decide it, and each is a real reason rather than a vibe:

    ``self_contained``
        The slice DECLARES that it can finish on its own (``self_contained:
        True``). A slice that does not say anything is treated as NOT
        self-contained -- an undeclared slice is a slice the rule cannot see,
        and defaulting it to "separable" would put two agents on one file.

    ``shared_context``
        Names every slice needs. A non-empty list is a dependency every slice
        carries, which is the conflict case Cognition names: parallel agents
        each holding a partial view making decisions the other cannot see.

    ``goal``
        An empty goal is an undescribed slice. Reported in ``unresolved``
        rather than assumed fine.

    The answer is ``in_the_room`` whenever ANY slice is not self-contained, and
    the reason names which one. A plan with one inseparable slice is not a plan
    that can be 90% parallelised, because the inseparable slice is where the
    conflict lands.
    """
    items = [item for item in (slices or ()) if isinstance(item, Mapping)]
    if not items:
        return SplitDecision(
            way="in_the_room",
            reason="no slices were described, so the rule has nothing to judge",
            slice_count=0,
        )

    self_contained: List[bool] = []
    unresolved: List[str] = []
    for item in items:
        name = str(item.get("name") or "(unnamed)")
        if not str(item.get("goal") or "").strip():
            unresolved.append(f"{name} declares no goal")
        declared = item.get("self_contained")
        if declared is None:
            self_contained.append(False)
            unresolved.append(f"{name} does not declare whether it can finish alone")
        else:
            self_contained.append(bool(declared))

    shared = [str(item) for item in (shared_context or ()) if str(item).strip()]
    if shared:
        return SplitDecision(
            way="in_the_room",
            reason=(
                "every slice needs shared context ("
                + ", ".join(shared[:5])
                + "); agents acting in parallel would each hold a partial view "
                "and could not see each other's decisions"
            ),
            slice_count=len(items),
            self_contained=tuple(self_contained),
            unresolved=tuple(unresolved),
        )

    if not all(self_contained):
        blockers = [
            str(item.get("name") or "(unnamed)")
            for item, ok in zip(items, self_contained, strict=False)
            if not ok
        ]
        return SplitDecision(
            way="in_the_room",
            reason=(
                "slice(s) "
                + ", ".join(blockers)
                + " cannot succeed knowing only their own slice, so their "
                "implicit decisions would conflict"
            ),
            slice_count=len(items),
            self_contained=tuple(self_contained),
            unresolved=tuple(unresolved),
        )
    return SplitDecision(
        way="own_window",
        reason=(
            f"all {len(items)} slices declare they can finish knowing only their "
            "own slice, so each gets its own window and returns a compressed summary"
        ),
        slice_count=len(items),
        self_contained=tuple(self_contained),
        unresolved=tuple(unresolved),
    )


@dataclass(frozen=True)
class IsolationProblem:
    """One way an isolation claim fails to hold."""

    dimension: str
    detail: str
    owner: str = "T1 / harness"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this problem."""
        return {"dimension": self.dimension, "detail": self.detail, "owner": self.owner}

    def render(self) -> str:
        """Return the one quotable line."""
        return f"ISOLATION BROKEN ({self.dimension}): {self.detail}"


@dataclass(frozen=True)
class IsolationReceipt:
    """The generalized receipt. Every subagent produces one, by construction.

    The load-bearing pair is ``transcript_chars`` and ``returned_chars``. The
    first is what the subagent's OWN window held; the second is what its parent
    received. ``compression_ratio`` is the measurement of whether the isolation
    actually isolated anything, and it is computed rather than asserted.
    """

    agent: str = ""
    transcript_messages: int = 0
    transcript_chars: int = 0
    transcript_digest: str = ""
    returned_chars: int = 0
    returned_tokens: int = 0
    parent_messages: int = 0
    parent_tokens: int = 0
    artifacts: Tuple[ArtifactPointer, ...] = ()
    fence: Optional[ContentFence] = None
    split: Optional[SplitDecision] = None
    truncated: bool = False
    notes: str = ""

    @property
    def compression_ratio(self) -> Optional[float]:
        """Return ``returned/transcript``, or ``None`` when there was no transcript.

        ``None`` and ``1.0`` are different answers and this keeps them apart: a
        subagent that never read anything has no ratio to report, and a subagent
        whose return equals its transcript has ratio 1.0 -- which is the failure.
        """
        if self.transcript_chars <= 0:
            return None
        return round(self.returned_chars / self.transcript_chars, 6)

    def dimensions(self) -> Dict[str, bool]:
        """Return which of :data:`ISOLATION_DIMENSIONS` this receipt holds."""
        ratio = self.compression_ratio
        return {
            "own_window": bool(self.transcript_messages),
            "compressed_return": bool(
                ratio is not None and ratio <= MIN_COMPRESSION_RATIO
            ),
            "no_trace_return": bool(
                self.returned_chars < self.transcript_chars
                or self.transcript_chars == 0
            ),
            "artifacts_offloaded": bool(self.artifacts),
            "content_fenced": bool(self.fence is not None and self.fence.reviewed),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection of this receipt."""
        return {
            "agent": self.agent,
            "transcript_messages": int(self.transcript_messages),
            "transcript_chars": int(self.transcript_chars),
            "transcript_digest": self.transcript_digest,
            "returned_chars": int(self.returned_chars),
            "returned_tokens": int(self.returned_tokens),
            "parent_messages": int(self.parent_messages),
            "parent_tokens": int(self.parent_tokens),
            "compression_ratio": self.compression_ratio,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "fence": self.fence.to_dict() if self.fence is not None else None,
            "split": self.split.to_dict() if self.split is not None else None,
            "truncated": bool(self.truncated),
            "dimensions": self.dimensions(),
            "notes": self.notes,
        }

    def render(self) -> str:
        """Return the one line a parent shows in its receipt."""
        ratio = self.compression_ratio
        return (
            f"subagent {self.agent or '(unnamed)'}: own window "
            f"{self.transcript_messages} msgs / {self.transcript_chars} chars, "
            f"parent received {self.returned_chars} chars "
            f"({ratio:.1%} of it)"
            if ratio is not None
            else f"subagent {self.agent or '(unnamed)'}: no transcript recorded"
        )


def isolation_receipt(
    agent: str,
    messages: Sequence[Mapping[str, Any]],
    returned: Any,
    *,
    artifacts: Sequence[ArtifactPointer] = (),
    fence: Optional[ContentFence] = None,
    split: Optional[SplitDecision] = None,
    parent_messages: int = 0,
    parent_tokens: int = 0,
    truncated: bool = False,
    notes: str = "",
) -> IsolationReceipt:
    """Build a receipt from a subagent's own message list and its return.

    The whole measurement in one call: the transcript is the subagent's OWN
    list (which the parent never receives), the return is whatever crossed the
    boundary, and the ratio is computed here rather than by each subagent
    reporting its own numbers and hoping they agree.
    """
    body = returned if isinstance(returned, str) else str(returned or "")
    transcript_chars = sum(
        len(str(message.get("content") or "")) for message in messages or ()
    )
    digest = hashlib.sha256(
        "\n".join(
            str(message.get("content") or "") for message in messages or ()
        ).encode("utf-8", "replace")
    ).hexdigest()
    return IsolationReceipt(
        agent=str(agent or ""),
        transcript_messages=len(messages or ()),
        transcript_chars=int(transcript_chars),
        transcript_digest=digest,
        returned_chars=len(body),
        returned_tokens=_chars_to_tokens(len(body)),
        parent_messages=int(parent_messages),
        parent_tokens=int(parent_tokens),
        artifacts=tuple(artifacts),
        fence=fence,
        split=split,
        truncated=bool(truncated),
        notes=str(notes or ""),
    )


def assert_isolated(
    receipt: IsolationReceipt,
    *,
    require_fence: bool = False,
) -> Tuple[bool, Tuple[IsolationProblem, ...]]:
    """Return whether this receipt's isolation claim HOLDS.

    Four checks, and the third is the one that cannot be argued with:

    * ``no_trace_return`` -- the returned text must be strictly smaller than the
      transcript when the transcript is real. Equal-or-larger is a return that
      carried the working notes.
    * ``compressed_return`` -- the ratio must clear
      :data:`MIN_COMPRESSION_RATIO`. A weak compression is reported, because a
      subagent that read 40,000 characters and returned 30,000 did not isolate
      anything.
    * **the return type cannot hold a transcript** -- checked by
      :func:`plan_research_result_isolated` against the actual dataclass, not
      against this receipt's fields. A future edit that adds a ``transcript``
      field to a result type fails HERE rather than passing quietly.
    * ``content_fenced`` when the caller requires it.

    The check is a TUPLE, not a raise, because a caller that has already spent
    the subagent's tokens wants a receipt, not an exception.
    """
    problems: List[IsolationProblem] = []
    transcript = int(receipt.transcript_chars or 0)
    returned = int(receipt.returned_chars or 0)

    if returned > 0 and transcript > 0 and returned >= transcript:
        problems.append(
            IsolationProblem(
                "no_trace_return",
                f"returned {returned} chars against a {transcript}-char transcript: "
                "the return is at least as large as the work, so the trace came back",
            )
        )
    ratio = receipt.compression_ratio
    if ratio is not None and ratio > MIN_COMPRESSION_RATIO:
        problems.append(
            IsolationProblem(
                "compressed_return",
                f"compression ratio {ratio:.1%} is weaker than the "
                f"{MIN_COMPRESSION_RATIO:.0%} floor; the return is most of the work",
            )
        )
    if require_fence and (receipt.fence is None or not receipt.fence.reviewed):
        problems.append(
            IsolationProblem(
                "content_fenced",
                "the subagent's content was not reviewed at the untrusted boundary; "
                "unreviewed repository text must not reach a parent as a summary",
            )
        )
    return (not problems), tuple(problems)


#: Fields a subagent result may carry. Read by name from the actual dataclass
#: rather than assumed, so the check cannot drift from the type it checks.
_RESULT_FIELD_DENYLIST: Tuple[str, ...] = (
    "transcript",
    "messages",
    "trace",
    "conversation",
    "history",
    "raw_messages",
    "window",
)


def plan_research_result_isolated(result_type: Any) -> Tuple[bool, Tuple[str, ...]]:
    """Return whether a subagent result TYPE can hold a transcript.

    This is the check Phase 0's audit asked for, and it is deliberately a
    check on the TYPE and not on an instance. An instance's fields are
    whatever it happens to have set; a field that exists is a field a future
    caller can put a transcript into, and the guarantee "the parent cannot
    receive the trace" has to be a property of the shape or it is a promise.

    Docstrings are excluded because a result type that DOCUMENTS holding a
    transcript is not one that can: the check reads the annotations.
    """
    annotations: Dict[str, Any] = {}
    for name in dir(result_type):
        if name.startswith("_"):
            continue
        value = getattr(result_type, name, None)
        if hasattr(value, "__annotations__"):
            annotations.update(dict(getattr(value, "__annotations__", {}) or {}))
    if not annotations and hasattr(result_type, "__annotations__"):
        annotations = dict(getattr(result_type, "__annotations__", {}) or {})
    present = tuple(
        sorted(
            name for name in annotations if str(name).lower() in _RESULT_FIELD_DENYLIST
        )
    )
    if present:
        return False, present
    return True, ()


def plan_research_result_isolated_check() -> Tuple[bool, Tuple[str, ...]]:
    """Run :func:`plan_research_result_isolated` against the real AGT-05 type.

    So the generalization is DEMONSTRATED against the instance Phase 0 audited
    rather than asserted: the existing receipt conforms, and a pin that fails
    the day someone adds a ``transcript`` field to it is one edit away.
    """
    from .subagents import PlanResearchResult

    return plan_research_result_isolated(PlanResearchResult)


def isolation_report(receipts: Sequence[IsolationReceipt]) -> Dict[str, Any]:
    """Return the run-level receipt for every subagent this run used.

    Reports the **worst** compression ratio alongside the mean, because a mean
    over subagents hides the one that pasted its work, and the one that pasted
    its work is the one that matters. A count of subagents with no receipt at
    all is reported as ``receipts_absent`` -- a subagent whose isolation was
    never measured is not an isolated subagent.
    """
    items = list(receipts or ())
    ratios = [
        item.compression_ratio for item in items if item.compression_ratio is not None
    ]
    problems: List[Dict[str, Any]] = []
    for item in items:
        ok, found = assert_isolated(item)
        if not ok:
            problems.append(
                {"agent": item.agent, "problems": [entry.to_dict() for entry in found]}
            )
    return {
        "subagents": len(items),
        "receipts_present": len(items),
        "transcript_chars_total": sum(int(item.transcript_chars) for item in items),
        "returned_chars_total": sum(int(item.returned_chars) for item in items),
        "mean_compression": (round(sum(ratios) / len(ratios), 6) if ratios else None),
        "worst_compression": round(max(ratios), 6) if ratios else None,
        "artifacts_offloaded": sum(len(item.artifacts) for item in items),
        "fenced": sum(
            1 for item in items if item.fence is not None and item.fence.reviewed
        ),
        "problems": problems,
        "all_isolated": bool(items) and not problems,
        "no_receipts": not items,
        "note": (
            "'no_receipts: true' means no subagent reported an isolation "
            "receipt, which is NOT the same as 'isolation held'. It is the "
            "absence of a measurement."
        ),
    }
