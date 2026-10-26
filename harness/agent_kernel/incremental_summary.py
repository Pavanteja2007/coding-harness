"""Anchored, INCREMENTAL compaction summaries — the fix for decision loss.

**The defect this module exists to close.** Before it, a compaction summarised
only the turns it was about to drop and never saw the previous summary
(``harness/agent_kernel/conversation.py::compact_tokens`` rendered
``render_dropped_transcript(droppable)`` and called
``summarize(transcript, turns)``). Independent summaries therefore accumulated,
the handoff rendered the last three, and **an early decision silently stopped
being in the request**. The corpus names this failure exactly: OpenCode "passes
the *previous* summary back in a ``<previous-summary>`` block and merged
('preserve still-true details, remove stale details') ... which preserves early
decisions that from-scratch re-summarization loses." Pi does the same through
iterative summary merging.

**Four mechanisms, and they are separable on purpose:**

1. :func:`render_previous_summary_block` produces the ``<previous-summary>``
   block. The summariser sees what it is merging into.
2. :data:`SECTION_HEADINGS` mandates five sections so the merge has a shape. A
   freeform summary has no stable merge key; a structured one does.
3. :func:`normalize_summary` does the merge **deterministically**, so correctness
   does not depend on the summariser model complying with an instruction. This
   is the load-bearing part: the prompt asks for the merge, the code performs
   it.
4. :func:`normalize_summary` also removes STALE detail, which is why the summary
   does not grow monotonically.

**Why the section structure is NOT system-prompt prose.** The AHEAD ablation
measured this repository's *agent* system prompt at **-2.3pp** for prose alone,
which is why every change to ``harness/prompts.py``'s agent prompts must land as
an eval arm. What lives here is a **summariser OUTPUT FORMAT** for a *different
request* — a maintenance call to a cheap tier that no agent ever reads as
instructions. Constraining a tool's output is not tuning the agent's voice, and
the distinction is recorded here because a future reader will otherwise
misread this file as prompt engineering and "improve" the prose. The prose
lives in :data:`MERGE_INSTRUCTION` and is rendered into the compaction request
only; :data:`SYSTEM_PROSE` in ``harness/prompts.py`` is the summariser's system
message for that maintenance call.

**Replacement, never union.** Every section takes the incoming value when the
incoming summary has one. That is the whole staleness rule: if a section is
unioned with the previous text, a detail the transcript has since contradicted
survives forever and the summary grows without bound. Carry-forward happens
ONLY for a section the incoming summary left absent or empty — which is a
missing key, not a contradiction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The five sections, in the order a handover summary must read. These are the
#: corpus-documented OpenCode section names; they are a MERGE KEY, not prose.
SECTION_HEADINGS: Tuple[str, ...] = (
    "Objective",
    "Important Details",
    "Work State",
    "Next Move",
    "Relevant Files",
)

#: Aliases accepted when PARSING a summary, so a summariser that writes
#: ``## Goal`` or ``**Objective**`` is still read into the canonical section.
#: Without this, a formatting variation reads as "no sections at all" and the
#: merge silently degrades to a concatenation.
_SECTION_ALIASES: Dict[str, str] = {
    "objective": "Objective",
    "goal": "Objective",
    "active task": "Objective",
    "task": "Objective",
    "important details": "Important Details",
    "details": "Important Details",
    "important information": "Important Details",
    "key details": "Important Details",
    "work state": "Work State",
    "state": "Work State",
    "current state": "Work State",
    "progress": "Work State",
    "next move": "Next Move",
    "next step": "Next Move",
    "next action": "Next Move",
    "open": "Next Move",
    "relevant files": "Relevant Files",
    "files": "Relevant Files",
    "files touched": "Relevant Files",
    "paths": "Relevant Files",
}

_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(?P<title>.+?)[ \t]*#*[ \t]*$")
_BOLD_HEADING_RE = re.compile(r"^[ \t]{0,3}\*\*(?P<title>[^*]{1,60})\*\*[ \t]*:?[ \t]*$")

#: A per-section ceiling. A section that exceeds it is truncated WITH A MARKER
#: rather than dropped, so a reader can tell a bounded section from an empty one.
DEFAULT_SECTION_MAX_CHARS = 1200

#: The whole-merged-summary ceiling. This is the anti-growth hard stop: no chain
#: of compactions can produce a summary past it, so a summary that would grow
#: monotonically is bounded rather than trusted.
DEFAULT_MAX_SUMMARY_CHARS = 6000

_TRUNCATION_MARKER = "\n...[section truncated]"


def render_previous_summary_block(previous: str, *, max_chars: int = 2000) -> str:
    """Return the ``<previous-summary>`` block, or ``""`` when there is none.

    The tag names are fixed and are what the merge instruction refers to. An
    empty previous summary renders as the empty string rather than as an empty
    tag pair, so a first compaction's request does not carry a block telling the
    model to merge with nothing.
    """
    text = str(previous or "").strip()
    if not text:
        return ""
    limit = max(400, int(max_chars))
    body = text if len(text) <= limit else text[: limit - 16] + "\n...[truncated]"
    return (
        "<previous-summary>\n"
        "The summary so far. Merge into it: keep every detail that is STILL "
        "TRUE, and REMOVE every detail the transcript below has contradicted or "
        "superseded. Do not accumulate - a stale detail that survives is worse "
        "than a missing one, because the agent cannot tell it is stale.\n"
        f"{body}\n"
        "</previous-summary>"
    )


#: The instruction the compaction request carries. Module-local prose rather than
#: ``harness/prompts.py``, because this is a CONTRACT for how to merge, not the
#: agent's voice; see the module docstring for why the two must not be confused.
MERGE_INSTRUCTION = (
    "Merge the previous summary with the new transcript into ONE summary.\n"
    "\n"
    "- Keep every detail from <previous-summary> that is STILL TRUE.\n"
    "- REMOVE any detail the transcript contradicts or supersedes. Removing a "
    "stale detail is the point of this call; a summary that only grows is a "
    "second bug.\n"
    "- Do not carry a detail forward just because it was there before.\n"
    "\n"
    "Output exactly these five sections, in this order, using these headings:\n"
    "\n"
    "## Objective\n"
    "## Important Details\n"
    "## Work State\n"
    "## Next Move\n"
    "## Relevant Files\n"
    "\n"
    "A section you have nothing new to say about may repeat what is still true "
    "from the previous summary. Do not add sections. Do not invent a detail that "
    "is in neither the previous summary nor the transcript."
)


@dataclass(frozen=True)
class SummarySections:
    """The five mandated sections, parsed out of a summary body."""

    values: Dict[str, str] = field(default_factory=dict)
    #: Headings seen in the text that are NOT one of the five, mapped to their
    #: content. Reported rather than silently dropped: a summariser that
    #: invented a sixth heading has told the reader something.
    extra: Dict[str, str] = field(default_factory=dict)
    #: ``True`` when the text carried at least one recognised heading, i.e. the
    #: parse found structure rather than falling back to "everything is prose".
    structured: bool = False

    def get(self, heading: str) -> str:
        """Return one section's text, or ``""`` when absent."""
        return str(self.values.get(heading) or "").strip()

    def present(self) -> Tuple[str, ...]:
        """Return the headings that carry non-empty text, in canonical order."""
        return tuple(name for name in SECTION_HEADINGS if self.get(name))

    def missing(self) -> Tuple[str, ...]:
        """Return the headings that are absent or empty, in canonical order."""
        return tuple(name for name in SECTION_HEADINGS if not self.get(name))


def parse_sections(text: str) -> SummarySections:
    """Parse a summary body into :data:`SECTION_HEADINGS`.

    Tolerant by design. A heading is a Markdown ATX heading, a bold-only line,
    or a bare recognised name on its own line — the three shapes a summariser
    actually produces. An unrecognised heading lands in
    :attr:`SummarySections.extra` and its body is kept, because dropping text a
    model wrote is how a summary loses a decision silently.

    A body with NO recognised heading parses to ``structured=False`` with
    everything in ``extra`` under the empty key, and
    :func:`normalize_summary` then treats it as one undifferentiated blob rather
    than pretending it had structure.
    """
    body = str(text or "")
    values: Dict[str, List[str]] = {}
    extra: Dict[str, List[str]] = {}
    current: Optional[str] = None
    saw_heading = False
    preamble: List[str] = []

    def flush() -> None:
        if current is None:
            return
        bucket = values if current in SECTION_HEADINGS else extra
        bucket.setdefault(current, []).append("")  # ensure the key exists

    for line in body.splitlines():
        title = _heading_title(line)
        canonical = _SECTION_ALIASES.get(title) if title is not None else None
        if canonical is not None:
            flush()
            current = canonical
            saw_heading = True
            continue
        if title is not None and current is not None:
            # An unrecognised heading inside a recognised structure: a new
            # bucket, kept so its text is never dropped.
            flush()
            current = title
            saw_heading = True
            continue
        if current is None:
            preamble.append(line)
            continue
        bucket = values if current in SECTION_HEADINGS else extra
        bucket.setdefault(current, []).append(line)

    flush()
    if not saw_heading:
        return SummarySections(
            values={}, extra={"": [line for line in preamble if line.strip()]},
            structured=False,
        )

    def _join(entries: Sequence[str]) -> str:
        return "\n".join(entries).strip()

    return SummarySections(
        values={key: _join(entries) for key, entries in values.items() if key},
        extra={key: _join(entries) for key, entries in extra.items() if key},
        structured=True,
    )


@dataclass(frozen=True)
class SummaryMergeReceipt:
    """What one merge did, so "it did not grow" is a measurement not a claim."""

    previous_chars: int = 0
    merged_chars: int = 0
    ceiling: int = 0
    #: Room the merge had left under :attr:`ceiling`. REPORTED rather than a
    #: separate knob: the ceiling is one absolute number, so the headroom is a
    #: derived fact and not a second rule that could disagree with the first.
    headroom_chars: int = 0
    #: Sections whose value came from the PREVIOUS summary because the incoming
    #: summary left them absent or empty. The mechanism by which an early
    #: decision survives a later compaction.
    carried: Tuple[str, ...] = ()
    #: Sections whose incoming value REPLACED a non-empty previous value. This is
    #: staleness removal, counted.
    replaced: Tuple[str, ...] = ()
    #: Sections bounded by :data:`DEFAULT_SECTION_MAX_CHARS`.
    truncated_sections: Tuple[str, ...] = ()
    #: Headings the incoming summary used that are not the five.
    extra_headings: Tuple[str, ...] = ()
    #: ``True`` when the incoming text carried no recognised heading, so the
    #: merge fell back to a single blob rather than five sections.
    unstructured: bool = False
    #: Text the incoming summary produced OUTSIDE any recognised section, kept
    #: rather than dropped. Non-empty means a section structure was violated and
    #: a reader needs to know.
    preamble: str = ""

    @property
    def grew(self) -> bool:
        """Whether the merged summary is strictly longer than its predecessor.

        This is the falsifiable half of "a summary does not grow monotonically":
        a reader can watch ``grew`` go ``False`` across a chain of compactions
        instead of taking the word for it.
        """
        return int(self.merged_chars) > int(self.previous_chars)

    @property
    def bounded(self) -> bool:
        """Whether the whole-summary ceiling bound this merge."""
        return int(self.merged_chars) >= int(self.ceiling)

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe receipt that rides the compaction record."""
        return {
            "previous_chars": int(self.previous_chars),
            "merged_chars": int(self.merged_chars),
            "ceiling": int(self.ceiling),
            "headroom_chars": int(self.headroom_chars),
            "carried": list(self.carried),
            "replaced": list(self.replaced),
            "truncated_sections": list(self.truncated_sections),
            "extra_headings": list(self.extra_headings),
            "unstructured": bool(self.unstructured),
            "preamble_chars": len(self.preamble or ""),
            "grew": bool(self.grew),
            "bounded": bool(self.bounded),
        }


def normalize_summary(
    incoming: str,
    previous: str = "",
    *,
    section_max_chars: int = DEFAULT_SECTION_MAX_CHARS,
    max_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> Tuple[str, SummaryMergeReceipt]:
    """Merge ``incoming`` into ``previous`` and return ``(text, receipt)``.

    The merge is performed HERE rather than trusted to the summariser, because
    the whole defect is that the summariser was never shown the previous summary
    and a model asked to do both jobs at once will sometimes accumulate. The
    rules, in order:

    * a section the incoming summary FILLED replaces the previous value outright
      (:attr:`SummaryMergeReceipt.replaced`) — this is how a contradicted detail
      is removed rather than unioned;
    * a section the incoming summary left ABSENT or EMPTY carries the previous
      value forward (:attr:`SummaryMergeReceipt.carried`) — this is how an early
      decision survives a later compaction;
    * a section with neither is rendered as an explicit ``(none)`` so a reader
      can tell "nothing happened here" from "this section was dropped";
    * every section is bounded, and the whole summary is bounded. The ceiling is
      what makes a non-monotonic summary a structural property rather than a
      hope.
    """
    ceiling = max(200, int(max_chars))
    section_cap = max(120, int(section_max_chars))
    prev_text = str(previous or "").strip()
    new_text = str(incoming or "").strip()

    parsed = parse_sections(new_text)
    prior = parse_sections(prev_text) if prev_text else SummarySections()
    carried: List[str] = []
    replaced: List[str] = []
    truncated: List[str] = []

    if not parsed.structured and not new_text:
        # Nothing came back. The previous summary is the whole answer, and the
        # receipt must say the incoming summary was empty rather than reporting
        # a merge that never happened.
        return _apply_ceiling(
            _render(prior) if prev_text else "",
            ceiling,
            prev_text,
            carried,
            replaced,
            truncated,
            parsed.extra,
            False,
            "",
            section_cap,
        )

    blocks: List[str] = []
    if not parsed.structured:
        # The incoming summary had no structure. Fall back to ONE blob: the
        # incoming text is the freshest thing anyone said, and it replaces the
        # previous body rather than joining it.
        body = new_text
        if prior.values or prior.extra:
            replaced.append("(unstructured)")
        blocks.append(body)
        unstructured = True
    else:
        unstructured = False
        for heading in SECTION_HEADINGS:
            value = parsed.get(heading)
            if value:
                blocks.append(f"## {heading}\n{value}")
                if prior.get(heading):
                    replaced.append(heading)
                continue
            fallback = prior.get(heading)
            if fallback:
                # A MISSING key, not a contradiction: carry it.
                blocks.append(f"## {heading}\n{fallback}")
                carried.append(heading)
                continue
            blocks.append(f"## {heading}\n(none)")
        for heading, value in sorted(parsed.extra.items()):
            if heading and value:
                blocks.append(f"## {heading}\n{value}")

    text = _apply_ceiling(
        "\n\n".join(blocks).strip(),
        ceiling,
        prev_text,
        carried,
        replaced,
        truncated,
        parsed.extra,
        unstructured,
        "",
        section_cap,
    )
    return text


def _render(sections: SummarySections) -> str:
    """Render a parsed summary back to canonical text."""
    if not sections.structured:
        return "\n\n".join(
            value for value in sections.extra.values() if value
        ).strip()
    blocks: List[str] = []
    for heading in SECTION_HEADINGS:
        blocks.append(f"## {heading}\n{sections.get(heading) or '(none)'}")
    return "\n\n".join(blocks).strip()


def _bound_section(value: str, limit: int) -> Tuple[str, bool]:
    """Bound one section, reporting whether the bound fired."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - len(_TRUNCATION_MARKER))] + _TRUNCATION_MARKER, True


def _apply_ceiling(
    text: str,
    ceiling: int,
    previous: str,
    carried: List[str],
    replaced: List[str],
    truncated: List[str],
    extra: Mapping[str, str],
    unstructured: bool,
    preamble: str,
    section_cap: int,
) -> Tuple[str, SummaryMergeReceipt]:
    """Bound the merged text and build its receipt.

    Split out from :func:`normalize_summary` so the empty-incoming path and the
    real path produce a receipt through ONE code path — a receipt built in two
    places is a receipt whose two arms can disagree about what happened.

    **The ceiling is the anti-growth mechanism, and it is deliberately a single
    absolute number.** An earlier draft computed a per-merge growth cap from the
    previous summary's length; that arithmetic was dead (``min(ceiling, ceiling
    + 0)``) while *reading* as a real bound, which is worse than no bound. The
    honest form is one absolute ceiling plus a REPORTED headroom, so a reader can
    see how much room a merge had left rather than inferring a rule that was not
    applied.
    """
    lines: List[str] = []
    for block in str(text or "").split("\n\n"):
        stripped = block.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            heading, _, body = stripped[3:].partition("\n")
            bounded, fired = _bound_section(body, section_cap)
            if fired:
                truncated.append(heading.strip())
            lines.append(f"## {heading.strip()}\n{bounded}")
            continue
        bounded, fired = _bound_section(stripped, section_cap)
        if fired:
            truncated.append("(preamble)")
        lines.append(bounded)
    merged = "\n\n".join(lines).strip()

    previous_chars = len(str(previous or "").strip())
    if len(merged) > ceiling:
        merged = (
            merged[: max(0, ceiling - len(_TRUNCATION_MARKER))] + _TRUNCATION_MARKER
        )
        if "(whole summary)" not in truncated:
            truncated.append("(whole summary)")

    receipt = SummaryMergeReceipt(
        previous_chars=previous_chars,
        merged_chars=len(merged),
        ceiling=ceiling,
        headroom_chars=max(0, ceiling - previous_chars),
        carried=tuple(carried),
        replaced=tuple(replaced),
        truncated_sections=tuple(truncated),
        extra_headings=tuple(sorted(key for key in extra if key)),
        unstructured=bool(unstructured),
        preamble=str(preamble or ""),
    )
    return merged, receipt


def _heading_title(line: str) -> Optional[str]:
    """Return a normalised heading title, or ``None`` when the line is not one."""
    text = str(line or "")
    match = _HEADING_RE.match(text) or _BOLD_HEADING_RE.match(text)
    if match is None:
        return None
    title = re.sub(r"[^a-z ]+", "", str(match.group("title") or "").strip().lower())
    return " ".join(title.split()) or None


def sections_report(text: str) -> Dict[str, Any]:
    """Return a structural report for one summary body.

    Used by tests and by the prompt-budget profiler: it answers "does this
    summary actually carry the five sections" without asserting the answer.
    """
    parsed = parse_sections(text)
    return {
        "structured": bool(parsed.structured),
        "present": list(parsed.present()),
        "missing": list(parsed.missing()),
        "extra_headings": sorted(key for key in parsed.extra if key),
        "chars": len(str(text or "")),
        "section_chars": {
            name: len(parsed.get(name)) for name in SECTION_HEADINGS
        },
    }
