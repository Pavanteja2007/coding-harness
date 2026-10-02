"""Prompt-token estimation and a measurable context budget for kernel runs.

Character limits are not a context budget. A provider rejects a request by
*token* count, so a run that only watches characters either overflows the model
window (the provider error) or throws away most of its window (a conservative
character cap). This module makes the token cost of a request measurable, splits
it into the categories an operator actually needs to see, and decides *what*
must leave the context before the window is at risk.

Contracts:

- :class:`TokenEstimator` never raises and never requires a dependency.
  ``heuristic`` is the default (deterministic, offline, and calibrated to round
  *up* so a run compacts early rather than overrunning the window). ``auto`` and
  ``tiktoken`` opt into ``tiktoken`` when it is importable AND its encoding is
  already cached locally; a missing or uncached encoding degrades to the
  heuristic and the estimator *name* in the meter says which one ran.
- :class:`ContextBudget` owns the window, the compaction fraction (compaction
  fires at a configurable fraction of the window, never at the edge), the
  per-category shares, and the estimate of one request.
- :func:`plan_drop` returns the oldest *droppable* messages only. The base
  ``[system, user]`` frame, the structured handoff, the most recent tool result,
  and the trailing constraint re-injection are never droppable, so a compaction
  can never silently destroy the instructions the run is bound by. The
  prompt-cached prefix is protected STRUCTURALLY (see :func:`prefix_identity`),
  not by a caller remembering to pass it.
- :func:`cap_tool_output` bounds a tool result at STORAGE time, in tokens, and
  :func:`plan_fanout` / :func:`bound_fanout_output` bound how many calls one
  turn may fan out and how much they may bring back. A compaction budget is
  only a budget if the things entering the context are bounded on the way in;
  tool output is the dominant one of those, and a fan-out is unbounded in both
  directions.
- :class:`CondensationRecord` makes every compaction INSPECTABLE: which
  messages it dropped, what replaced them, and whether the cached head survived
  byte-identical. History is never rewritten, so the record plus the durable
  journal reconstruct the prior view.
- :class:`CompactionThrashPolicy` / :class:`CompactionThrashError` make a
  compaction that reclaims nothing FAIL instead of looping.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# -- categories ---------------------------------------------------------------

#: System prompt, standing rules, and the run's own instructions.
SYSTEM_RULES = "system_rules"
#: What the user asked for, including steering and re-planning messages.
USER_MESSAGES = "user_messages"
#: The model's own prior replies.
ASSISTANT = "assistant"
#: Tool outputs, which dominate a real agent session.
TOOL_RESULTS = "tool_results"
#: Files the context compiler or retrieval injected.
RETRIEVED_FILES = "retrieved_files"
#: Decision memory, compacted handoffs, and recovered pre-kill state.
MEMORY = "memory"
#: Live workspace diffs and changed-file state.
DIFFS = "diffs"
#: The re-injected constraint block that rides the end of a long context.
CONSTRAINTS = "constraints"
#: Anything a classifier could not place.
OTHER = "other"

CATEGORIES: Tuple[str, ...] = (
    SYSTEM_RULES,
    USER_MESSAGES,
    ASSISTANT,
    TOOL_RESULTS,
    RETRIEVED_FILES,
    MEMORY,
    DIFFS,
    CONSTRAINTS,
    OTHER,
)

#: Relative weights, not percentages: they are normalized against the window so
#: a caller may add a category without the shares having to sum to one.
DEFAULT_CATEGORY_SHARES: Dict[str, float] = {
    SYSTEM_RULES: 0.10,
    USER_MESSAGES: 0.10,
    ASSISTANT: 0.15,
    TOOL_RESULTS: 0.30,
    RETRIEVED_FILES: 0.20,
    MEMORY: 0.10,
    DIFFS: 0.15,
    CONSTRAINTS: 0.05,
    OTHER: 0.05,
}

#: Content markers that decide a category before the role is consulted. Ordered
#: most specific first: a diff rides inside a user message, memory rides inside a
#: user message, and both must beat the role default.
_CONTENT_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("[neo-reminder]", CONSTRAINTS),
    ("## REMINDER", CONSTRAINTS),
    ("## Live workspace state", DIFFS),
    ("Diff so far", DIFFS),
    ("## Prior diff", DIFFS),
    ("## Retrieved context", RETRIEVED_FILES),
    ("## Retrieved files", RETRIEVED_FILES),
    ("## Repository map", RETRIEVED_FILES),
    ("## Applicable instructions", SYSTEM_RULES),
    ("## Relevant past decisions", MEMORY),
    ("## Compacted earlier turns", MEMORY),
    ("## Compaction summary", MEMORY),
    ("Durable state recovered", MEMORY),
    ("TOOL RESULT ", TOOL_RESULTS),
)

#: A chat message costs role/separator tokens on top of its content.
DEFAULT_MESSAGE_OVERHEAD = 4
#: Average characters per token for source-code-shaped text.
CHARS_PER_TOKEN = 4.0

#: The window a run gets when it declared none AND the context-window authority
#: had no confident answer (no model name, authority module unavailable, or a
#: model the table has never heard of). It matches ``harness.config.DEFAULTS``'s
#: ``context_window_tokens`` so an unconfigured run is byte-identical to before
#: the authority was consulted at all.
DEFAULT_CONTEXT_WINDOW = 32768


def classify_category(message: Mapping[str, Any]) -> str:
    """Return the budget category for one provider message.

    Assumes a chat-shaped mapping with ``role`` and ``content``. Content markers
    win over the role because the kernel packs diffs, memory, retrieved files,
    and constraints into user messages by design.
    """
    content = str(message.get("content") or "")
    for marker, category in _CONTENT_MARKERS:
        if marker in content:
            return category
    role = str(message.get("role") or "user").lower()
    if role == "system":
        return SYSTEM_RULES
    if role == "assistant":
        return ASSISTANT
    if role == "tool":
        return TOOL_RESULTS
    return USER_MESSAGES


def _heuristic_tokens(text: str) -> int:
    """Return a deterministic, upward-biased token estimate for ``text``.

    Whitespace-delimited words are counted at roughly four characters per token,
    which matches source code and English prose closely enough that the estimate
    errs high. Erring high is the safe direction: a run compacts a little early
    instead of overrunning the provider window.
    """
    if not text:
        return 0
    total = 0
    for word in text.split():
        length = len(word)
        total += 1 if length <= 4 else (length + 3) // 4
    return max(1, total)


class TokenEstimator:
    """Estimate prompt tokens for a request, with a never-raise guarantee.

    ``mode`` is one of:

    ``heuristic``
        Deterministic, dependency-free, offline. The default.
    ``auto``
        Use ``tiktoken`` when it is importable and its encoding is already
        available locally; otherwise behave exactly like ``heuristic``.
    ``tiktoken``
        Request ``tiktoken`` explicitly. An unavailable encoding still degrades
        to the heuristic and records a warning rather than raising.

    The resolved :attr:`name` is written into every meter so a receipt never
    claims an exactness the run did not have.
    """

    def __init__(self, mode: str = "heuristic") -> None:
        requested = str(mode or "heuristic").strip().lower()
        self.requested = (
            requested if requested in {"heuristic", "auto", "tiktoken"} else "heuristic"
        )
        self.warnings: List[str] = []
        self._encoding: Any = None
        self._resolved = False
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        """Return the estimator actually in use (``tiktoken`` or ``heuristic``)."""
        self._resolve()
        return "tiktoken" if self._encoding is not None else "heuristic"

    def tokens(self, text: str) -> int:
        """Return the estimated token count of ``text`` (never raises)."""
        value = str(text or "")
        if not value:
            return 0
        encoding = self._encoding_or_none()
        if encoding is not None:
            try:
                return len(encoding.encode(value, disallowed_special=()))
            except Exception as exc:  # pragma: no cover - defensive
                self._warn(f"token encoding failed, using heuristic: {exc}")
        return _heuristic_tokens(value)

    def message_tokens(self, message: Mapping[str, Any]) -> int:
        """Return the estimated token cost of one chat message."""
        overhead = DEFAULT_MESSAGE_OVERHEAD
        return self.tokens(str(message.get("content") or "")) + overhead

    def messages_tokens(
        self, messages: Sequence[Mapping[str, Any]], *, overhead: Optional[int] = None
    ) -> int:
        """Return the estimated token cost of a whole request."""
        per_message = (
            DEFAULT_MESSAGE_OVERHEAD if overhead is None else max(0, int(overhead))
        )
        total = 0
        for message in messages or ():
            total += self.tokens(str(message.get("content") or "")) + per_message
        return total

    def _encoding_or_none(self) -> Any:
        if self.requested == "heuristic":
            return None
        self._resolve()
        return self._encoding

    def _resolve(self) -> None:
        with self._lock:
            if self._resolved:
                return
            self._resolved = True
            if self.requested == "heuristic":
                return
            if not _tiktoken_cache_available():
                self._warn(
                    "tiktoken encoding is not cached locally; "
                    "using the deterministic heuristic estimator"
                )
                return
            try:
                import tiktoken

                self._encoding = tiktoken.get_encoding("cl100k_base")
            except Exception as exc:
                self._warn(f"tiktoken unavailable, using heuristic: {exc}")
                self._encoding = None

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _tiktoken_cache_available() -> bool:
    """Return whether a ``tiktoken`` encoding can be resolved without network.

    ``tiktoken`` downloads its BPE file on first use. A run must never block on a
    network fetch to size a prompt, so a cache directory that already holds a
    ``cl100k_base`` blob is the only accepted offline path.
    """
    candidates = [
        os.environ.get("TIKTOKEN_CACHE_DIR"),
        os.environ.get("DATA_GYM_CACHE_DIR"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            root = os.path.expanduser(str(raw))
        except Exception:  # pragma: no cover - defensive
            continue
        try:
            for _dirpath, _dirnames, names in os.walk(root):
                if any(
                    name.startswith("9b5ad71b2ce5302211f9c61530b329a4922fc6a4")
                    for name in names
                ):
                    return True
        except OSError:
            continue
    return False


@dataclass
class ContextMeter:
    """Measured token cost of one request, split by budget category."""

    window: int = 0
    used: int = 0
    limit: int = 0
    threshold: int = 0
    message_count: int = 0
    categories: Dict[str, int] = field(default_factory=dict)
    estimator: str = "heuristic"
    turn: int = 0
    peak_utilization: float = 0.0
    compactions: int = 0
    dropped_messages: int = 0
    reinjections: int = 0
    reclaimed_tokens: int = 0

    @property
    def utilization(self) -> float:
        """Return used/window, rounded to six places (0.0 when unknown)."""
        if not self.limit:
            return 0.0
        return round(min(self.used / self.limit, 999.0), 6)

    @property
    def over_threshold(self) -> bool:
        """Return whether this request is at or past the compaction trigger."""
        return bool(self.threshold) and self.used >= self.threshold

    @property
    def over_window(self) -> bool:
        """Return whether this request would exceed the provider window."""
        return bool(self.limit) and self.used > self.limit

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable meter used by trace rows and ``--json``."""
        return {
            "window": int(self.window),
            "limit": int(self.limit or self.window),
            "used": int(self.used),
            "threshold": int(self.threshold),
            "utilization": self.utilization,
            "peak_utilization": round(float(self.peak_utilization or 0.0), 6),
            "message_count": int(self.message_count),
            "categories": {
                name: int(value) for name, value in sorted(self.categories.items())
            },
            "estimator": str(self.estimator),
            "turn": int(self.turn),
            "compactions": int(self.compactions),
            "dropped_messages": int(self.dropped_messages),
            "reinjections": int(self.reinjections),
            "reclaimed_tokens": int(self.reclaimed_tokens),
        }

    def as_event(self, **extra: Any) -> Dict[str, Any]:
        """Return a ``context_budget`` trace payload for this measurement."""
        payload = self.as_dict()
        payload.update(extra)
        return payload


class ContextBudget:
    """The window, the compaction trigger, and the per-category shares.

    Assumes ``window`` is the provider's real context size in tokens and
    ``fraction`` is the fraction of that window at which compaction fires.
    ``fraction`` is deliberately well below 1.0: compaction must happen before
    the window is at risk, not at its edge.
    """

    def __init__(
        self,
        *,
        window: int = 32768,
        fraction: float = 0.6,
        shares: Optional[Mapping[str, float]] = None,
        estimator: TokenEstimator | str = "heuristic",
        enabled: bool = True,
        reserved_output_tokens: int = 0,
        window_source: str = "",
        window_model: str = "",
        window_declared: int = 0,
        window_authority: int = 0,
    ) -> None:
        self.window = max(1024, int(window or 32768))
        raw_fraction = float(fraction if fraction is not None else 0.6)
        self.fraction = min(0.95, max(0.1, raw_fraction))
        self.shares = self._normalize(shares or DEFAULT_CATEGORY_SHARES)
        self.estimator = (
            estimator
            if isinstance(estimator, TokenEstimator)
            else TokenEstimator(str(estimator))
        )
        self.enabled = bool(enabled)
        self.reserved_output_tokens = max(0, int(reserved_output_tokens or 0))
        # Where the window came from. `runtime.model_capabilities` is the ONE
        # authority; these say whether the authority's answer was taken, and
        # what the run had declared before it was applied as a ceiling.
        self.window_source = str(window_source or "")
        self.window_model = str(window_model or "")
        self.window_declared = max(0, int(window_declared or 0))
        self.window_authority = max(0, int(window_authority or 0))

    @property
    def usable_window(self) -> int:
        """Return the input window after the reserved completion budget."""
        return max(512, self.window - self.reserved_output_tokens)

    @property
    def threshold(self) -> int:
        """Return the token count at which compaction fires."""
        return int(self.usable_window * self.fraction)

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable budget description."""
        return {
            "window": self.window,
            "usable_window": self.usable_window,
            "threshold": self.threshold,
            "fraction": round(self.fraction, 4),
            "shares": dict(self.shares),
            "estimator": self.estimator.name,
            "enabled": self.enabled,
            "window_source": self.window_source,
            "window_model": self.window_model,
            "window_declared": self.window_declared,
            "window_authority": self.window_authority,
            "window_authority_applied": bool(
                self.window_authority and self.window == self.window_authority
            ),
        }

    def measure(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        turn: int = 0,
        state: Optional[Mapping[str, Any]] = None,
    ) -> ContextMeter:
        """Return the measured token cost of one request.

        ``state`` carries the running counters (peak utilization, compactions,
        dropped messages, re-injections, reclaimed tokens) so the meter is a
        whole-run measurement rather than a per-request snapshot.
        """
        categories: Dict[str, int] = {}
        used = 0
        count = 0
        for message in messages or ():
            cost = self.estimator.message_tokens(message)
            used += cost
            count += 1
            name = classify_category(message)
            categories[name] = categories.get(name, 0) + cost
        meter = ContextMeter(
            window=self.window,
            used=used,
            limit=self.usable_window,
            threshold=self.threshold,
            message_count=count,
            categories=categories,
            estimator=self.estimator.name,
            turn=int(turn or 0),
        )
        if state:
            meter.peak_utilization = max(
                float(state.get("peak_utilization") or 0.0), meter.utilization
            )
            meter.compactions = int(state.get("compactions") or 0)
            meter.dropped_messages = int(state.get("dropped_messages") or 0)
            meter.reinjections = int(state.get("reinjections") or 0)
            meter.reclaimed_tokens = int(state.get("reclaimed_tokens") or 0)
        return meter

    def needs_compaction(self, meter: ContextMeter) -> bool:
        """Return whether the measured request is at or past the trigger."""
        if not self.enabled:
            return False
        return meter.over_threshold

    def category_caps(self) -> Dict[str, int]:
        """Return the per-category token caps implied by the shares."""
        return {
            name: int(self.usable_window * share) for name, share in self.shares.items()
        }

    @staticmethod
    def _normalize(shares: Mapping[str, float]) -> Dict[str, float]:
        values: Dict[str, float] = {}
        for name in CATEGORIES:
            try:
                value = float(shares.get(name, DEFAULT_CATEGORY_SHARES.get(name, 0.0)))
            except (TypeError, ValueError):
                value = 0.0
            values[name] = max(0.0, value)
        if sum(values.values()) <= 0:
            return dict(DEFAULT_CATEGORY_SHARES)
        return values


@dataclass
class DropPlan:
    """Which request messages a compaction may remove, and why."""

    indexes: List[int] = field(default_factory=list)
    reclaim_tokens: int = 0
    protected: List[int] = field(default_factory=list)
    reason: str = ""
    #: How many LEADING messages form the request's cached prefix. Zero when
    #: the request is empty.
    prefix_messages: int = 0
    #: The cached prefix's digest at plan time. A compaction receipt compares
    #: this against the post-compaction digest to prove the head is untouched.
    prefix_sha256: str = ""
    #: The first index a compaction may touch - ``body_after_prefix``. Everything
    #: before it is the reusable, cacheable head.
    body_from: int = 0

    @property
    def droppable(self) -> int:
        """Return how many messages the plan removes."""
        return len(self.indexes)

    @property
    def prefix_protected(self) -> bool:
        """Return whether every cached-prefix index is outside the plan.

        This is an assertion about the plan, not a promise about intent: it is
        recomputed from :attr:`indexes`, so a future change to the selection
        loop that reached into the prefix would flip it to ``False`` in the
        receipt instead of silently invalidating a provider's cache every turn.
        """
        return not (set(self.indexes) & set(range(int(self.prefix_messages or 0))))

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable plan recorded on a compaction receipt."""
        return {
            "indexes": list(self.indexes),
            "droppable": len(self.indexes),
            "reclaim_tokens": int(self.reclaim_tokens),
            "protected": list(self.protected),
            "reason": self.reason,
            "prefix_messages": int(self.prefix_messages),
            "prefix_sha256": str(self.prefix_sha256),
            "body_from": int(self.body_from),
            "prefix_protected": bool(self.prefix_protected),
        }


def plan_drop(
    messages: Sequence[Mapping[str, Any]],
    *,
    protect: Sequence[int] = (),
    reclaim_tokens: int = 0,
    estimator: Optional[TokenEstimator] = None,
    protect_tail: int = 1,
    protect_prefix: bool = True,
    tools: Optional[Sequence[Any]] = None,
    breakpoint_index: Optional[int] = None,
) -> DropPlan:
    """Return the oldest droppable message indexes for one request.

    ``protect`` holds indexes that must never be dropped (the base
    ``[system, user]`` frame, the structured handoff, a constraint
    re-injection). The final ``protect_tail`` messages are always protected too:
    the most recent tool result is the fact the model needs next, and a
    compaction that ate it would be silent context loss.

    The result is the *oldest-first prefix* of droppable messages needed to
    reclaim ``reclaim_tokens``; with ``reclaim_tokens=0`` it is every droppable
    message, which is what a summarizer needs to describe what it replaced.

    **The cached prefix is protected structurally, not by convention.**
    ``protect_prefix`` (on by default) unions the frozen-prefix boundary -
    ``runtime.prompt_cache``'s own answer, read not reimplemented - into the
    guarded set, so a caller that forgets to pass ``protect`` still cannot drop
    the prompt-cached head. That is the whole point: the head is the only part
    of a request a provider can serve from cache, so summarising it away
    re-creates the cache entry on every turn, and it is the one region whose loss
    a model cannot recover from because nothing else in the request restates it.
    Pass ``protect_prefix=False`` only in a test that is deliberately measuring
    the unprotected shape.
    """
    items = list(messages or ())
    total = len(items)
    prefix = PrefixIdentity()
    if total:
        prefix = prefix_identity(
            items,
            tools=tools,
            breakpoint_index=breakpoint_index,
            estimator=estimator,
        )
    prefix_messages = int(prefix.messages or 0)
    if not total:
        return DropPlan(reason="no messages", prefix_messages=prefix_messages)
    token_counter = estimator or TokenEstimator("heuristic")
    guarded = {int(index) for index in protect}
    if protect_prefix and prefix_messages:
        guarded.update(range(prefix_messages))
    tail_start = max(0, total - max(0, int(protect_tail)))
    droppable: List[int] = []
    skipped: List[int] = []
    for index in range(total):
        if index in guarded or index >= tail_start:
            skipped.append(index)
            continue
        droppable.append(index)
    if not droppable:
        return DropPlan(
            protected=skipped,
            reason="nothing droppable",
            prefix_messages=prefix_messages,
            prefix_sha256=prefix.sha256,
            body_from=prefix_messages,
        )
    if reclaim_tokens <= 0:
        return DropPlan(
            indexes=droppable,
            reclaim_tokens=sum(
                token_counter.message_tokens(items[i]) for i in droppable
            ),
            protected=skipped,
            reason="compact every droppable message",
            prefix_messages=prefix_messages,
            prefix_sha256=prefix.sha256,
            body_from=prefix_messages,
        )
    chosen: List[int] = []
    reclaimed = 0
    for index in droppable:
        chosen.append(index)
        reclaimed += token_counter.message_tokens(items[index])
        if reclaimed >= reclaim_tokens:
            break
    return DropPlan(
        indexes=chosen,
        reclaim_tokens=reclaimed,
        protected=skipped,
        reason="oldest-first reclaim",
        prefix_messages=prefix_messages,
        prefix_sha256=prefix.sha256,
        body_from=prefix_messages,
    )


def digest_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable digest of a request, used by compaction receipts."""
    payload = "\n".join(
        f"{item.get('role') or 'user'!s}:"
        f"{hashlib.sha256(str(item.get('content') or '').encode('utf-8', 'replace')).hexdigest()[:16]}"
        for item in messages or ()
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def render_meter_line(meter: ContextMeter) -> str:
    """Return a one-line human rendering of a meter for CLI/TUI surfaces."""
    if not meter.limit:
        return "context: unknown"
    percent = round(meter.utilization * 100)
    return (
        f"context {meter.used}/{meter.limit} tok ({percent}%) "
        f"peak {round(meter.peak_utilization * 100)}% "
        f"compactions {meter.compactions} [{meter.estimator}]"
    )


# ---------------------------------------------------------------------------
# Tool-output storage cap and fan-out bounds (AGT-04)
# ---------------------------------------------------------------------------
#
# Two costs a context budget cannot see coming unless something bounds them at
# the moment the data is STORED, and both are paid by the same line of code:
#
# - **Tool output is the dominant context cost** in a real agent session - far
#   more than the model's own replies. Capping it at RENDER time is too late:
#   the uncapped text is already in the conversation, the journal and the next
#   request. :func:`cap_tool_output` is therefore applied before storage, and
#   it says what it did rather than quietly shortening a payload.
# - **A fan-out is unbounded in two directions**: how many calls run at once,
#   and how much they bring back. :func:`plan_fanout` bounds both, and a call
#   it refuses is a *refusal with a reason*, not a silent drop.

#: Default cap on a single tool result, in TOKENS. Applied at storage time.
TOOL_OUTPUT_TOKEN_LIMIT = 4000

#: Default ceiling on how many calls one turn may fan out, across both
#: concurrency classes. A model that emits 200 reads in one turn is describing
#: a problem, not solving it, and running all of them is how a turn times out.
FANOUT_MAX_CALLS = 8

#: Default ceiling on the COMBINED characters one turn's fan-out may return.
#: Per-result caps do not bound a fan-out: ten individually-legal results are
#: still ten results' worth of context.
FANOUT_MAX_OUTPUT_CHARS = 24000

#: Marker written where output was shortened. Never elided: a silently
#: truncated payload is indistinguishable from a complete one.
CAP_MARKER = "[neo-truncated"


@dataclass
class ToolOutputCap:
    """One tool result after the storage-time cap, plus what the cap did."""

    text: str = ""
    limit: int = 0
    original_chars: int = 0
    kept_chars: int = 0
    estimator: str = "heuristic"
    truncated: bool = False
    reason: str = ""

    @property
    def marker(self) -> str:
        """Return the truncation marker embedded in :attr:`text`, if any."""
        return CAP_MARKER if self.truncated else ""

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable receipt for a trace row."""
        return {
            "limit": int(self.limit),
            "original_chars": int(self.original_chars),
            "kept_chars": int(self.kept_chars),
            "estimator": str(self.estimator),
            "truncated": bool(self.truncated),
            "reason": str(self.reason),
        }


def cap_tool_output(
    value: Any,
    *,
    token_limit: int = TOOL_OUTPUT_TOKEN_LIMIT,
    estimator: Optional[TokenEstimator] = None,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> ToolOutputCap:
    """Cap one tool result at STORAGE time, in tokens.

    The cap is expressed in tokens because the budget is a token budget: a
    character cap is a proxy that is wrong in both directions depending on the
    content. The head is kept and the tail is dropped, because the head of a
    tool result is the thing that was asked for; the marker names what was lost
    and how much, so a reader can tell a shortened payload from a complete one.

    A non-positive ``token_limit`` disables the cap (returns the text
    unchanged, ``truncated=False``) rather than emptying every result - a
    config that means "no cap" must be expressible.

    Assumes ``value`` is anything ``str()`` renders; never raises.
    """
    text = value if isinstance(value, str) else str(value if value is not None else "")
    try:
        limit = int(token_limit)
    except (TypeError, ValueError):
        limit = TOOL_OUTPUT_TOKEN_LIMIT
    counter = estimator or TokenEstimator("heuristic")
    if limit <= 0:
        return ToolOutputCap(
            text=text,
            limit=0,
            original_chars=len(text),
            kept_chars=len(text),
            estimator=counter.name,
            truncated=False,
            reason="disabled",
        )
    try:
        chars = max(1, round(limit * max(1.0, float(chars_per_token))))
    except (TypeError, ValueError):
        chars = max(1, round(limit * CHARS_PER_TOKEN))
    if len(text) <= chars:
        return ToolOutputCap(
            text=text,
            limit=limit,
            original_chars=len(text),
            kept_chars=len(text),
            estimator=counter.name,
            truncated=False,
        )
    dropped = len(text) - chars
    marker = f"\n{CAP_MARKER}: {dropped} of {len(text)} chars omitted]\n"
    kept = text[:chars]
    # A result that only fits by being cut must still be legible, so a very
    # small cap spends its budget on the marker rather than on a stub.
    if len(kept) + len(marker) > chars + 32:
        kept = kept[: max(0, chars - len(marker))]
    return ToolOutputCap(
        text=kept + marker,
        limit=limit,
        original_chars=len(text),
        kept_chars=len(kept),
        estimator=counter.name,
        truncated=True,
        reason="tool_output_token_limit",
    )


@dataclass
class FanoutBounds:
    """The three limits one turn's tool fan-out is held to."""

    max_concurrency: int = 4
    max_calls: int = FANOUT_MAX_CALLS
    max_output_chars: int = FANOUT_MAX_OUTPUT_CHARS
    tool_output_tokens: int = TOOL_OUTPUT_TOKEN_LIMIT

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable bounds for a trace row."""
        return {
            "max_concurrency": int(self.max_concurrency),
            "max_calls": int(self.max_calls),
            "max_output_chars": int(self.max_output_chars),
            "tool_output_tokens": int(self.tool_output_tokens),
        }


def fanout_bounds_from_config(
    config: Optional[Mapping[str, Any]] = None,
) -> FanoutBounds:
    """Read the fan-out and tool-output bounds from a run's config.

    Every value is read from config rather than hardcoded, per the project's
    reproducibility rule: a run stays reproducible from its merged config. An
    unusable value falls back to the documented default and is never allowed to
    become zero, because ``max_calls=0`` would silently refuse every call.
    """
    values = dict(config or {})

    def _int(key: str, default: int, floor: int = 1) -> int:
        raw = values.get(key)
        if raw is None:
            return default
        try:
            return max(floor, int(raw))
        except (TypeError, ValueError):
            return default

    return FanoutBounds(
        max_concurrency=_int("max_parallel_tools", 4),
        max_calls=_int("max_tool_fanout", FANOUT_MAX_CALLS),
        max_output_chars=_int(
            "max_tool_fanout_chars", FANOUT_MAX_OUTPUT_CHARS, floor=256
        ),
        tool_output_tokens=_int(
            "tool_output_token_limit", TOOL_OUTPUT_TOKEN_LIMIT, floor=0
        ),
    )


@dataclass
class FanoutPlan:
    """How one turn's tool calls split, and which were refused.

    ``concurrent`` is the read-only class - calls the catalog declares safe to
    run at the same time as each other. ``sequential`` is everything else,
    preserved in the order the model emitted it, because a mutation's order is
    meaning (an edit then a test, not a test then an edit). ``refused`` pairs a
    call with the reason it was not run; a refusal is always a value, never a
    disappearance.
    """

    concurrent: List[Any] = field(default_factory=list)
    sequential: List[Any] = field(default_factory=list)
    refused: List[Tuple[Any, str]] = field(default_factory=list)
    bounds: Dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Return how many calls the plan accounts for."""
        return len(self.concurrent) + len(self.sequential) + len(self.refused)

    def order(self) -> List[Any]:
        """Return every planned call in the order the model emitted it.

        Concurrent results have to be put back where the model wrote them or the
        tool-result feedback reads as a different conversation than the one the
        model had.
        """
        index: Dict[int, Any] = {}
        seen: Dict[int, int] = {}
        for position, call in enumerate(self.concurrent):
            index[id(call)] = call
            seen[id(call)] = position
        return list(self.sequential) + list(self.concurrent)


def plan_fanout(
    calls: Sequence[Any],
    *,
    bounds: Optional[FanoutBounds] = None,
    is_read_only: Optional[Any] = None,
) -> FanoutPlan:
    """Split one turn's calls into a concurrent class and a sequential one.

    ``is_read_only`` is a callable asking the question for one call — the
    registry's ``is_concurrent`` in production. It is a parameter rather than
    an import so this module stays the lowest layer and cannot grow a
    dependency on the tool registry.

    The answer is read STRICTLY: only ``True`` (or the literal string
    ``"concurrent"``) counts as concurrent. That is not pedantry — a predicate
    that happens to return a descriptive string would make ``bool("sequential")``
    true and schedule every mutation as if it were a read, which is exactly the
    bug this strictness exists to prevent.

    ``max_calls`` bounds the TOTAL number of calls a turn may execute, not just
    the concurrent class: a turn that emits 200 writes would otherwise be
    unbounded in a way a turn emitting 200 reads is not. The budget is spent
    READ-ONLY FIRST, in emission order, and that is deliberate: a read is how a
    model finds out what to mutate, so refusing a read to fit a mutation in
    would refuse the information the mutation needs. A call over the budget is
    REFUSED WITH A REASON — never deferred silently, because a mutation that
    quietly runs a turn later than the model expected is a lost edit.

    Never raises: an unusable ``is_read_only`` is treated as "not read only",
    which is the conservative direction.
    """
    limits = bounds or FanoutBounds()
    budget = max(1, int(limits.max_calls))
    items = list(calls or ())
    classes: List[bool] = []
    for call in items:
        try:
            answer = is_read_only(call) if callable(is_read_only) else False
        except Exception:
            answer = False
        classes.append(answer is True or answer == "concurrent")
    concurrent: List[Any] = []
    sequential: List[Any] = []
    refused: List[Tuple[Any, str]] = []
    spent = 0
    decided = [False] * len(items)
    for pass_read_only in (True, False):
        for index, call in enumerate(items):
            if decided[index] or classes[index] is not pass_read_only:
                continue
            decided[index] = True
            if spent >= budget:
                refused.append(
                    (
                        call,
                        f"fan-out budget of {budget} tool call(s) per turn is "
                        "exhausted",
                    )
                )
                continue
            spent += 1
            (concurrent if pass_read_only else sequential).append(call)
    return FanoutPlan(
        concurrent=concurrent,
        sequential=sequential,
        refused=refused,
        bounds=limits.as_dict(),
    )


def bound_fanout_output(
    results: Sequence[Tuple[Any, Any]],
    *,
    max_output_chars: int = FANOUT_MAX_OUTPUT_CHARS,
) -> Tuple[List[Tuple[Any, Any]], Dict[str, Any]]:
    """Bound the COMBINED size of one turn's fan-out results.

    Per-result caps do not bound a fan-out, so this does: each result gets a
    fair share of what is left, in emission order, and once the budget is spent
    the remaining results are REPLACED BY A BOUNDED NOTE naming the tool rather
    than returned whole. A note is not the answer, so the receipt says
    ``replaced`` and the caller can tell a withheld result from an empty one.

    Returns ``(pairs, receipt)``. Never raises; a result that is not a 2-tuple
    is passed through untouched and counted as ``unmeasured``.
    """
    try:
        budget = max(0, int(max_output_chars))
    except (TypeError, ValueError):
        budget = FANOUT_MAX_OUTPUT_CHARS
    pairs: List[Tuple[Any, Any]] = []
    consumed = 0
    replaced = 0
    unmeasured = 0
    truncated = 0
    for item in list(results or ()):
        try:
            call, result = item
        except (TypeError, ValueError):
            pairs.append(item)
            unmeasured += 1
            continue
        text = getattr(result, "output", result)
        size = len(text if isinstance(text, str) else str(text or ""))
        remaining = budget - consumed
        if remaining <= 0:
            name = str(getattr(call, "tool", "") or "tool")
            note = (
                f"{CAP_MARKER}: fan-out output budget of {budget} chars is spent; "
                f"the result of {name} was withheld. Re-run it alone if you "
                "need it.]"
            )
            replacement = _replace_output(result, note)
            pairs.append((call, replacement))
            replaced += 1
            continue
        if size <= remaining:
            consumed += size
            pairs.append((call, result))
            continue
        head = text if isinstance(text, str) else str(text or "")
        cap = cap_tool_output(
            head,
            token_limit=max(1, int(remaining / CHARS_PER_TOKEN)),
        )
        pairs.append((call, _replace_output(result, cap.text)))
        consumed += remaining
        truncated += 1
    receipt = {
        "max_output_chars": budget,
        "returned": len(pairs),
        "measured_chars": consumed,
        "truncated": truncated,
        "replaced": replaced,
        "unmeasured": unmeasured,
        "bounded": bool(truncated or replaced),
    }
    return pairs, receipt


def _replace_output(result: Any, text: str) -> Any:
    """Return ``result`` with its output replaced, mutating in place when safe.

    The fan-out bound is applied to results the strategy is about to store, so
    it must replace the stored value rather than wrap it. A result type that
    does not expose a writable ``output`` is returned unchanged with the text
    as a last resort, and the receipt's ``unmeasured`` count is the caller's
    signal that this happened.
    """
    if result is not None and hasattr(result, "output"):
        try:
            result.output = text
            return result
        except Exception:
            pass
    return text


def budget_from_config(
    config: Optional[Mapping[str, Any]] = None,
    *,
    model: str = "",
) -> ContextBudget:
    """Build a :class:`ContextBudget` from a task/session config mapping.

    **The window has ONE authority.** ``runtime.model_capabilities`` owns the
    resolution ladder (injected probe -> litellm model info -> local table ->
    documented floor) and the per-``(api_base, model)`` cache; this function
    READS it. It used to resolve a second window from its own config keys, and
    two sources that can disagree is exactly how a 32k model ends up being run
    with a 128k budget and an over-window request.

    The two rules that make "reads it" true:

    - A config key may **lower** the window, never raise it above what the
      authority resolved. Raising a budget past the model's real window can only
      produce a request the provider rejects, so the authority is a ceiling and
      the config is a declared intent the ceiling can veto.
    - The authority's *floor* rung is a refusal to guess, not a capability
      answer, so it never shrinks a run's declared budget. An unknown model
      keeps whatever the run declared.

    ``window_source`` on the budget always names the rung that produced the
    number, so a reader can tell a measured window from a declared one and from
    a floor. Every value is read from config rather than hardcoded, per the
    project's reproducibility rule.
    """
    values = dict(config or {})
    name = str(model or values.get("model") or "")
    declared, declared_source = _declared_window(values, name)
    authority, authority_source = _resolve_window_authority(name, values)
    window = declared
    source = declared_source
    if authority > 0 and (window <= 0 or authority < window):
        window = authority
        source = authority_source
    if window <= 0:
        window = DEFAULT_CONTEXT_WINDOW
        source = "default"
    return ContextBudget(
        window=window,
        fraction=float(values.get("context_compaction_fraction", 0.6) or 0.6),
        shares=values.get("context_category_shares") or DEFAULT_CATEGORY_SHARES,
        estimator=str(
            values.get("context_token_estimator", "heuristic") or "heuristic"
        ),
        enabled=bool(values.get("context_budget_enabled", True)),
        reserved_output_tokens=int(
            values.get("context_reserved_output_tokens", 0) or 0
        ),
        window_source=source,
        window_model=name,
        window_declared=declared,
        window_authority=authority,
    )


def _declared_window(values: Mapping[str, Any], name: str) -> Tuple[int, str]:
    """Return the window THIS run declared, and which key declared it.

    ``context_window_by_model`` (a per-model pin, matched on a name or a name
    prefix) is the more specific declaration and wins over the global
    ``context_window_tokens``. ``(0, "")`` means nothing was declared.

    ``None``/absent is "not declared" rather than a value: a value in
    ``harness.config.DEFAULTS`` is merged into EVERY task, so a key that meant
    "unset" by default would silently become a declared window everywhere.
    """
    by_model = values.get("context_window_by_model")
    if isinstance(by_model, Mapping) and name:
        for key, candidate in by_model.items():
            token = str(key)
            if token and (name == token or name.startswith(token)):
                try:
                    return int(candidate), f"config:context_window_by_model[{token}]"
                except (TypeError, ValueError):
                    continue
    declared = values.get("context_window_tokens")
    if declared is None:
        return 0, ""
    try:
        return int(declared), "config:context_window_tokens"
    except (TypeError, ValueError):
        return 0, ""


def _resolve_window_authority(name: str, values: Mapping[str, Any]) -> Tuple[int, str]:
    """Ask the ONE context-window authority what this model's window is.

    ``runtime.model_capabilities.resolve_context_window`` is the authority. It
    is imported lazily and defensively for two honest reasons: the harness must
    stay importable with no runtime package present, and its ladder imports
    litellm (which is slow enough to dominate a test suite) unless the provider
    is one the harness implements itself.

    Returns ``(0, source)`` when there is no usable answer, which the caller
    reads as "no ceiling to apply" - never as a window of zero. A zero window is
    not "unknown", it is a lie a budgeter acts on.
    """
    if not name:
        return 0, ""
    try:
        from runtime.model_capabilities import resolve_context_window
    except Exception:
        return 0, "authority_unavailable"
    try:
        record = resolve_context_window(
            name,
            provider=values.get("provider"),
            api_base=values.get("api_base"),
            probe=values.get("context_window_probe"),
        )
    except Exception:
        return 0, "authority_error"
    try:
        window = int(record.get("context_window") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0, "authority_error"
    source = str(record.get("context_window_source") or "")
    if window <= 0 or source == "fallback":
        # The documented floor is a refusal to guess. It must not be allowed to
        # shrink a run's declared budget for a model the table simply has not
        # heard of yet.
        return 0, source
    return window, source


# ---------------------------------------------------------------------------
# Cached-prefix safety, replayable condensation, and thrash refusal (AGT-06)
# ---------------------------------------------------------------------------
#
# Three defects a token budget alone does not catch, all of them about WHAT the
# budget spends and on what:
#
# - **The prefix is not the budget's to spend.** A provider prompt cache only
#   pays when the request begins with a byte-identical prefix, so a compaction
#   that summarises the cached head away trades a cheap long request for a
#   re-created cache entry on every single turn. Codex scopes auto-compaction to
#   ``body_after_prefix`` for exactly this reason. :func:`prefix_identity` says
#   where that boundary is, and :func:`plan_drop` protects it structurally, so a
#   caller that forgets to protect it still cannot drop it.
# - **A compaction nobody can read is a compaction nobody can debug.** History is
#   never rewritten; instead :class:`CondensationRecord` names every dropped
#   message (identity plus content digest) and carries the replacement summary
#   verbatim. Combined with the durable journal's content, that record
#   reconstructs the exact prior view - see
#   ``conversation.reconstruct_prior_view``.
# - **Compaction that reclaims nothing is a loop, not a strategy.**
#   :class:`CompactionThrashPolicy` watches consecutive no-progress compactions
#   and :class:`CompactionThrashError` aborts the run with a reason instead of
#   compacting again.

#: How many consecutive compactions may reclaim NOTHING before the run is
#: aborted. ``0`` disables the guard. The default is a cap and not a switch:
#: "compact repeatedly without progress" is a bug in the budget, and a cap that
#: only exists when asked for is not a cap.
DEFAULT_COMPACTION_THRASH_LIMIT = 3

#: The smallest reclaim that counts as progress. One token is the honest floor:
#: any reclaim at all is progress, and zero is not. It is deliberately not a
#: percentage - a compaction that reclaims 1% of a large window every turn is
#: making progress, and aborting it would be the harness guessing.
DEFAULT_MIN_RECLAIM_TOKENS = 1

#: Closed set of reasons a compaction was refused rather than retried. A refusal
#: names itself; it is never a disappearance.
THRASH_REASON_NO_PROGRESS = "compaction_no_progress"
THRASH_REASONS = (THRASH_REASON_NO_PROGRESS,)


@dataclass(frozen=True)
class PrefixIdentity:
    """The byte-stable, prompt-cacheable head of a request.

    ``messages`` is how many LEADING messages form the prefix (the boundary
    ``runtime.prompt_cache`` would attach a provider breakpoint to), ``sha256``
    is that prefix's digest INCLUDING the normalized tool schemas when the
    runtime authority is importable, and ``source`` names which implementation
    produced it so a receipt never claims a precision it did not have.
    """

    messages: int = 0
    sha256: str = ""
    tokens: int = 0
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable form recorded on a compaction receipt."""
        return {
            "messages": int(self.messages),
            "sha256": str(self.sha256),
            "tokens": int(self.tokens),
            "source": str(self.source),
        }


def _prefix_payload(items: Sequence[Mapping[str, Any]]) -> str:
    """Return the local fallback's canonical text for a prefix."""
    return "\n".join(
        f"{str(item.get('role') or 'user')!s}:{item.get('content') or ''}"
        for item in items
    )


def _prefix_length(
    items: Sequence[Mapping[str, Any]], breakpoint_index: Optional[int]
) -> Tuple[int, bool]:
    """Return ``(messages, used_runtime_authority)`` for the cached prefix.

    ``runtime.prompt_cache`` is the ONE authority for where a cache breakpoint
    sits, and this module READS it rather than keeping a second opinion. It is
    imported defensively for the same reason ``budget_from_config`` imports the
    context-window authority lazily: the harness must stay importable with no
    runtime package present. A caller that knows its prefix is longer says so
    with ``breakpoint_index``.
    """
    try:
        from runtime.prompt_cache import split_at_breakpoint

        prefix, _suffix, _index = split_at_breakpoint(items, None, breakpoint_index)
        return len(prefix), True
    except Exception:
        pass
    if breakpoint_index is not None:
        try:
            return max(1, int(breakpoint_index) + 1), False
        except (TypeError, ValueError):
            pass
    return (1 if items else 0), False


def prefix_identity(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Optional[Sequence[Any]] = None,
    breakpoint_index: Optional[int] = None,
    estimator: Optional[TokenEstimator] = None,
) -> PrefixIdentity:
    """Return the identity of the request's cached, byte-stable prefix.

    The returned digest is what "the cached prefix is unchanged" means for a
    receipt: two measurements with the same ``sha256`` are the same bytes, so a
    compaction that changed the head shows up as a changed digest rather than as
    a silent cache miss nobody attributes.

    Never raises. Without the runtime authority it falls back to the leading
    message (or an explicit ``breakpoint_index``) and hashes the content
    locally, and :attr:`PrefixIdentity.source` says so.
    """
    items = list(messages or ())
    if not items:
        return PrefixIdentity(source="empty")
    count, used_runtime = _prefix_length(items, breakpoint_index)
    count = max(1, min(int(count), len(items)))
    head = items[:count]
    digest = ""
    if used_runtime:
        try:
            from runtime.prompt_cache import prefix_digest

            digest = str(prefix_digest(items, tools, breakpoint_index))
        except Exception:
            used_runtime = False
    if not digest:
        digest = hashlib.sha256(
            _prefix_payload(head).encode("utf-8", "replace")
        ).hexdigest()
    counter = estimator if isinstance(estimator, TokenEstimator) else None
    return PrefixIdentity(
        messages=count,
        sha256=digest,
        tokens=(counter or TokenEstimator("heuristic")).messages_tokens(head),
        source="runtime.prompt_cache" if used_runtime else "budget.local",
    )


@dataclass(frozen=True)
class DroppedMessage:
    """One message a condensation dropped: identity and content digest, no text.

    The CONTENT is deliberately absent. The durable journal is what stores it,
    and a record that carried the text would make the in-memory conversation
    grow exactly as fast as the compaction that was supposed to bound it. The
    digest is enough to prove a reconstruction is byte-exact.
    """

    seq: int = 0
    turn: int = 0
    kind: str = "note"
    role: str = "user"
    tool: str = ""
    ok: Optional[bool] = None
    target: str = ""
    chars: int = 0
    content_sha256: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable dropped-message record."""
        return {
            "seq": int(self.seq),
            "turn": int(self.turn),
            "kind": str(self.kind),
            "role": str(self.role),
            "tool": str(self.tool),
            "ok": self.ok,
            "target": str(self.target),
            "chars": int(self.chars),
            "content_sha256": str(self.content_sha256),
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "DroppedMessage":
        """Rebuild one dropped-message record from its projection."""
        data = dict(values or {})
        ok = data.get("ok")
        return cls(
            seq=int(data.get("seq") or 0),
            turn=int(data.get("turn") or 0),
            kind=str(data.get("kind") or "note"),
            role=str(data.get("role") or "user"),
            tool=str(data.get("tool") or ""),
            ok=None if ok is None else bool(ok),
            target=str(data.get("target") or ""),
            chars=int(data.get("chars") or 0),
            content_sha256=str(data.get("content_sha256") or ""),
        )


@dataclass(frozen=True)
class CondensationRecord:
    """What one compaction replaced, recorded so the swap can be inspected.

    This is the OpenHands-style "which events were forgotten, and what replaced
    them" record. It answers, without reading any other artifact:

    * **what was dropped** - one :class:`DroppedMessage` per message, in order;
    * **what replaced it** - the summary VERBATIM, not a digest of it;
    * **what the head looked like** - the cached prefix digest before and after,
      and whether it survived byte-identical;
    * **whether it can be undone** - ``reversible`` names the journal rows and
      the restore call that un-drops exactly these sequences.

    ``reclaimed_tokens`` is the honest measure of whether the compaction was
    worth anything, and :attr:`progress` is the boolean the thrash policy reads.
    """

    compaction_id: str = ""
    method: str = "structural_trim"
    dropped: Tuple[DroppedMessage, ...] = ()
    summary: str = ""
    summary_sha256: str = ""
    summary_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    reclaimed_tokens: int = 0
    limit_tokens: int = 0
    first_turn: int = 0
    last_turn: int = 0
    #: The handoff as it stood BEFORE this condensation, so a replay can rebuild
    #: the pre-compaction view without consulting any later state.
    handoff_before: Mapping[str, Any] = field(default_factory=dict)
    cache_prefix_messages: int = 0
    cache_prefix_sha256_before: str = ""
    cache_prefix_sha256_after: str = ""
    cache_prefix_preserved: bool = True
    #: The first index a compaction may touch - Codex's ``body_after_prefix``.
    body_from: int = 0
    protected: Tuple[int, ...] = ()
    error: str = ""

    @property
    def progress(self) -> bool:
        """Return whether this condensation reclaimed anything at all."""
        return int(self.reclaimed_tokens) > 0

    @property
    def dropped_seqs(self) -> List[int]:
        """Return the journal sequences this condensation removed."""
        return [int(item.seq) for item in self.dropped]

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable record (the trace event and journal row)."""
        return {
            "compaction_id": str(self.compaction_id),
            "method": str(self.method),
            "dropped": [item.as_dict() for item in self.dropped],
            "dropped_count": len(self.dropped),
            "dropped_seqs": list(self.dropped_seqs),
            "summary": str(self.summary),
            "summary_sha256": str(self.summary_sha256),
            "summary_chars": int(self.summary_chars),
            "before_tokens": int(self.before_tokens),
            "after_tokens": int(self.after_tokens),
            "reclaimed_tokens": int(self.reclaimed_tokens),
            "limit_tokens": int(self.limit_tokens),
            "first_turn": int(self.first_turn),
            "last_turn": int(self.last_turn),
            "handoff_before": dict(self.handoff_before or {}),
            "cache_prefix_messages": int(self.cache_prefix_messages),
            "cache_prefix_sha256_before": str(self.cache_prefix_sha256_before),
            "cache_prefix_sha256_after": str(self.cache_prefix_sha256_after),
            "cache_prefix_preserved": bool(self.cache_prefix_preserved),
            "body_from": int(self.body_from),
            "protected": [int(item) for item in self.protected],
            "progress": bool(self.progress),
            "error": str(self.error),
            "reversible": {
                "mechanism": "append-only conversation journal",
                "restore": "ConversationJournal.restore_compaction(compaction_id)",
                "un_drops": list(self.dropped_seqs),
            },
        }

    def as_event(self, **extra: Any) -> Dict[str, Any]:
        """Return a ``context_condensed`` trace payload for this record."""
        payload = self.as_dict()
        payload.update(extra)
        return payload

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "CondensationRecord":
        """Rebuild one record from its projection, tolerating a missing field."""
        data = dict(values or {})
        handoff = data.get("handoff_before")
        return cls(
            compaction_id=str(data.get("compaction_id") or ""),
            method=str(data.get("method") or "structural_trim"),
            dropped=tuple(
                DroppedMessage.from_mapping(item) for item in data.get("dropped") or ()
            ),
            summary=str(data.get("summary") or ""),
            summary_sha256=str(data.get("summary_sha256") or ""),
            summary_chars=int(data.get("summary_chars") or 0),
            before_tokens=int(data.get("before_tokens") or 0),
            after_tokens=int(data.get("after_tokens") or 0),
            reclaimed_tokens=int(data.get("reclaimed_tokens") or 0),
            limit_tokens=int(data.get("limit_tokens") or 0),
            first_turn=int(data.get("first_turn") or 0),
            last_turn=int(data.get("last_turn") or 0),
            handoff_before=dict(handoff) if isinstance(handoff, Mapping) else {},
            cache_prefix_messages=int(data.get("cache_prefix_messages") or 0),
            cache_prefix_sha256_before=str(
                data.get("cache_prefix_sha256_before") or ""
            ),
            cache_prefix_sha256_after=str(data.get("cache_prefix_sha256_after") or ""),
            cache_prefix_preserved=bool(data.get("cache_prefix_preserved", True)),
            body_from=int(data.get("body_from") or 0),
            protected=tuple(int(item) for item in data.get("protected") or ()),
            error=str(data.get("error") or ""),
        )


class CompactionThrashError(RuntimeError):
    """Raised when compaction repeats without reclaiming anything.

    A run that compacts again and again while the request stays the same size is
    not making progress; it is spending a model call per turn to produce an
    identical request. Claude Code's answer to that shape is to bail with an
    error, and so is this one: the run ends ``failed`` with this message as its
    reason. It can never be reported as a completion, verified or otherwise.
    """

    def __init__(
        self,
        reason: str,
        *,
        streak: int = 0,
        limit: int = 0,
        min_reclaim_tokens: int = 0,
        receipt: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.reason = str(reason or THRASH_REASON_NO_PROGRESS)
        self.streak = max(0, int(streak))
        self.limit = max(0, int(limit))
        self.min_reclaim_tokens = max(0, int(min_reclaim_tokens))
        self.receipt = dict(receipt or {})
        super().__init__(
            f"context compaction made no progress {self.streak} time(s) in a row "
            f"(limit {self.limit}, minimum reclaim {self.min_reclaim_tokens} "
            f"token(s)); aborting the run instead of compacting again: "
            f"{self.reason}"
        )

    def as_dict(self) -> Dict[str, Any]:
        """Return the serializable abort record for a receipt or a trace row."""
        return {
            "reason": self.reason,
            "reasons": list(THRASH_REASONS),
            "streak": int(self.streak),
            "limit": int(self.limit),
            "min_reclaim_tokens": int(self.min_reclaim_tokens),
            "compaction_id": str(self.receipt.get("compaction_id") or ""),
            "before_tokens": int(self.receipt.get("before_tokens") or 0),
            "after_tokens": int(self.receipt.get("after_tokens") or 0),
            "reclaimed_tokens": int(self.receipt.get("reclaimed_tokens") or 0),
            "limit_tokens": int(self.receipt.get("limit_tokens") or 0),
            "error": str(self),
        }


@dataclass
class CompactionThrashPolicy:
    """How many unproductive compactions are tolerated before the run aborts.

    **What counts as progress is "the compaction achieved its purpose", not
    "the token count went down".** A compaction's purpose is to bring the
    request back under its trigger. One that leaves the request at or above the
    trigger will be asked again on the next turn with the same inputs and the
    same answer, so a run of them spends a turn each time to rebuild the request
    it already had. That is the loop the guard exists to break, and a
    token-count test would miss it: a compaction can reclaim a token and still
    have accomplished nothing.

    ``limit=0`` disables the guard entirely. The streak RESETS on the first
    productive compaction, so a run that compacts twice, gets under the trigger,
    and compacts again is never accused of thrashing - only an unbroken run of
    compactions that change nothing can be.
    """

    limit: int = DEFAULT_COMPACTION_THRASH_LIMIT
    min_reclaim_tokens: int = DEFAULT_MIN_RECLAIM_TOKENS

    @property
    def enabled(self) -> bool:
        """Return whether the guard can refuse anything."""
        return int(self.limit) > 0

    def made_progress(self, receipt: Mapping[str, Any]) -> bool:
        """Return whether one compaction receipt achieved its purpose.

        With a trigger in the receipt the test is ``after < limit`` - the
        request is back under the line. Without one (a caller that measures
        without a budget) the floor is the reclaimed token count, so the policy
        still means something instead of defaulting to "always progress".
        """
        values = dict(receipt or {})
        try:
            limit = int(values.get("limit_tokens") or 0)
            after = int(values.get("after_tokens") or 0)
        except (TypeError, ValueError):
            return False
        if limit > 0:
            return after < limit
        try:
            return int(values.get("reclaimed_tokens") or 0) >= max(
                0, int(self.min_reclaim_tokens)
            )
        except (TypeError, ValueError):
            return False

    def note(self, receipt: Mapping[str, Any], *, streak: int = 0) -> int:
        """Return the consecutive no-progress streak after observing a receipt."""
        return 0 if self.made_progress(receipt) else max(0, int(streak)) + 1

    def decide(self, streak: int) -> str:
        """Return "" while compaction still makes progress, else the reason.

        The reason is a human sentence, not a slug: the abort is reported to a
        user, and "compaction thrashed" without the numbers is not a diagnosis.
        """
        if not self.enabled or int(streak) < int(self.limit):
            return ""
        return (
            f"{int(streak)} consecutive compactions left the request at or above "
            "its compaction trigger, so the next turn would compact again and "
            "reach the same place; the protected prefix plus the handoff alone "
            f"({self.min_reclaim_tokens} token floor) cannot be reclaimed"
        )


def compaction_thrash_policy_from_config(
    config: Optional[Mapping[str, Any]] = None,
) -> CompactionThrashPolicy:
    """Build the thrash policy from a run's config mapping.

    Every value is read from config rather than hardcoded, per the project's
    reproducibility rule. An unusable value falls back to the documented
    default; ``0`` is a real value (the guard is off) and is never confused with
    "unset".
    """
    values = dict(config or {})

    def _int(key: str, default: int, floor: int = 0) -> int:
        raw = values.get(key)
        if raw is None:
            return default
        try:
            return max(floor, int(raw))
        except (TypeError, ValueError):
            return default

    return CompactionThrashPolicy(
        limit=_int("compaction_thrash_limit", DEFAULT_COMPACTION_THRASH_LIMIT),
        min_reclaim_tokens=_int(
            "compaction_min_reclaim_tokens", DEFAULT_MIN_RECLAIM_TOKENS
        ),
    )


__all__ = [
    "ASSISTANT",
    "CAP_MARKER",
    "CATEGORIES",
    "CHARS_PER_TOKEN",
    "CONSTRAINTS",
    "DEFAULT_CATEGORY_SHARES",
    "DEFAULT_COMPACTION_THRASH_LIMIT",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MESSAGE_OVERHEAD",
    "DEFAULT_MIN_RECLAIM_TOKENS",
    "DIFFS",
    "FANOUT_MAX_CALLS",
    "FANOUT_MAX_OUTPUT_CHARS",
    "MEMORY",
    "OTHER",
    "RETRIEVED_FILES",
    "SYSTEM_RULES",
    "THRASH_REASONS",
    "THRASH_REASON_NO_PROGRESS",
    "TOOL_OUTPUT_TOKEN_LIMIT",
    "TOOL_RESULTS",
    "USER_MESSAGES",
    "CompactionThrashError",
    "CompactionThrashPolicy",
    "CondensationRecord",
    "ContextBudget",
    "ContextMeter",
    "DropPlan",
    "DroppedMessage",
    "FanoutBounds",
    "FanoutPlan",
    "PrefixIdentity",
    "TokenEstimator",
    "ToolOutputCap",
    "bound_fanout_output",
    "budget_from_config",
    "cap_tool_output",
    "classify_category",
    "compaction_thrash_policy_from_config",
    "digest_messages",
    "fanout_bounds_from_config",
    "plan_drop",
    "plan_fanout",
    "prefix_identity",
    "render_meter_line",
]
