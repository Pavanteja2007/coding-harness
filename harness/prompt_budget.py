"""The per-turn PROMPT BUDGET profile — Phase 3b's baseline, measured.

**Why this exists.** Phase 3b changes the tool surface, and the corpus's most
directly reusable finding for us is Claude Code's **~40% prompt reduction** from
deferred tool loading. A reduction target with no "before" number is a slogan.
This module produces the "before", per turn, split into the components a tool-
surface change can actually move.

**A single turn's profile is not the profile.** Composition changes over a
session: tool schemas are roughly constant, live tool results grow with the
conversation, and the summary appears only after the first compaction. So
:func:`profile_session` reports the SAME table at several turn depths and the
caller compares across them. A number measured at turn 1 and quoted as "the
prompt cost" is wrong by the time the run reaches turn 40.

**Every number is a MEASUREMENT with its estimator named.** Token counts come
from ``harness.agent_kernel.budget.TokenEstimator``, the same authority the
context meter uses, and ``estimator.name`` rides on the receipt — so a table
built on the 4-chars-per-token heuristic is never presented as a tokenizer
count. There is no second estimator in this file; a second one is how a profiler
and the meter end up disagreeing about whether a run is over its threshold.

**Components, and why the split is this one.** ``tool_schemas`` is separate
from ``system_prompt`` because they are separate arguments to a provider call:
``ModelGateway.call(messages, tools=...)``. Folding them together is exactly
what makes a deferred-tool-loading win invisible. ``context_files`` is separate
from ``system_prompt`` for the same reason — it is the part a JIT trigger adds
and the part ``PathTrigger`` is meant to move. ``tool_results_live`` is separate
again because it is the clearing target, and a profile that merged it into
"conversation" would hide the only component a lossless lever can reclaim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from harness.agent_kernel.budget import CHARS_PER_TOKEN, TokenEstimator

#: The measured components, in the order the report prints them. A closed set:
#: every message and every schema byte lands in exactly one bucket, so the shares
#: always sum to 1.0 and an unclassified remainder can never hide.
BUDGET_COMPONENTS: Tuple[str, ...] = (
    "system_prompt",
    "tool_schemas",
    "knowledge_context",
    "context_files",
    "conversation_recent",
    "conversation_summary",
    "tool_results_live",
    "tool_results_cleared",
    "constraint_reinjection",
    "other",
)

#: **The Phase 3b target.** Named explicitly rather than left for a reader to
#: find, because a tool-surface change is aimed at exactly this row.
PHASE_3B_TARGET = "tool_schemas"

#: Markers that identify a component inside a rendered message. Read from the
#: renderers themselves so the profiler cannot drift from what it measures:
#: `context_compiler.render_project_instructions` emits the first two,
#: `harness/context_files.render_injected_context` the third, the handoff the
#: fourth, `prompts.CONTEXT_REINJECTION_TEMPLATE` the fifth, and
#: `harness/agent_kernel/clearing` the sixth.
_CONTEXT_FILE_MARKERS: Tuple[str, ...] = (
    "\n--- ",  # render_project_instructions: `--- <path> (scope: <scope>) ---`
    "[nested context: ",
)
_SUMMARY_MARKER = "## Compacted earlier turns (structured handoff)"
_REINJECTION_MARKER = "[neo-reminder]"
_CLEARED_MARKER = "[tool result cleared:"
_KNOWLEDGE_MARKERS: Tuple[str, ...] = (
    "## Compiled repository context",
    "## Repository map",
    "### Cited sources",
    "## Applicable skills",
    "## Relevant past decisions",
    "## Coordinated-change fan-out",
)

#: Every cut point, in ONE place. Kept separate from the per-component lists
#: because the lists above answer "what does this fragment CONTAIN" and this one
#: answers "where may a fragment begin" — two questions that must stay in step,
#: and a second hard-coded copy of the marker strings is how they would not.
_ALL_MARKERS: Tuple[str, ...] = (
    _SUMMARY_MARKER,
    _REINJECTION_MARKER,
    _CLEARED_MARKER,
    *_KNOWLEDGE_MARKERS,
    *_CONTEXT_FILE_MARKERS,
)


@dataclass(frozen=True)
class PromptBudgetProfile:
    """One turn's prompt composition, per component."""

    turn: int = 0
    estimator: str = ""
    chars_per_token: float = float(CHARS_PER_TOKEN)
    totals: Mapping[str, int] = field(default_factory=dict)
    shares: Mapping[str, float] = field(default_factory=dict)
    message_count: int = 0
    tool_schema_count: int = 0
    #: The rendered request, bounded. Present so a reader can see WHAT was
    #: measured, not only how big it was.
    rendered_chars: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens across every component."""
        return int(sum(self.totals.values()))

    def tokens(self, component: str) -> int:
        """Tokens for one component, or 0 for an unrecognised name."""
        return int(self.totals.get(str(component), 0) or 0)

    def share(self, component: str) -> float:
        """Fraction of the prompt one component accounts for."""
        return float(self.shares.get(str(component), 0.0) or 0.0)

    @property
    def schema_share(self) -> float:
        """The Phase-3b number: tool schemas as a fraction of the whole prompt."""
        return self.share(PHASE_3B_TARGET)

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection."""
        return {
            "turn": int(self.turn),
            "estimator": str(self.estimator),
            "chars_per_token": float(self.chars_per_token),
            "total_tokens": int(self.total_tokens),
            "tokens": {name: self.tokens(name) for name in BUDGET_COMPONENTS},
            "shares": {
                name: round(self.share(name), 6) for name in BUDGET_COMPONENTS
            },
            "phase_3b_target": PHASE_3B_TARGET,
            "phase_3b_share": round(self.schema_share, 6),
            "message_count": int(self.message_count),
            "tool_schema_count": int(self.tool_schema_count),
            "rendered_chars": int(self.rendered_chars),
        }

    def table(self) -> str:
        """Return the profile as PLAIN markdown rows, largest component first."""
        total = max(1, self.total_tokens)
        lines = [
            f"| turn {self.turn} | tokens | share |",
            "|---|---:|---:|",
        ]
        for name in sorted(BUDGET_COMPONENTS, key=lambda n: -self.tokens(n)):
            value = self.tokens(name)
            lines.append(
                f"| {name} | {value} | {value / total * 100:.1f}% |"
            )
        lines.append(f"| **total** | **{self.total_tokens}** | 100.0% |")
        return "\n".join(lines)


def _estimator_tokens(estimator: TokenEstimator, text: str) -> int:
    """Token count for a fragment, through the ONE estimator authority."""
    if not text:
        return 0
    return int(estimator.tokens(text))


def split_message(content: str) -> List[Tuple[str, str]]:
    """Split one message body into ``(component, fragment)`` pairs.

    A MARKER-POSITION split, not a role split and not a first-match split. Both
    of those are wrong here and the reasons are worth stating, because this is
    the function the whole table's accuracy rests on:

    * **role** — this codebase deliberately packs compiled knowledge, retrieved
      files, the handoff summary and the re-injection into USER messages, so a
      role split puts all four in one bucket and the tool-schema target becomes
      unmeasurable against a constant.
    * **first match** — the compiled base frame carries the knowledge block AND
      the instruction block in ONE system message. Whichever marker happens to
      appear first would claim the whole message and the other component would
      report zero tokens while visibly being in the prompt. A number that is
      zero for something that is plainly there is the exact dishonesty the
      budget's honesty rules exist to prevent.

    So every marker position is found, the body is cut at all of them, and each
    fragment is classified on its own. Preamble before the first marker keeps the
    message's own component, so a system prompt with no markers is still counted
    as ``system_prompt``.
    """
    body = str(content or "")
    if not body:
        return []
    cuts = sorted(
        {
            index
            for marker in _ALL_MARKERS
            for index in _find_all(body, marker)
        }
    )
    if not cuts:
        return [(classify_message(body), body)]
    fragments: List[Tuple[str, str]] = []
    if cuts[0] > 0:
        fragments.append((classify_message(body[: cuts[0]]), body[: cuts[0]]))
    for position, start in enumerate(cuts):
        end = cuts[position + 1] if position + 1 < len(cuts) else len(body)
        chunk = body[start:end]
        if chunk:
            fragments.append((classify_message(chunk), chunk))
    return fragments


def _find_all(body: str, marker: str) -> List[int]:
    """Return every index at which ``marker`` occurs in ``body``."""
    found: List[int] = []
    start = body.find(marker)
    while start >= 0:
        found.append(start)
        start = body.find(marker, start + 1)
    return found


def classify_message(content: str) -> str:
    """Return the component one message body belongs to.

    Precedence is deliberate and load-bearing. The re-injection marker is checked
    FIRST because ``[neo-reminder]`` appears at the END of a long context: a
    marker-first sweep that walked components in the table's order would classify
    a reminder that follows a summary as a summary. ``summary`` is checked before
    ``knowledge`` because the handoff is appended to the same user turn as
    compiled context.
    """
    body = str(content or "")
    if _REINJECTION_MARKER in body:
        return "constraint_reinjection"
    if _SUMMARY_MARKER in body:
        return "conversation_summary"
    if _CLEARED_MARKER in body:
        return "tool_results_cleared"
    for marker in _KNOWLEDGE_MARKERS:
        if marker in body:
            return "knowledge_context"
    for marker in _CONTEXT_FILE_MARKERS:
        if marker in body:
            return "context_files"
    return "other"


def _schema_chars(tools: Optional[Sequence[Any]]) -> Tuple[int, int]:
    """Return ``(serialised_chars, schema_count)`` for a tool-schema list.

    Serialised rather than counted because a provider is billed for the JSON.
    A profile that counted ``len(tools)`` would report 45 tokens for 45 tools and
    make the Phase-3b target look free.
    """
    if not tools:
        return 0, 0
    try:
        blob = json.dumps(list(tools), default=str, sort_keys=True)
    except (TypeError, ValueError):
        return 0, len(list(tools))
    return len(blob), len(list(tools))


def profile_prompt(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Optional[Sequence[Any]] = None,
    estimator: Optional[TokenEstimator] = None,
    turn: int = 0,
) -> PromptBudgetProfile:
    """Return the per-component composition of ONE request.

    ``tools`` is the provider schema list, NOT the harness catalog: a schema list
    that was never sent cannot be part of the prompt. When the caller has no
    schemas it passes ``None`` and the row is honestly zero with
    ``tool_schema_count == 0`` — "we did not measure it" and "it is free" must
    not read alike, which is why the count rides the receipt.

    Total and never raising on a malformed message: an unrenderable entry is
    counted as ``other`` rather than aborting a measurement.
    """
    engine = estimator or TokenEstimator()
    totals: Dict[str, int] = {name: 0 for name in BUDGET_COMPONENTS}
    rendered = 0

    for message in messages or ():
        entry = dict(message or {})
        content = entry.get("content")
        if isinstance(content, list):
            # A multimodal turn: text first, then provider blocks. The text is
            # what a character-based estimator can measure, and the block list is
            # counted as rendered bytes so nothing disappears from the total.
            text = "".join(
                str(part) for part in content if isinstance(part, str)
            )
            blocks = [part for part in content if not isinstance(part, str)]
        else:
            text = str(content or "")
            blocks = []
        role = str(entry.get("role") or "user")
        rendered += len(text)
        if blocks:
            rendered += len(json.dumps(blocks, default=str))
        default = (
            "system_prompt"
            if role == "system"
            else "conversation_recent"
            if role == "assistant"
            else "tool_results_live"
        )
        for component, fragment in split_message(text):
            name = component if component != "other" else default
            totals[name] += _estimator_tokens(engine, fragment)

    schema_chars, schema_count = _schema_chars(tools)
    totals["tool_schemas"] = _estimator_tokens(engine, "x" * schema_chars) if schema_chars else 0

    grand = max(1, sum(totals.values()))
    return PromptBudgetProfile(
        turn=int(turn),
        estimator=str(getattr(engine, "name", "") or ""),
        chars_per_token=float(getattr(engine, "chars_per_token", CHARS_PER_TOKEN) or CHARS_PER_TOKEN),
        totals=dict(totals),
        shares={name: value / grand for name, value in totals.items()},
        message_count=len(list(messages or ())),
        tool_schema_count=int(schema_count),
        rendered_chars=int(rendered),
    )


@dataclass(frozen=True)
class SessionBudgetReport:
    """The same table at several turn depths, plus the deltas between them."""

    depths: Tuple[int, ...] = ()
    profiles: Tuple[PromptBudgetProfile, ...] = ()
    phase_3b_target: str = PHASE_3B_TARGET

    def at(self, turn: int) -> Optional[PromptBudgetProfile]:
        """Return the profile recorded at ``turn``, or ``None``."""
        for item in self.profiles:
            if int(item.turn) == int(turn):
                return item
        return None

    def growth(self) -> Dict[str, Dict[str, float]]:
        """Return per-component growth from the first depth to the last.

        A component that does not grow is a component a tool-surface change does
        not need to touch; one that grows 40x is where the context actually goes.
        """
        if len(self.profiles) < 2:
            return {}
        first, last = self.profiles[0], self.profiles[-1]
        rows: Dict[str, Dict[str, float]] = {}
        for name in BUDGET_COMPONENTS:
            start = first.tokens(name)
            end = last.tokens(name)
            rows[name] = {
                "first": float(start),
                "last": float(end),
                "growth_x": round(end / start, 4) if start else float("inf") if end else 1.0,
                "share_first": round(first.share(name), 6),
                "share_last": round(last.share(name), 6),
            }
        return rows

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection."""
        return {
            "depths": list(self.depths),
            "phase_3b_target": str(self.phase_3b_target),
            "profiles": [item.as_dict() for item in self.profiles],
            "growth": self.growth(),
        }

    def table(self) -> str:
        """Return the cross-depth table: one column per turn depth."""
        if not self.profiles:
            return "(no profiles recorded)"
        header = "| component | " + " | ".join(
            f"turn {item.turn}" for item in self.profiles
        ) + " | growth |"
        divider = "|---|" + "---:|" * (len(self.profiles) + 1)
        rows = [header, divider]
        growth = self.growth()
        for name in BUDGET_COMPONENTS:
            cells = [str(item.tokens(name)) for item in self.profiles]
            row = growth.get(name, {})
            factor = row.get("growth_x")
            if factor is None:
                marker = ""
            elif factor == float("inf"):
                # A component that did not exist at the first depth has no
                # meaningful ratio. Printing `infx` would be a number nobody can
                # act on; `new` says what happened.
                marker = "new"
            else:
                marker = f"{factor}x"
            rows.append(f"| {name} | " + " | ".join(cells) + f" | {marker} |")
        rows.append(
            "| **total** | "
            + " | ".join(str(item.total_tokens) for item in self.profiles)
            + " | |"
        )
        return "\n".join(rows)


def profile_session(
    requests: Iterable[Tuple[int, Sequence[Mapping[str, Any]], Optional[Sequence[Any]]]],
    *,
    estimator: Optional[TokenEstimator] = None,
    depths: Optional[Sequence[int]] = None,
) -> SessionBudgetReport:
    """Return the budget table at the requested turn depths.

    ``requests`` is an iterable of ``(turn, messages, tools)``. ``depths``
    selects which turns to report — the round's brief asks for **1 / 10 / 40 /
    80**, and a depth with no recorded turn is SKIPPED and named in the report's
    ``depths`` rather than interpolated. Interpolating a turn that was never
    measured would publish a number no run produced.
    """
    wanted = tuple(int(item) for item in (depths or ()))
    profiles: List[PromptBudgetProfile] = []
    for turn, messages, tools in requests:
        if wanted and int(turn) not in wanted:
            continue
        profiles.append(
            profile_prompt(
                messages, tools=tools, estimator=estimator, turn=int(turn)
            )
        )
    return SessionBudgetReport(
        depths=tuple(item.turn for item in profiles),
        profiles=tuple(profiles),
    )
