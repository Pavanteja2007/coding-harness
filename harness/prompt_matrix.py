"""Model-family prompt matrix — FORMAT FACTS ONLY, and behavioural policy elsewhere.

This module is the answer to a trap
-----------------------------------
The corpus's systems ship nine base prompts dispatched by model-id substring,
each a different register, and the corpus records where the industry is going:
*"Codex's newest model prompts dropped the no-commit and anti-gold-plating
directives in favor of feature flags. Mistral Vibe deleted its 'Never Commit'
hard rule and re-founded its prompt on a seven-level precedence contract"* —
**"behavioural policy is moving from the prompt (where the model reads it) to
configuration (where the platform enforces it)."**

And this repository measured the cost of getting it wrong: **AHEAD ablated
system-prompt prose alone at −2.3pp.** Prose is not free.

So the matrix exists and is deliberately NARROW. It carries **format facts** —
facts about the wire protocol and about how a particular family parses a
response — and it cannot carry anything else, structurally:

* every fact is classified into :data:`FORMAT_FACT_KINDS`, a **closed
  taxonomy with no behavioural member**. A new behavioural instruction has
  nowhere to go. That is the whole enforcement mechanism: not a review
  checklist, an absence.
* every fact carries a **cited observation** — a required field naming the
  observed failure that justifies it. The corpus's own pattern is Hermes
  gating ~15 guidance blocks by family "with code comments citing observed
  per-model failures"; a block with no such citation is a preference, and a
  preference in a prompt is exactly what the −2.3pp measured.
* the text is assembled by a function that takes NO behavioural input. There is
  no parameter through which a behavioural directive could arrive.

What a format fact is, precisely
--------------------------------
A statement about **what the model must emit or how its output is parsed**, such
as: this family's tool calls arrive in a particular envelope; this family emits
a thinking block that must not be surfaced as content; this family needs a
cache marker in a particular dialect; this family calibrates verbosity
differently for the same task.

What it is NOT: what the agent should *do*. "Don't commit", "avoid gold
plating", "be concise", "read before you edit" — all behavioural, all
enforced elsewhere in this repository:

============================  ==========================================
behaviour                     where it is actually ENFORCED
============================  ==========================================
no commit                     the approval model; ``git_commit`` is
                              ``requires_approval``
no gold plating               :mod:`harness.tool_surface`'s
                              ``surface_violations`` refusing a surface
                              that cannot do the job
read before edit              ``expected_revision`` + ``stale_read`` —
                              a mutation of a file never read is REFUSED
protected paths               ``harness.editor.is_protected``, plus
                              Ceiling-R2-03's test-configuration guard
never claim done              the verifier gate; ``completed_verified`` is
                              minted at one place from evidence
============================  ==========================================

:mod:`harness.behaviour_policy` is the configuration layer those behaviours are
*declared* in, and it is where a user turns one on. This module never sees it.

Dispatch is on model METADATA, never a name string
--------------------------------------------------
The corpus's own systems dispatch on a substring of the model id. That is the
part of the pattern this repository has already measured as wrong:
``runtime.model_capabilities.effort_family_for``'s docstring records that an
``openai``-named gateway fronts Anthropic and Google models, so sending
``reasoning_effort`` to one is a 400 — and
``harness.tool_surface._family_for`` delegates to it for exactly this reason,
with an AST pin forbidding a ``gpt-``/``claude``/``gemini`` comparison in that
module.

This module resolves families through
:func:`runtime.model_capabilities.vision_family_for` — the SHAPE resolver, not
the EFFORT one. That distinction is load-bearing and was a real seam failure
T3 documented: ``effort_family_for`` deliberately withholds a family wherever
attaching that family's *effort* parameter might be a 400, so
``effort_family_for("gpt-4o-mini")`` returns ``""``. Asking the effort table
what wire SHAPE a model speaks inherits that withholding and concludes nothing
is known. A matrix dispatched on the effort table would silently apply no
format facts to this project's own ``easy`` tier.

The default is EMPTY
--------------------
:func:`format_prompt` returns ``""`` for a family with no facts, and the whole
matrix is opt-in behind ``prompt_format_facts``. So an unconfigured run's prompt
is byte-identical to its pre-Phase-3b self, the eval matrix has a stable
baseline, and **every prompt change this module can make is by definition an
eval arm** — which is the phase's hardest rule satisfied by construction rather
than by discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "CONFIG_KEY",
    "FORMAT_FACTS",
    "FORMAT_FACT_KINDS",
    "FormatFact",
    "MatrixReceipt",
    "format_prompt",
    "matrix_report",
    "resolve_family",
]


#: The closed taxonomy of format-fact categories. **There is no behavioural
#: member and adding one is the defect this taxonomy exists to prevent.**
#:
#: Each member is a question about the PROTOCOL or about PARSING:
#:
#: ``tool_call_syntax``
#:     The envelope this family wraps a tool call in, and how it terminates one.
#:     A fact about bytes on the wire.
#: ``thinking_block``
#:     Whether the family emits a reasoning/thinking block, where it sits, and
#:     that it must not be surfaced to the user as the answer. A fact about the
#:     response SHAPE — and note that conflating it with behaviour is how a
#:     summarizer ends up substituting hidden reasoning for a reply, which
#:     ``runtime.model_router`` already forbids ("the reasoning is never the
#:     answer").
#: ``reasoning_tag``
#:     The literal delimiters a family wraps reasoning in, when it uses literal
#:     text tags rather than a structured field. A fact about a PARSER.
#: ``cache_dialect``
#:     Which cache-control spelling and placement this endpoint accepts. A fact
#:     about the wire: an unknown key is a hard 400 on most gateways, which is
#:     why ``runtime.prompt_cache`` sends it to Anthropic-family endpoints only.
#: ``verbosity_calibration``
#:     How much prose this family emits for the same instruction. A fact about
#:     the model's PRIORS, discovered by measurement — not an opinion about how
#:     verbose an answer should be.
#: ``stop_sequencing``
#:     Whether a partial/streaming response can be acted on mid-stream or must
#:     be buffered. A fact about the transport.
#:
#: ``reasoning_tag`` is currently the one member with no registered fact, because
#: the only observation this repository has for it concerns families the
#: resolver cannot reach (see :data:`UNREACHABLE_OBSERVATIONS`). It is kept in
#: the taxonomy because it is a real category of wire-format fact and a future
#: reachable family will need it — unlike a *fact* declared for an unreachable
#: family, which is shipped prompt text no model can ever be shown.
FORMAT_FACT_KINDS: Tuple[str, ...] = (
    "tool_call_syntax",
    "thinking_block",
    "reasoning_tag",
    "cache_dialect",
    "verbosity_calibration",
    "stop_sequencing",
)

#: The opt-in key. **Absent or false means no format facts at all**, which is
#: what keeps the default prompt byte-identical and makes every use of this
#: matrix an eval arm rather than a shipped default.
CONFIG_KEY = "prompt_format_facts"


@dataclass(frozen=True)
class FormatFact:
    """One family-specific FORMAT fact, with the observation that justifies it.

    ``observed`` is REQUIRED and must be non-empty. A fact whose justification
    is "this model is better at X" is an opinion, and an opinion in a system
    prompt is precisely what AHEAD measured at −2.3pp. The field is required at
    construction so the question cannot be deferred to review.
    """

    fact_id: str
    kind: str
    families: Tuple[str, ...]
    text: str
    observed: str
    #: The harness mechanism that ALREADY handles this, if any. A format fact
    #: that duplicates an existing mechanism is documentation, not behaviour —
    #: and recording it here is how a reviewer notices the duplication.
    handled_by: str = ""

    def __post_init__(self) -> None:
        """Refuse a fact outside the taxonomy, or one with no citation."""
        if self.kind not in FORMAT_FACT_KINDS:
            raise ValueError(
                f"{self.fact_id!r}: kind {self.kind!r} is not a FORMAT fact kind "
                f"({list(FORMAT_FACT_KINDS)}). Behavioural policy belongs in "
                "harness.behaviour_policy and in the enforcement mechanisms, not "
                "in a prompt."
            )
        if not str(self.observed or "").strip():
            raise ValueError(
                f"{self.fact_id!r}: a family-specific block requires a cited "
                "observed failure. Without one it is a preference, and a "
                "preference in a prompt is a regression with extra steps."
            )
        if not str(self.text or "").strip():
            raise ValueError(f"{self.fact_id!r}: a format fact needs text")
        if not tuple(self.families or ()):
            raise ValueError(f"{self.fact_id!r}: a format fact needs a family")


def resolve_family(
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """Return the wire-format FAMILY for this model, or ``""``.

    Delegates to :func:`runtime.model_capabilities.vision_family_for`, which is
    the SHAPE resolver. See the module docstring for why the effort resolver is
    the wrong one here and what happens when it is used instead.

    The import is guarded and the failure mode is ``""`` — the same answer as an
    unrecognised family, because both mean "apply no format facts". That is the
    safe direction: an unknown family gets the historical prompt, and the
    receipt says the family did not resolve so a reader can tell an absent fact
    from an unresolved one.
    """
    try:
        from runtime.model_capabilities import vision_family_for
    except Exception:  # pragma: no cover - import guard
        return ""
    try:
        return str(vision_family_for(model, provider) or "")
    except Exception:
        return ""


#: The registry. **Eight facts, and every one names the observation that produced
#: it.** A fact with no observation cannot be constructed — see
#: :meth:`FormatFact.__post_init__` — so the discipline is enforced by the type
#: rather than by review.
FORMAT_FACTS: Tuple[FormatFact, ...] = (
    FormatFact(
        fact_id="anthropic_tool_call_envelope",
        kind="tool_call_syntax",
        families=("anthropic",),
        text=(
            "Emit each tool call as its own `tool_use` block in the assistant "
            "turn, with a `tool_result` block per call in the next user turn. "
            "Do not batch several calls into one block, and do not answer in "
            "prose and call a tool in the same turn."
        ),
        observed=(
            "`runtime.model_router` normalises provider tool calls into "
            "{content, tool_calls}; a family that emits a prose preamble "
            "alongside its call block yields `finish_reason` with content set "
            "AND tool_calls set, which the daily strategy's DAILY_DONE "
            "classifier reads as a finish and drops the calls."
        ),
        handled_by="runtime.model_router.parse_model_response",
    ),
    FormatFact(
        fact_id="openai_tool_call_envelope",
        kind="tool_call_syntax",
        families=("openai",),
        text=(
            "Emit tool calls through the function-call mechanism your runtime "
            "provides. Do not write a tool call as XML, as a fenced code block, "
            "or as prose describing what you would call."
        ),
        observed=(
            "`harness/agent_loop.py`'s `_PLAIN_PATTERNS` prose-fallback "
            "grammar exists precisely because models emit `{\"tool\": ...}` as "
            "text rather than as a native call; the fallback is a compatibility "
            "shim for a wire mismatch, and every call routed through it is a "
            "parse the provider's own mechanism should have carried."
        ),
        handled_by="harness.agent_loop.parse_tool_call (fallback only)",
    ),
    FormatFact(
        fact_id="anthropic_thinking_block",
        kind="thinking_block",
        families=("anthropic",),
        text=(
            "A thinking block is reasoning, not your answer. Never present its "
            "contents to the user as a result, and never treat an empty "
            "content with a non-empty thinking block as a finished answer."
        ),
        observed=(
            "`runtime.model_router` records this as "
            "`empty_response_reasoning_only` against a `400`-token "
            "`max_completion_tokens` probe on this project's own ablation "
            "endpoint: the whole budget was consumed as hidden reasoning and "
            "zero visible tokens came back. The receipt exists because the "
            "failure is indistinguishable from an empty reply."
        ),
        handled_by="runtime.model_router._extract_reasoning",
    ),
    FormatFact(
        fact_id="anthropic_cache_dialect",
        kind="cache_dialect",
        families=("anthropic",),
        text=(
            "The cached prefix is the leading system message plus the tool "
            "schemas. Do not restate or reorder them between turns; a changed "
            "prefix is a cache miss."
        ),
        observed=(
            "`runtime.prompt_cache.frozen_prefix` is deliberately ONE system "
            "message: over-including a volatile system message silently "
            "destroys every future cache hit while looking healthy in the "
            "receipts, and Ceiling-09 measured the under-included cost as a "
            "few tokens. The measured direction is why the prefix is narrow."
        ),
        handled_by="runtime.prompt_cache.frozen_prefix",
    ),
    FormatFact(
        fact_id="openai_no_cache_control",
        kind="cache_dialect",
        families=("openai", "google"),
        text=(
            "Send no cache-control parameter. If your endpoint rejects an "
            "unknown key, the whole call fails."
        ),
        observed=(
            "`runtime.prompt_cache` sends `cache_control` to Anthropic-family "
            "endpoints ONLY, because an unknown key is a hard 400 on most "
            "OpenAI-compatible gateways. The prefix is still digested for "
            "OpenAI-compatible and Google endpoints so their receipts work."
        ),
        handled_by="runtime.prompt_cache.plan_cache",
    ),
)


#: Observations that are real, cited, and **deliberately not registered as
#: prompt facts** because the families they describe are unreachable.
#:
#: The first draft of this module registered three facts for ``qwen``,
#: ``deepseek`` and ``glm`` — the free-tier proxy families this project
#: actually routes to — and they were DEAD on arrival. Measured against every
#: model/provider combination this repository can name,
#: :func:`runtime.model_capabilities.vision_family_for` produces exactly three
#: families: ``openai``, ``anthropic``, ``google``. ``qwen3.8-27b`` resolves to
#: ``openai``, because its provider is an OpenAI-compatible gateway and the
#: model name carries no shape hint.
#:
#: A prompt block no model can ever be selected for is a comment with a string
#: in it. This repository's own rule is explicit — "a status nothing can produce
#: is untested vocabulary" — and a fact nobody can reach is the same defect: it
#: looks like coverage in the registry table and delivers none.
#:
#: So the observations are kept HERE, as data with the same citations, and the
#: prompt blocks are not shipped. What would make them reachable is a shape
#: resolver that knows these families, which is :mod:`runtime`'s file and T3's
#: to change. Until then, a reviewer reading this list learns something true;
#: a reviewer reading three dead rows in ``FORMAT_FACTS`` learns something
#: false.
#:
#: Each entry names the observation AND the condition under which it would become
#: a fact, so the next round can promote one by adding a family the resolver can
#: produce — not by adding a row.
UNREACHABLE_OBSERVATIONS: Tuple[Dict[str, Any], ...] = (
    {
        "candidate_id": "reasoning_tag_is_not_content",
        "kind": "reasoning_tag",
        "families": ("qwen", "deepseek", "glm"),
        "observed": (
            "`harness/agent_loop.py`'s degenerate-fetch salvage records a real "
            "reply that GLUED a <think> block onto its FETCH line; the URL "
            "parser saw control characters and the whole research run errored. "
            "A glued delimiter is not a parse edge case, it is a whole lost run."
        ),
        "reachable_when": (
            "runtime.model_capabilities.vision_family_for resolves one of these "
            "families from model metadata (a declared capability, not a name "
            "substring)"
        ),
    },
    {
        "candidate_id": "verbosity_of_small_families",
        "kind": "verbosity_calibration",
        "families": ("qwen", "deepseek", "glm"),
        "observed": (
            "Measured on this project's own ablation transcripts: the free-tier "
            "proxy families this names returned multi-paragraph preambles and "
            "recaps around a one-line command result, and the recap lands at "
            "the END of the assistant turn where "
            "harness.prompts.render_context_reinjection appends the constraint "
            "block - so the reinforcement is no longer the last thing the "
            "model reads."
        ),
        "reachable_when": (
            "same; note this one is the most valuable of the three, because the "
            "defect it describes is a PROMPT-ORDERING defect this repository "
            "can fix directly rather than by telling a model to be brief"
        ),
    },
    {
        "candidate_id": "no_partial_tool_call_stream",
        "kind": "stop_sequencing",
        "families": ("qwen", "deepseek"),
        "observed": (
            "harness/agent_kernel/tools.py's dedupe_tool_calls/note_event drop "
            "retried and duplicated provider events by event id, and "
            "_infer_stop_reason records incomplete_truncated - both exist "
            "because a truncated stream has been observed arriving as a "
            "half-written call that would otherwise dispatch."
        ),
        "reachable_when": "same",
    },
)


@dataclass(frozen=True)
class MatrixReceipt:
    """What the matrix did, so a reader can tell silence from absence."""

    text: str = ""
    family: str = ""
    model: str = ""
    provider: str = ""
    fact_ids: Tuple[str, ...] = ()
    kinds: Tuple[str, ...] = ()
    observations: Tuple[str, ...] = ()
    enabled: bool = False
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe projection."""
        return {
            "enabled": bool(self.enabled),
            "family": self.family,
            "model": self.model,
            "provider": self.provider,
            "family_resolved": bool(self.resolved),
            "fact_ids": list(self.fact_ids),
            "kinds": list(self.kinds),
            "chars": len(self.text),
            "citations": len(self.observations),
        }


def _enabled(config: Optional[Mapping[str, Any]]) -> bool:
    """Read the opt-in by key PRESENCE plus the shared truthiness rule.

    ``harness.tool_surface.truthiness_of`` is reused rather than reimplemented:
    this repository has already shipped two spellings of "is this config value
    on", and a third would be a third answer to the same question.
    """
    if not config or CONFIG_KEY not in config:
        return False
    from harness.tool_surface import truthiness_of

    return bool(truthiness_of(config.get(CONFIG_KEY)))


def facts_for(family: str) -> Tuple[FormatFact, ...]:
    """Return the facts declared for ``family``, in registry order."""
    wanted = str(family or "").strip().lower()
    if not wanted:
        return ()
    return tuple(fact for fact in FORMAT_FACTS if wanted in fact.families)


def format_prompt(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    facts: Optional[Iterable[FormatFact]] = None,
) -> MatrixReceipt:
    """Return this model's FORMAT facts, or an empty receipt.

    **The default is empty and the signature has no behavioural parameter.**
    There is no argument through which a behavioural directive could reach this
    function, which is the enforcement the module docstring promises. Behaviour
    is declared in :mod:`harness.behaviour_policy` and enforced by the approval
    model, the stale-read gate, the protected-path guard and the verifier — none
    of which is this function's business.
    """
    family = resolve_family(model, provider)
    enabled = _enabled(config)
    if not enabled:
        return MatrixReceipt(
            family=family,
            model=str(model or ""),
            provider=str(provider or ""),
            enabled=False,
            resolved=bool(family),
        )
    chosen = (
        tuple(facts)
        if facts is not None
        else facts_for(family)
    )
    if not chosen:
        return MatrixReceipt(
            family=family,
            model=str(model or ""),
            provider=str(provider or ""),
            enabled=True,
            resolved=bool(family),
        )
    lines = [
        "[wire format for this model family]",
        *(
            f"- {fact.text}"
            for fact in chosen
        ),
    ]
    text = "\n".join(lines)
    return MatrixReceipt(
        text=text,
        family=family,
        model=str(model or ""),
        provider=str(provider or ""),
        fact_ids=tuple(fact.fact_id for fact in chosen),
        kinds=tuple(fact.kind for fact in chosen),
        observations=tuple(fact.observed for fact in chosen),
        enabled=True,
        resolved=bool(family),
    )


def matrix_report() -> Dict[str, Any]:
    """Publish every registered fact with its citation, for review.

    A matrix nobody can read is a matrix nobody can check, and the reviewable
    artefact is the table — not the rendered prompt, which is per-family and
    assembled at call time.
    """
    reachable = resolve_all_families()
    declared = sorted({family for fact in FORMAT_FACTS for family in fact.families})
    return {
        "config_key": CONFIG_KEY,
        "kinds": list(FORMAT_FACT_KINDS),
        "families": declared,
        "families_resolvable": reachable,
        "unreachable_families": sorted(set(declared) - set(reachable)),
        "count": len(FORMAT_FACTS),
        "facts": [
            {
                "fact_id": fact.fact_id,
                "kind": fact.kind,
                "families": list(fact.families),
                "observed": fact.observed,
                "handled_by": fact.handled_by,
                "chars": len(fact.text),
            }
            for fact in FORMAT_FACTS
        ],
        "unreachable_observations": [dict(row) for row in UNREACHABLE_OBSERVATIONS],
        "note": (
            "every row carries a cited observation and a kind from the closed "
            "FORMAT taxonomy; there is no behavioural member, so a behavioural "
            "instruction has nowhere to be registered in this module. "
            "unreachable_observations holds cited observations that are NOT "
            "shipped as prompt facts because their families cannot currently "
            "be resolved - see its docstring."
        ),
    }


def resolve_all_families() -> Tuple[str, ...]:
    """Return every family the metadata resolver can actually produce.

    Probed over a fixed model/provider grid rather than read from a table,
    because the whole question is what the resolver DOES and a table of what it
    ought to do cannot answer it. :func:`unreachable_families` turns the answer
    into a pin.
    """
    probe_models = (
        "gpt-4o-mini", "gpt-4o", "gpt-5",
        "claude-3-5-sonnet-20241022", "claude-sonnet-4",
        "gemini-2.0-flash", "gemini-1.5-pro",
        "qwen3.8-27b", "deepseek-chat", "z-ai/glm-5.3-free",
        "stepfun-3.7-flash", "llama-3.3-70b", "mistral-large",
    )
    probe_providers = (None, "openai", "anthropic", "google", "tokenrouter")
    found = {
        resolve_family(model, provider)
        for model in probe_models
        for provider in probe_providers
    }
    return tuple(sorted(item for item in found if item))


def unreachable_families() -> Tuple[str, ...]:
    """Return declared families the resolver cannot produce. **Must be empty.**

    This is the function that would have caught the three dead facts, and it is
    a pin rather than a comment: a fact declared for a family nothing can
    resolve is a prompt block no model will ever be shown, which is untested
    vocabulary wearing a citation.
    """
    reachable = set(resolve_all_families())
    return tuple(
        sorted(
            {
                family
                for fact in FORMAT_FACTS
                for family in fact.families
                if family not in reachable
            }
        )
    )
