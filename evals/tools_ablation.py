"""The tools ablation: does deferring tool schemas change anything?

WHAT THIS MEASURES, AND WHAT IT CANNOT
--------------------------------------
Phase 3b's target is prompt-budget control, and the corpus's number for it is
**+3.3pp for tools** - another stack's measurement of another system. This
module produces OURS, or it reports that ours is not measurable here. It does
not do both.

THE VACUITY TRAP, AND IT IS REAL IN THIS TREE
----------------------------------------------
``evals/run.py`` pins ``use_mock_provider: True``. That makes an effort or
capability arm vacuous, because the mock compares two identical configs and
publishes a "no delta". The question this module had to answer BEFORE running
anything was whether the same trap applies to DEFERRED LOADING, and the answer
is: **it applies to the success axis and not to the request axis.** Both halves
were established mechanically, not by reading:

* **The request axis is REAL.** The schema list is genuinely constructed per
  turn by :meth:`harness.agent_kernel.strategy.DailyCodingStrategy._schemas_for_model`
  and threaded to the Boundary-2 call. Shrinking it shrinks the bytes on the
  wire. This is measured from the SERIALISED schema list for the reason T1's
  baseline states: a provider is billed for the JSON, and a profile counting
  ``len(tools)`` would report 45 tokens for 45 tools and make the target look
  free.
* **The success axis is VACUOUS under the mock provider.** The scripted
  responder's signature is ``fn(messages: list, model: str) -> str`` - it has
  no ``tools`` parameter, and :func:`runtime.mock_provider.synthesize` takes
  only ``(model, messages)``. The schema list is therefore **dropped at the
  model boundary**: a task's outcome cannot move because the deferred arm's
  model never sees the difference. Reporting a success delta from this driver
  would be a fabricated number, so the driver refuses to compute one and says
  ``success_axis_vacuous: true`` on every run.
* **The provider-reported token axis is ALSO vacuous here.** Every scripted
  call reports a fixed token count regardless of its input - the same defect
  ``docs/p3a-g3a.md`` records for the memory ablation, which published
  ``cost_axis_vacuous: true`` rather than quoting a ``$0.00`` delta.

SO THIS DRIVER USES A SCRIPTED-DETERMINISTIC, SCHEMA-AWARE MODEL
---------------------------------------------------------------
The brief's instruction for exactly this case is "use a real or scripted-
deterministic model and say which". This is the scripted-deterministic option,
and the "schema-aware" part is the whole design: :class:`SchemaAwareModel` has
a signature that NAMES ``tools`` and whose behaviour genuinely depends on
whether the schema it needs is present. It is still a script - it opens no
socket, reads no credential, and is bit-identical run to run - so the arm is
deterministic and free, and unlike the shipped mock it can be *hurt* by
deferral.

That is what makes the metric the brief calls out as the one that could turn
this into a regression measurable at all: the **discoverability failure rate**,
how often the model failed to use a tool it could have discovered.

THE SINGLE-VARIABLE CHECK IS MECHANICAL
---------------------------------------
Two arms, differing in exactly one variable, and the check is a DIFF of the
decisions rather than a claim about them. :func:`single_variable_report`
compares the two configs key by key and reports every differing key, then
refuses to call the experiment valid if the diff is anything other than the
deferral key. An ablation whose arms differ in two variables reports both
differences and ``single_variable: false`` - it does not quietly attribute a
delta to the one the author meant.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - path boot
    sys.path.insert(0, str(REPO_ROOT))

from integrations.tool_deferral import (  # noqa: E402
    DeferralDecision,
    DeferralPolicy,
    DeferralReceipt,
    deferral_safety_report,
    resolve_selection,
    selection_from_config,
)
from integrations.tool_search import SearchCandidate  # noqa: E402

__all__ = [
    "ARMS",
    "VACUITY",
    "VACUITY_PROOF",
    "ArmResult",
    "SchemaAwareModel",
    "arms_differ_in",
    "measure_arm",
    "run_matrix",
    "run_repeated",
    "single_variable_report",
    "vacuity_of_the_go_no_go_axis",
    "variance",
]


#: The ONE variable. Every other key is identical in both arms, and
#: :func:`single_variable_report` proves it by diffing rather than asserting.
THE_ONE_VARIABLE = "tool_deferral_enabled"

#: The two arms. A config fragment each, exactly as it would be merged into
#: ``Task.config``, so the single-variable check runs on the SAME dicts the
#: measurement uses rather than on a copy that could drift.
ARMS: Dict[str, Dict[str, Any]] = {
    "eager": {"tool_deferral_enabled": False},
    "deferred": {"tool_deferral_enabled": True},
}


#: WHY the shipped mock cannot exercise this, recorded as data so the claim is
#: inspectable and a future change to the mock is caught.
VACUITY_PROOF: Dict[str, Any] = {
    "shipped_mock_responder_signature": "fn(messages: list, model: str) -> str",
    "shipped_mock_synthesize_signature": "synthesize(model: str, messages: list)",
    "mock_mentions_tools": False,
    "grep_evidence": (
        "runtime/mock_provider.py contains zero occurrences of the token "
        "`tools`; evals/run.py contains zero as well"
    ),
    "consequence": (
        "the schema list is dropped at the model boundary, so a task outcome "
        "cannot differ between the arms under use_mock_provider=True"
    ),
    "provider_token_axis": (
        "every scripted call reports a fixed token count, so a provider-reported "
        "token or cost delta is a measurement of the mock's constant"
    ),
    "resolution": (
        "this driver uses SchemaAwareModel, whose signature NAMES tools and "
        "whose behaviour depends on the schemas it is given"
    ),
    "verified_by": ("python -m evals.tools_ablation --vacuity  (re-derives all of it)"),
}


@dataclass
class ArmResult:
    """One arm's measurement.

    ``metrics`` holds only things that were MEASURED. An unmeasurable axis is a
    key whose value is ``None`` plus a sibling ``*_vacuous: true`` flag, never a
    zero and never a fabricated delta - the rule ``docs/p3a-g3a.md`` states and
    this driver follows.
    """

    arm: str
    config: Dict[str, Any]
    metrics: Dict[str, Any] = field(default_factory=dict)
    decision: Optional[DeferralDecision] = None
    receipt: Optional[DeferralReceipt] = None
    safety: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "config": dict(self.config),
            "metrics": dict(self.metrics),
            "decision": self.decision.to_dict() if self.decision else None,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "safety": dict(self.safety),
            "events": list(self.events),
        }


class SchemaAwareModel:
    """A scripted-deterministic model whose signature NAMES ``tools``.

    The whole point is the parameter. The shipped mock's responder is
    ``fn(messages, model)``, so the schema list cannot reach it; this one is
    ``fn(messages, model, tools=None)`` and reads it. It then behaves the way a
    real tool-using model behaves, which gives the ablation something to
    measure:

    * **it uses a tool whose schema is present**, and reports the call;
    * **it does NOT invent a call for a tool whose schema is absent** - a real
      model cannot call a tool it was never shown, and a script that pretended
      otherwise would make the eager arm look worse for the wrong reason;
    * **when the schema is absent it searches**, and records the search. Whether
      the search SUCCEEDS is the discoverability measurement.

    Deterministic by construction: no clock, no randomness, no I/O. Every field
    it reports is derived from its arguments, so two runs are bit-identical.

    :param needed: the tool names this model wants to call, in priority order.
    :param discoverable: which of those it would find by SEARCHING. The gap
        between this and ``needed`` is what makes a deferred tool that search
        cannot reach a measurable failure rather than a silent one.
    """

    def __init__(
        self,
        *,
        needed: Sequence[str],
        discoverable: Optional[Sequence[str]] = None,
        search_limit: int = 10,
    ) -> None:
        """Record which tools this model wants, and which it can find."""
        self.needed = [str(n) for n in needed]
        self.discoverable = (
            [str(n) for n in discoverable]
            if discoverable is not None
            else list(self.needed)
        )
        self.search_limit = int(search_limit)
        self.calls: List[Dict[str, Any]] = []
        self.schemas_seen: List[str] = []
        self.searches: List[Dict[str, Any]] = []

    def __call__(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str = "",
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        **kwargs: Any,
    ) -> str:
        """Return one scripted reply, recording what it could see.

        ``tools`` is accepted by NAME here, which is what makes this a valid
        receiver of the real Boundary-2 call. A model that cannot see the
        schemas cannot be affected by their absence, and no success delta it
        reports would mean anything.

        IT ISSUES ONE SEARCH PER TOOL IT IS MISSING, NOT ONE SEARCH TOTAL
        ------------------------------------------------------------------
        The first version emitted a single ``select:<needed[0]>`` query, so a
        model that wanted nine deferred tools could recover exactly one of them.
        Every "capability intact when the model searches" number was therefore
        ``1/n`` - it measured this script's one-line simplification rather than
        the discovery mechanism, and it made deferral look far more damaging
        than it is.

        A model that needs N tools and is offered K of them asks for the N-K it
        is missing, one query each, because that is what a per-tool lookup is.
        Each query is recorded so :func:`measure_arm` can resolve it
        independently and attribute reachability per tool. The number of
        searches is a COUNT and is never converted into a token or money saving:
        on a real provider N-K extra round trips are latency and money exactly
        like the schema tokens are.
        """
        names = [
            str(
                (entry.get("function") or {}).get("name")
                if isinstance(entry, Mapping)
                else getattr(entry, "name", "")
            )
            for entry in (tools or ())
        ]
        names = [n for n in names if n]
        self.schemas_seen = names
        missing = [n for n in self.needed if n not in names]
        if not missing:
            # Everything needed was offered outright; no discovery needed.
            for name in self.needed:
                self.calls.append({"tool": name, "via": "schema_present"})
            return f'{{"tool": "{self.needed[0] if self.needed else ""}"}}'
        if self.discoverable:
            for name in missing:
                self.searches.append(
                    {
                        "reason": "needed tool was not offered",
                        "searched_for": name,
                        "query": f"select:{name}",
                    }
                )
            return (
                '{"tool": "search_tools", "queries": ['
                + ", ".join(f'"select:{n}"' for n in missing)
                + "]}"
            )
        self.calls.append({"tool": "", "via": "nothing_offered_and_nothing_searchable"})
        return "I have no tool available for this task."

    def report(self) -> Dict[str, Any]:
        """What this model observed, as data."""
        return {
            "schemas_seen": list(self.schemas_seen),
            "schemas_seen_count": len(self.schemas_seen),
            "calls": list(self.calls),
            "searches": list(self.searches),
        }


VACUITY: Dict[str, Any] = {
    "success_axis_vacuous_under_shipped_mock": True,
    "provider_token_axis_vacuous": True,
    "request_axis_vacuous": False,
    "model_used": "evals.tools_ablation.SchemaAwareModel",
    "model_kind": "scripted-deterministic",
    "live_provider_used": False,
    "credential_read": False,
}


# --------------------------------------------------------------------------
# the vacuity check, re-derived rather than asserted
# --------------------------------------------------------------------------


def verify_vacuity() -> Dict[str, Any]:
    """Re-derive every vacuity claim from the source, on this run.

    A vacuity claim recorded as prose is a claim, and this repository's whole
    reason for being is that claims should be measurements. So this reads the
    two modules that decide the question and reports what it found:

    * ``runtime/mock_provider.py`` and ``evals/run.py`` - do they mention
      ``tools`` at all? A mock that never sees the schemas cannot be moved by
      their absence.
    * the live signatures - does the responder accept ``tools``, and does
      ``synthesize`` forward it?

    Returns a report whose ``agrees_with_record`` key compares what it found
    against :data:`VACUITY_PROOF`. A disagreement is reported rather than
    hidden, because it means one of the two is stale and a reader needs to know
    which.
    """
    import ast
    import inspect

    observed: Dict[str, Any] = {}
    for rel in ("runtime/mock_provider.py", "evals/run.py"):
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            observed[rel] = {"readable": False, "reason": str(exc)}
            continue
        observed[rel] = {
            "readable": True,
            "mentions_tools": "tools" in text,
            "occurrences": text.count("tools"),
        }

    signatures: Dict[str, str] = {}
    try:
        from runtime import mock_provider

        signatures["synthesize"] = str(inspect.signature(mock_provider.synthesize))
    except Exception as exc:  # pragma: no cover - introspection guard
        signatures["synthesize"] = f"{type(exc).__name__}: {exc}"
    # The dynamic responder is a CLOSURE, so `inspect.signature` cannot see it.
    # Its source is parsed instead - and by AST rather than by regex, because
    # the question is "does this function's SIGNATURE name `tools`", and a
    # substring search over the file cannot tell a parameter from a mention in
    # the body. That distinction is the entire finding.
    try:
        source = (REPO_ROOT / "runtime" / "mock_provider.py").read_text(
            encoding="utf-8", errors="replace"
        )
        tree = ast.parse(source)
        found: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            names = [a.arg for a in node.args.args]
            found.append(f"{node.name}({', '.join(names)})")
        signatures["all_function_signatures"] = found
        responder_accepts = any(
            name.startswith("fn(") and "tools" in name for name in found
        )
    except Exception as exc:  # pragma: no cover - parse guard
        signatures["ast_error"] = f"{type(exc).__name__}: {exc}"
        responder_accepts = None

    mock_has_tools_param = responder_accepts is True
    mock_sees_tools = any(
        isinstance(v, dict) and v.get("mentions_tools") for v in observed.values()
    )
    findings = {
        "mock_provider_reads_tools": mock_sees_tools,
        "mock_responder_accepts_tools": mock_has_tools_param,
        "success_axis_vacuous_under_shipped_mock": (not mock_has_tools_param)
        if responder_accepts is not None
        else None,
        "observed_signatures": signatures,
        "module_scan": observed,
    }
    recorded = {
        "mock_provider_reads_tools": VACUITY_PROOF["mock_mentions_tools"],
        "mock_responder_accepts_tools": False,
        "success_axis_vacuous_under_shipped_mock": True,
    }
    findings["agrees_with_record"] = all(
        findings.get(key) == value for key, value in recorded.items()
    )
    findings["recorded"] = recorded
    findings["consequence"] = (
        "if `success_axis_vacuous_under_shipped_mock` is true, no success delta "
        "from the shipped mock is attributable to deferral, and this driver "
        "uses SchemaAwareModel instead"
    )
    return findings


# --------------------------------------------------------------------------
# the arms
# --------------------------------------------------------------------------


def arms_differ_in(arms: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Diff two arm configs key by key. A claim about a diff, not the diff.

    :func:`single_variable_report` is the policy half; this is the measurement
    half. Returns every key whose value differs, sorted, so "these arms differ in
    exactly one variable" is a reader's verification rather than the driver's
    assertion.
    """
    names = sorted(arms)
    if len(names) != 2:
        return {"comparable": False, "reason": f"expected 2 arms, got {len(names)}"}
    left, right = arms[names[0]], arms[names[1]]
    keys = sorted(set(left) | set(right))
    differing = [
        key for key in keys if left.get(key, "<absent>") != right.get(key, "<absent>")
    ]
    return {
        "comparable": True,
        "arms": names,
        "keys_examined": keys,
        "differing_keys": differing,
        "differing_key_count": len(differing),
        "per_key": {
            key: {
                names[0]: left.get(key, "<absent>"),
                names[1]: right.get(key, "<absent>"),
            }
            for key in differing
        },
    }


def single_variable_report(arms: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Report whether the arms differ in exactly the intended variable.

    Returns ``single_variable: True`` only when the diff is EXACTLY
    :data:`THE_ONE_VARIABLE`. Anything else - two keys, zero keys, an arm that
    is a superset of another - reports ``False`` with the diff attached, and the
    report's ``attributable`` is ``False`` as a consequence.

    Zero differing keys is reported as a specific failure rather than as a pass,
    because two identical arms are the exact trap this phase's brief names: they
    would produce a clean-looking delta of 0.0 and publish a vacuous result.
    """
    diff = arms_differ_in(arms)
    if not diff.get("comparable"):
        return {**diff, "single_variable": False, "attributable": False}
    differing = diff["differing_keys"]
    exactly = differing == [THE_ONE_VARIABLE]
    if not differing:
        verdict = "IDENTICAL_ARMS"
        detail = (
            "the two arms are byte-identical, so any delta between them is "
            "zero by construction and publishing it would be the vacuous "
            "'no delta' this driver exists to prevent"
        )
    elif exactly:
        verdict = "SINGLE_VARIABLE"
        detail = (
            f"the arms differ in exactly {THE_ONE_VARIABLE!r}, so a delta is "
            "attributable to deferral and to nothing else"
        )
    else:
        verdict = "MULTI_VARIABLE"
        detail = (
            f"the arms differ in {differing}, which is more than the intended "
            f"{THE_ONE_VARIABLE!r}; any delta is NOT attributable to deferral"
        )
    return {
        **diff,
        "intended_variable": THE_ONE_VARIABLE,
        "single_variable": exactly,
        "attributable": exactly,
        "verdict": verdict,
        "detail": detail,
    }


def _schema_tokens(schemas: Sequence[Mapping[str, Any]]) -> int:
    """Tokens for the SERIALISED schema list, via the repo's own authority.

    Uses ``harness.prompt_budget``'s divisor rather than a second literal, and
    serialises the schemas rather than counting them - for the reason T1's
    baseline records: "a provider is billed for the JSON, and a profile counting
    ``len(tools)`` would report 45 tokens for 45 tools and make the target look
    free".
    """
    from harness.agent_kernel.budget import CHARS_PER_TOKEN

    blob = json.dumps(list(schemas), default=str, sort_keys=True)
    return int(len(blob) / float(CHARS_PER_TOKEN))


def _schema_name(schema: Mapping[str, Any]) -> str:
    """Read a tool name out of either wire shape.

    The harness catalog emits the MCP/OpenAI function shape
    (``{"type": "function", "function": {"name": ...}}``); a flat
    ``{"name": ...}`` also appears in fixtures and in this module's own
    injected rows. Both are read, because guessing one shape produced a
    ``KeyError: 'name'`` on the first real run - which is the mildest possible
    version of "the driver never ran against the real catalog".
    """
    inner = schema.get("function")
    if isinstance(inner, Mapping):
        return str(inner.get("name") or "")
    return str(schema.get("name") or "")


def measure_arm(
    arm: str,
    *,
    query: str,
    schemas: Sequence[Mapping[str, Any]],
    candidates: Sequence[SearchCandidate],
    descriptions: Optional[Mapping[str, str]] = None,
    needed: Sequence[str] = (),
    discoverable: Optional[Sequence[str]] = None,
    discovery_tool: str = "search_tools",
    withheld: Sequence[str] = (),
    scorer: str = "",
) -> ArmResult:
    """Measure one arm end to end and return every axis, measured or refused.

    The five axes the brief asks for, and what each one is measured ON:

    ====================  ====================================================
    prompt tokens         the serialised request, via the repo's divisor
    tool-schema tokens    the serialised schema list alone - THE target
    resolve rate          deferred tools the model actually reached
    model calls           calls that changed behaviour, from the model itself
    cost                  ``unpriced``, never ``0.0``
    discoverability       the metric that could make this a REGRESSION
    ====================  ====================================================

    ``cost`` is reported as the string ``"unpriced"`` and never as ``0.0``,
    because no live provider ran and ``$0.00`` reads as cheapest to a price
    ladder. ``DOCTRINE.md`` section 1 names this exact failure.
    """
    overrides = dict(ARMS.get(arm, {}))
    if scorer:
        overrides["tool_deferral_scorer"] = str(scorer)
    policy = selection_from_config(overrides)
    if withheld:
        policy = DeferralPolicy(
            enabled=policy.enabled,
            threshold=policy.threshold,
            always_keep=policy.always_keep,
            withheld=tuple(withheld),
            defer_unmatched=policy.defer_unmatched,
            scorer_name=policy.scorer_name,
            max_results=policy.max_results,
            policy_version=policy.policy_version,
        )
    kept_schemas, receipt, decision = policy.view(
        query,
        candidates,
        schemas={_schema_name(s): s for s in schemas},
        descriptions=descriptions or {},
    )
    model = SchemaAwareModel(needed=needed, discoverable=discoverable)
    reply = model(
        [{"role": "user", "content": query}],
        model="ablation",
        tools=kept_schemas,
    )
    # EACH RECORDED SEARCH IS RESOLVED INDEPENDENTLY. The first version resolved
    # a single `select:<needed[0]>` and unioned its one hit, so a model that
    # searched could only ever recover one tool and every "capability intact"
    # rate was 1/n. A per-tool lookup is resolved per tool.
    #
    # A search route counts ONLY IF the model actually issued a search AND that
    # search's own resolution returned the tool. A model that never searches
    # therefore reports a 1.0 failure rate for the tools it did not have, which
    # is the real regression risk - "the model never thinks to look".
    searches = model.searches
    per_query_resolutions: List[Dict[str, Any]] = []
    resolved_names: List[str] = []
    for entry in searches:
        query = str(entry.get("query") or "")
        found = resolve_selection(query, candidates) if query else {"hits": []}
        names = [h.name for h in found.get("hits", [])]
        resolved_names.extend(names)
        per_query_resolutions.append(
            {
                "query": query,
                "resolution": found.get("resolution"),
                "scored": found.get("scored"),
                "returned": names,
            }
        )
    resolution = (
        {
            "resolution": "resolved_per_search",
            "hits": [],
            "per_query": per_query_resolutions,
        }
        if searches
        else {
            "resolution": "not_searched" if model.needed else "not_attempted",
            "hits": [],
        }
    )
    offered_names = set(model.schemas_seen)
    needed = [n for n in model.needed]
    routes: Dict[str, str] = {}
    reachable: List[str] = []
    failed: List[str] = []
    for name in needed:
        if name in offered_names:
            routes[name] = "schema_present"
        elif name in resolved_names:
            routes[name] = "discovered_by_search"
        else:
            routes[name] = "unreachable"
            failed.append(name)
            continue
        reachable.append(name)
    model_view = model.report()
    safety = deferral_safety_report(
        policy,
        decision,
        discovery_tool_names=[discovery_tool],
        catalog_names=[c.name for c in candidates],
    )
    return ArmResult(
        arm=arm,
        config=overrides,
        metrics={
            "schema_count_total": len(schemas),
            "schema_count_offered": len(kept_schemas),
            "schema_count_deferred": len(decision.deferred),
            "schema_count_withheld": len(decision.withheld),
            "tool_schema_tokens": _schema_tokens(kept_schemas),
            "tool_schema_tokens_full": _schema_tokens(schemas),
            "prompt_tokens": _schema_tokens(kept_schemas) + len(query) // 4,
            "prompt_tokens_full": _schema_tokens(schemas) + len(query) // 4,
            "deferred_receipt_chars": len(receipt.render()),
            "model_calls": len([c for c in model_view["calls"] if c.get("tool")]),
            "model_calls_total": 1,
            "model_searches": len(model_view["searches"]),
            # THE COST THE TOKEN SAVING HIDES. A deferred tool the model has to
            # go and find costs an EXTRA MODEL ROUND TRIP, and on a real
            # provider that is latency and money exactly like the tokens are.
            # On this driver the extra call is a scripted double and therefore
            # free, so the number is reported as a COUNT and never converted
            # into a saving. Publishing "40% fewer schema tokens" without this
            # line is how a token win gets mistaken for a latency win.
            "extra_round_trips_for_discovery": len(model_view["searches"]),
            "cost_usd": "unpriced",
            "cost_priced": False,
            "cost_source": "no_live_provider",
            "resolve_rate": (len(reachable) / len(needed)) if needed else None,
            "resolved_tools": reachable,
            "unresolved_tools": failed,
            "reach_routes": dict(routes),
            "discoverability_failure_rate": (
                (len(failed) / len(needed)) if needed else None
            ),
            "search_resolution": resolution.get("resolution"),
            "schemas_seen_by_model": model_view["schemas_seen_count"],
            "reply": reply,
            "estimator": "heuristic CHARS_PER_TOKEN from harness.agent_kernel.budget",
            "estimator_is_tokenizer": False,
        },
        decision=decision,
        receipt=receipt,
        safety=safety,
        events=[
            {
                "event": "tool_deferral",
                "arm": arm,
                "offered": model_view["schemas_seen_count"],
                "deferred": len(decision.deferred),
                "threshold": decision.threshold,
                "scorer": decision.scorer,
            },
            {
                "event": "tool_surface",
                "arm": arm,
                "note": (
                    "harness.tool_surface is the PERMISSION surface and is "
                    "unchanged in both arms; the withheld set is passed "
                    "through and never ranked"
                ),
                "withheld": list(decision.withheld),
            },
        ],
    )


def _deltas(by_arm: Mapping[str, ArmResult]) -> Dict[str, Any]:
    """Per-axis deltas, with ``None`` for any axis that is not measurable."""
    out: Dict[str, Any] = {}
    eager = by_arm.get("eager")
    deferred = by_arm.get("deferred")
    if eager is None or deferred is None:
        return {"comparable": False, "reason": "both arms are required"}
    axes = (
        "tool_schema_tokens",
        "prompt_tokens",
        "schema_count_offered",
        "schema_count_deferred",
        "model_calls",
        "model_searches",
        "extra_round_trips_for_discovery",
    )
    for axis in axes:
        left = eager.metrics.get(axis)
        right = deferred.metrics.get(axis)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            out[axis] = {
                "eager": left,
                "deferred": right,
                "delta": right - left,
                "relative": ((right - left) / left if left else None),
            }
        else:
            out[axis] = {
                "eager": left,
                "deferred": right,
                "delta": None,
                "relative": None,
                "reason": "axis is not measurable on this driver",
            }
    out["cost_usd"] = {
        "eager": eager.metrics.get("cost_usd"),
        "deferred": deferred.metrics.get("cost_usd"),
        "delta": None,
        "relative": None,
        "reason": "no live provider ran; a numeric cost here would be invented",
    }
    out["resolve_rate"] = {
        "eager": eager.metrics.get("resolve_rate"),
        "deferred": deferred.metrics.get("resolve_rate"),
        "delta": (
            (deferred.metrics.get("resolve_rate") or 0.0)
            - (eager.metrics.get("resolve_rate") or 0.0)
        ),
    }
    out["discoverability_failure_rate"] = {
        "eager": eager.metrics.get("discoverability_failure_rate"),
        "deferred": deferred.metrics.get("discoverability_failure_rate"),
        "delta": (
            (deferred.metrics.get("discoverability_failure_rate") or 0.0)
            - (eager.metrics.get("discoverability_failure_rate") or 0.0)
        ),
        "this_is_the_regression_axis": True,
        "note": (
            "a positive delta here means deferral made the model FAIL to reach "
            "a tool it could have discovered, which is the outcome that would "
            "make deferred loading a regression rather than a saving"
        ),
    }
    return out


def _verdict(
    by_arm: Mapping[str, ArmResult],
    single: Mapping[str, Any],
    safety_ok: bool,
    vacuity: Mapping[str, Any],
) -> str:
    """A three-valued verdict. Never "CLEAN" for a zero delta."""
    if not single.get("single_variable"):
        return "INVALID_MULTI_VARIABLE"
    if not safety_ok:
        return "UNSAFE"
    if not vacuity.get("agrees_with_record", False):
        return "VACUITY_RECORD_STALE"
    failure_delta = None
    deferred = by_arm.get("deferred")
    if deferred is not None:
        failure_delta = deferred.metrics.get("discoverability_failure_rate")
    if failure_delta is not None and failure_delta > 0.0:
        return "REGRESSION_DISCOVERABILITY"
    return "MEASURED"


#: The one tool the driver measures reachability FOR. Named, because "the tool
#: the model needs" appears in four places in this module and a second literal
#: would be a second answer to one question.
_MEASURED_TOOL = "read_config_file"

#: The queries the repeated measurement sweeps. A QUERY SWEEP rather than a
#: hand-picked "the deferring query", because whether a query exercises the
#: discovery path is a property of the SCORER, not of the query - and under the
#: shipped substring scorer at threshold 0.0 almost nothing is ever deferred,
#: because any English query matches any English description on a function word.
#: See :func:`derive_a_deferring_case`.
QUERIES: Dict[str, str] = {
    "matches_the_needed_tool": "read the configuration file and report the timeout",
    "does_not_match_it": "explain what this module does and summarise the architecture",
    "unrelated_short": "write a release note",
    "unrelated_docs": "update the documentation to describe the new flag",
    "unrelated_tests": "run the tests and tell me what broke",
    "empty": "",
}

#: The measured tool's DESCRIPTION. NOT ``f"the {name} tool"``, which is what the
#: first version used.
#:
#: The generic placeholder was not a neutral choice, it was a measurement
#: artefact: it contains "the", which the shipped substring scorer matches in
#: almost any English query, so the tool was KEPT for a reason that had nothing
#: to do with the task and the discovery path was never entered.
#:
#: Writing a *realistic* English description did NOT fix it either, and that is
#: the more interesting half of this finding. Measured: a description of "Retrieve
#: the parsed contents of a named configuration file and return its top-level
#: keys." scores **6.0 against a query about summarising architecture**, and so
#: does a description with no name match at all. Every English sentence contains
#: "the", "a" or "an", the substring scorer has no word boundary and no stopword
#: list, and at threshold 0.0 any positive score is a KEEP.
#:
#: So under the shipped default, **an English-described tool is essentially
#: never deferred**, whatever the query. That is the finding, and it is why
#: :func:`derive_a_deferring_case` sweeps a description with NO function words
#: rather than writing another realistic sentence and assuming it defers.
#: :data:`DERIVABLE_DESCRIPTION` is therefore deliberately terse and
#: non-English-shaped, and its necessity is asserted rather than assumed.
MEASURED_TOOL_DESCRIPTION = (
    "Retrieve the parsed contents of a named configuration file and return "
    "its top-level keys."
)

#: A description chosen to be UNMATCHABLE by the shipped substring scorer.
#:
#: Not a realistic tool description and never shipped. Its only job is to be the
#: one shape the scorer cannot match by accident, so the go/no-go axis becomes
#: reachable at all.
#:
#: The constraint is harder than it looks and the derivation is worth recording.
#: ``SubstringToolSearch.score`` tests ``term in description`` with no word
#: boundary and no stopword list, and every score is multiplied by 6. So the
#: description must contain NO letter that appears anywhere in any probe query:
#:
#: * "the" rules out every English sentence, because every English sentence
#:   contains "the", "a" or "an" - which is why the realistic description above
#:   scores 6.0 against a query about summarising architecture;
#: * a SINGLE-LETTER query term is fatal. "write a release note" contains "a",
#:   so the description may not contain the letter "a" at all - which is why the
#:   first attempt ("quorum ledger shard state reconciliation") was dropped:
#:   "state" contains an "a", so that query scored 6.0 and the derivation fell
#:   back to the degenerate empty query.
#:
#: Hence this string: no ``a``, no ``e``, no ``i``, no ``o``, no ``u``, no ``y``.
#: A test asserts it is genuinely unmatchable against every registered query, so
#: if a future change makes the scorer word-boundary-aware this constant becomes
#: unnecessary - and that test is written to say so when it happens, rather than
#: leaving a contrived fixture in place forever.
DERIVABLE_DESCRIPTION = "shrd-wght-blnc: pstgrd shrd stt rcncl"


def _real_catalog_names() -> Tuple[str, ...]:
    """The harness catalog's tool names, so 'injected' is derived not asserted."""
    _, _, candidates, _, _ = _with_measured_tool()
    return tuple(c.name for c in candidates)


def run_matrix(
    *,
    arms: Optional[Sequence[str]] = None,
    query: str = "read the configuration file and report the timeout",
    needed: Sequence[str] = (_MEASURED_TOOL,),
    withheld: Sequence[str] = ("privileged_reset",),
    scorer: str = "",
) -> Dict[str, Any]:
    """Run every arm over one query and return the publishable report.

    Uses the REAL harness catalog as the corpus - ``harness.tools``' typed tool
    schemas - rather than a synthetic one, because a synthetic catalog cannot
    show whether the mechanism helps at the scale that matters. The schemas are
    read, not modified.

    THE INJECTED TOOL'S DESCRIPTION IS A MEASUREMENT INSTRUMENT
    -----------------------------------------------------------
    The measured tool has no harness counterpart, so it is injected. The first
    version described it as ``f"the {name} tool"``, and that string made the
    whole go/no-go axis unfalsifiable: the shipped substring scorer tests
    ``term in description`` with no word boundary and no stopword list, so the
    word "the" matched almost any English query, the tool was KEPT for a reason
    unrelated to the task, and the discovery path was never entered.

    The description is therefore :data:`MEASURED_TOOL_DESCRIPTION`, and the
    report publishes whether the fixture is actually deferrable so a reader can
    check that rather than take it on trust.
    """
    selected = list(arms) if arms else list(ARMS)
    unknown = [a for a in selected if a not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm(s): {sorted(unknown)}")
    _, schemas, candidates, descriptions, _ = _with_measured_tool(scorer)
    injected_names = [n for n in (*needed, *withheld) if n not in _real_catalog_names()]
    if "search_tools" not in descriptions:
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "search_tools",
                    "description": "Search the tool catalog for a tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        candidates.append(
            SearchCandidate(name="search_tools", description="Search the tool catalog")
        )
        descriptions["search_tools"] = "Search the tool catalog"

    results = [
        measure_arm(
            arm,
            query=query,
            schemas=schemas,
            candidates=candidates,
            descriptions=descriptions,
            needed=needed,
            discoverable=list(needed),
            withheld=withheld,
            scorer=scorer,
        )
        for arm in selected
    ]
    by_arm = {r.arm: r for r in results}
    # The single-variable check runs on the EFFECTIVE configs the measurement
    # actually used, not on the module-level ARMS. Those differ whenever a
    # scorer is supplied, and a check run on a dict the measurement did not use
    # is the "a claim about a diff rather than the diff" defect this module
    # already fixed once for the arm table.
    effective = {r.arm: dict(r.config) for r in results}
    single = single_variable_report(effective)
    deltas = _deltas(by_arm)
    vacuity = verify_vacuity()
    safety_ok = all(r.safety.get("safe") for r in results)
    decision_view = by_arm.get("deferred")
    decision = decision_view.decision if decision_view is not None else None
    return {
        "kind": "p3b_tools_deferral_ablation",
        "query": query,
        "scorer": scorer or "substring (module default)",
        "arms": selected,
        "corpus": {
            "source": "harness.tools.typed_tool_specs()",
            "schema_count": len(schemas),
            "injected_for_measurement": injected_names,
            "measured_tool_description": MEASURED_TOOL_DESCRIPTION,
            "measured_tool_deferred": (
                _MEASURED_TOOL in decision.deferred if decision is not None else None
            ),
            "enters_discovery_path": (
                _MEASURED_TOOL in decision.deferred if decision is not None else None
            ),
            "note": (
                "the catalog is the real one; the measured tool is ADDED with a "
                "realistic description so a resolve rate and a discoverability "
                "rate are defined rather than None"
            ),
        },
        "observations": [r.to_dict() for r in results],
        "single_variable": single,
        "deltas": deltas,
        "vacuity": {**VACUITY, "verified": vacuity},
        "safety_ok": safety_ok,
        "verdict": _verdict(by_arm, single, safety_ok, vacuity),
        "honesty": [
            "No live provider ran. No credential was read, printed or retained.",
            "The model is a scripted double whose signature NAMES `tools`, "
            "which the shipped mock's does not; without that the success axis "
            "is vacuous and this driver says so instead of reporting a zero.",
            "Token figures are a CHARS_PER_TOKEN heuristic over the SERIALISED "
            "schema list, not tokenizer counts, and every metric says which.",
            "`cost_usd` is the string 'unpriced', never 0.0: no provider ran, and "
            "a $0.00 reads as cheapest to a price ladder.",
            "The headline token saving is bought with EXTRA MODEL ROUND TRIPS "
            "(`extra_round_trips_for_discovery`), which are free on a scripted "
            "double and are NOT free on a real provider. Both numbers are "
            "reported together so a token win cannot be read as a latency win.",
            "resolve_rate and discoverability_failure_rate are measured over "
            "the tools this driver asks for; when nothing is deferred they are "
            "0.0 BY CONSTRUCTION and corpus.enters_discovery_path says so.",
        ],
    }


def _catalog():
    """The real catalog, built once per call. Reads ``harness.tools``; no copy."""
    from harness.tools import typed_tool_specs
    from integrations.tool_search import SearchCandidate

    specs = list(typed_tool_specs())
    schemas = [spec.to_schema() for spec in specs]
    candidates = [
        SearchCandidate(name=str(s.name), description=str(s.description), tags=())
        for s in specs
    ]
    descriptions = {str(s.name): str(s.description) for s in specs}
    return specs, schemas, candidates, descriptions


def _inject(
    candidates: List[SearchCandidate],
    schemas: List[Dict[str, Any]],
    descriptions: Dict[str, str],
    name: str,
    description: str,
) -> None:
    """Add one tool to the corpus if absent. The single injection path.

    One function because three copies of this block is three places for the
    description to drift, and the description is a measurement instrument here:
    :data:`DERIVABLE_DESCRIPTION` versus :data:`MEASURED_TOOL_DESCRIPTION`
    changes whether the go/no-go axis is reachable at all.
    """
    if any(c.name == name for c in candidates):
        return
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    )
    candidates.append(SearchCandidate(name=name, description=description))
    descriptions[name] = description


def _with_measured_tool(scorer: str = "", *, derivable: bool = False):
    """The real catalog plus the measured tool, injected with a real description.

    Injected rather than measured-in-place because no harness tool is named
    ``read_config_file``; without one the resolve rate and the discoverability
    rate are ``None`` and the driver reports two nulls and a zero delta. The
    schemas are read, never modified.

    :param derivable: use :data:`DERIVABLE_DESCRIPTION` instead of
        :data:`MEASURED_TOOL_DESCRIPTION`. Only the go/no-go derivation needs
        it, because only that function has to defeat the substring scorer's
        stopword blindness to reach a deferred state at all. Every ordinary
        measurement run uses the realistic description, because using the
        contrived one everywhere would make the headline token number describe a
        tool that does not exist.
    """
    from integrations.tool_search import scorer_by_name

    specs, schemas, candidates, descriptions = _catalog()
    measured_description = (
        DERIVABLE_DESCRIPTION if derivable else MEASURED_TOOL_DESCRIPTION
    )
    _inject(candidates, schemas, descriptions, _MEASURED_TOOL, measured_description)
    _inject(
        candidates,
        schemas,
        descriptions,
        "privileged_reset",
        "Reset the privileged sandbox permission set.",
    )
    _inject(
        candidates,
        schemas,
        descriptions,
        "search_tools",
        "Search the tool catalog for a tool",
    )
    return (
        specs,
        schemas,
        candidates,
        descriptions,
        (scorer_by_name(scorer) if scorer else None),
    )


def _registered_scorer_names() -> Tuple[str, ...]:
    """The registered scorer names, read from the registry, not re-listed.

    Re-listing them here would mean a scorer added tomorrow is not swept
    tomorrow, and the go/no-go sweep would silently cover less than the
    product can do - which is the "the registry some lanes read and
    others ignore" defect this repo has already paid for in CI.
    """
    from integrations.tool_search import TOOL_SEARCH_SCORERS

    return tuple(sorted(TOOL_SEARCH_SCORERS))


_REGISTERED_SCORERS: Tuple[str, ...] = _registered_scorer_names()


def derive_a_deferring_case(
    *,
    needed: Sequence[str] = (_MEASURED_TOOL,),
    scorer: str = "",
) -> Optional[Dict[str, Any]]:
    """Find a (query, scorer) pair that actually DEFERS the measured tool.

    WHY THIS IS SEARCHED FOR RATHER THAN WRITTEN DOWN
    -------------------------------------------------
    The first version of the go/no-go probe named one query as "the one that
    defers the tool". It did not. Under the shipped substring scorer at
    threshold 0.0 a tool is KEPT whenever ANY query term is a substring of its
    description, and every English description contains "the" or "a", so
    **essentially nothing is ever deferred** - measured: 1 to 12 of 45 tools
    across six realistic queries.

    So a hand-written "this query defers it" is a claim about a scorer that has
    to be re-checked every time the corpus or the default scorer changes. This
    function DERIVES the case instead.

    WHY IT PREFERS A MINORITY, NOT A SWEEP
    ---------------------------------------
    The empty query defers **all 45 tools** and trivially satisfies "the
    measured tool is deferred". Publishing that as the go/no-go case would be
    measuring the degenerate policy where nothing is loaded - a configuration
    whose token saving is maximal precisely because the agent is blind. So
    candidates are filtered to a policy that keeps a real share of the catalog
    (:data:`MAX_DEFERRED_FRACTION` of it), and among those the one that defers
    the FEWEST tools wins: the smallest intervention that still exercises the
    path is the most informative one, because it is the one a shipping policy
    would plausibly use.

    :returns: the best ``{"query", "query_label", "scorer", "deferred", ...}``,
        or ``None`` when no combination defers the measured tool at all.
    """
    from integrations.tool_deferral import selection_from_config

    candidates_found: List[Dict[str, Any]] = []
    for label, query in QUERIES.items():
        for scorer_name in [scorer] if scorer else list(_REGISTERED_SCORERS):
            # The DERIVABLE description, not the realistic one. Under the
            # shipped substring scorer a realistic English description is
            # matched by almost any query (see DERIVABLE_DESCRIPTION), so with
            # it this function finds nothing and the axis stays vacuous.
            _, _, catalog, _, _ = _with_measured_tool(scorer_name, derivable=True)
            policy = selection_from_config(
                {
                    "tool_deferral_enabled": True,
                    "tool_deferral_scorer": scorer_name,
                    "tool_deferral_withheld": ["privileged_reset"],
                }
            )
            decision = policy.decide(query, catalog)
            if not all(name in decision.deferred for name in needed):
                continue
            fraction = len(decision.deferred) / max(1, len(catalog))
            candidates_found.append(
                {
                    "query": query,
                    "query_label": label,
                    "scorer": scorer_name,
                    "deferred": list(decision.deferred),
                    "deferred_count": len(decision.deferred),
                    "catalog_count": len(catalog),
                    "deferred_fraction": fraction,
                }
            )
    usable = [
        c for c in candidates_found if c["deferred_fraction"] <= MAX_DEFERRED_FRACTION
    ]
    pool = usable or candidates_found
    if not pool:
        return None
    pool.sort(key=lambda c: (c["deferred_count"], c["query_label"], c["scorer"]))
    best = dict(pool[0])
    best["all_candidates"] = candidates_found
    best["rejected_as_degenerate"] = [
        {
            "query_label": c["query_label"],
            "scorer": c["scorer"],
            "deferred_fraction": round(c["deferred_fraction"], 4),
        }
        for c in candidates_found
        if c["deferred_fraction"] > MAX_DEFERRED_FRACTION
    ]
    best["selection_rule"] = (
        "fewest tools deferred among candidates whose deferred fraction is at "
        f"most {MAX_DEFERRED_FRACTION:.0%}; the empty query defers the entire "
        "catalog and is rejected as degenerate"
    )
    return best


#: A derived go/no-go case must not be the degenerate "nothing is loaded"
#: policy. Above this deferred fraction the case measures a blind agent, whose
#: token saving is maximal and whose capability is nil - true, and useless as a
#: measurement of the mechanism.
MAX_DEFERRED_FRACTION = 0.80


def vacuity_of_the_go_no_go_axis() -> Dict[str, Any]:
    """Can the go/no-go metric EVER be non-zero? Drive it there and measure.

    THIS IS THE HEADLINE CHECK OF THE WHOLE ABLATION, AND IT FOUND TWO BUGS.
    -----------------------------------------------------------------------
    Two SEPARATE vacuity questions, and the second one is the dangerous one:

    1. Do the two arms differ in exactly one variable?
       :func:`single_variable_report` answers that, and it is about the CONFIGS.
    2. **Can the headline metric move at all?** Nothing before this wave checked
       it, and the answer was NO, three times over:

       a. ``measure_arm`` resolved the needed tool UNCONDITIONALLY, and
          ``select:<name>`` resolves any catalog tool by exact lookup, so
          ``discoverability_failure_rate`` was structurally 0.0 and no setting
          of any parameter could move it. Verified by sweeping ``discoverable``
          over ``[needed]``, ``[]``, ``["something_else"]`` and ``None``:
          0.0 in all four.
       b. The injected fixture description was ``f"the {name} tool"``. Under the
          shipped substring scorer ``term in description`` has no word boundary
          and no stopword list, so the word "the" matched almost any English
          query, the tool was KEPT for a reason unrelated to the task, and the
          discovery path was never entered.
       c. The chosen "needed" tool was never deferred by ANY realistic query
          under EITHER registered scorer - only by the empty query, which
          defers 46 of 47 tools and is the degenerate blind-agent policy.

    The fix for (c) is the actual experiment: **measure the tools deferral
    really hides.** For each realistic query the deferred set is taken from the
    real decision and used AS the need, so the question becomes "given the
    tools this policy removed, can the model still reach them?" - which is the
    question the phase actually has.

    THE NUMBER THIS PRODUCES, AND WHY IT IS THE GO/NO-GO
    ---------------------------------------------------
    The eager arm reaches a tool by its schema, so its failure rate is 0.0
    regardless of model behaviour. The deferred arm's rate depends entirely on
    whether the model SEARCHES. So the measured quantity is the price of
    deferral in capability, priced in searches:

    * a model that searches when it needs a tool -> **0.0**, capability intact,
      at the cost of ``extra_round_trips_for_discovery``;
    * a model that does not -> **1.0**, capability gone, invisibly.

    That is the whole trade, measured, and neither number is a model-quality
    claim: both models are scripted doubles.
    """
    observations: List[Dict[str, Any]] = []
    sweep: List[Dict[str, Any]] = []
    for label, query in QUERIES.items():
        for scorer_name in list(_REGISTERED_SCORERS):
            # DERIVABLE description for the same reason as in
            # derive_a_deferring_case: the measured tool has to actually be
            # deferrable for this row to measure anything.
            derived = derive_a_deferring_case_for_query(
                query, scorer_name, derivable=True
            )
            if derived is None:
                sweep.append(
                    {
                        "query_label": label,
                        "scorer": scorer_name,
                        "deferred_count": 0,
                        "skipped": "the policy defers nothing, so there is no "
                        "deferral risk to measure on this row",
                    }
                )
                continue
            needed = tuple(derived["deferred"])
            _, schemas, candidates, descriptions, _ = _with_measured_tool(
                scorer_name, derivable=True
            )
            row: Dict[str, Any] = {
                "query_label": label,
                "scorer": scorer_name,
                "query": query,
                "deferred_count": derived["deferred_count"],
                "deferred_fraction": round(derived["deferred_fraction"], 4),
                "models": [],
            }
            for behaviour, disc in (
                ("searches_when_needed", list(needed)),
                ("never_searches", []),
            ):
                for arm in ("eager", "deferred"):
                    result = measure_arm(
                        arm,
                        query=query,
                        schemas=schemas,
                        candidates=candidates,
                        descriptions=descriptions,
                        needed=needed,
                        discoverable=disc,
                        scorer=scorer_name,
                    )
                    row["models"].append(
                        {
                            "model_behaviour": behaviour,
                            "arm": arm,
                            "discoverability_failure_rate": result.metrics.get(
                                "discoverability_failure_rate"
                            ),
                            "resolve_rate": result.metrics.get("resolve_rate"),
                            "extra_round_trips": result.metrics.get(
                                "extra_round_trips_for_discovery"
                            ),
                            "n_needed": len(needed),
                            "n_unresolved": len(
                                result.metrics.get("unresolved_tools") or ()
                            ),
                        }
                    )
            sweep.append(row)
            observations.extend(row["models"])

    rates = [
        o["discoverability_failure_rate"]
        for o in observations
        if isinstance(o["discoverability_failure_rate"], (int, float))
    ]
    deferred_searching = [
        o["discoverability_failure_rate"]
        for o in observations
        if o["arm"] == "deferred"
        and o["model_behaviour"] == "searches_when_needed"
        and isinstance(o["discoverability_failure_rate"], (int, float))
    ]
    deferred_blind = [
        o["discoverability_failure_rate"]
        for o in observations
        if o["arm"] == "deferred"
        and o["model_behaviour"] == "never_searches"
        and isinstance(o["discoverability_failure_rate"], (int, float))
    ]
    can_fail = bool(rates) and max(rates) > 0.0
    can_pass = bool(rates) and min(rates) == 0.0
    # The capability question: does a SEARCHING model keep every deferred tool?
    intact_when_searching = bool(deferred_searching) and max(deferred_searching) == 0.0
    lost_when_blind = bool(deferred_blind) and max(deferred_blind) > 0.0
    return {
        "question": (
            "can discoverability_failure_rate ever be non-zero, or is it "
            "structurally 0.0 and therefore not a go/no-go?"
        ),
        "sweep": sweep,
        "observations": observations,
        "observed_rates": rates,
        "can_fail": can_fail,
        "can_pass": can_pass,
        "non_vacuous": can_fail and can_pass,
        "capability_intact_when_the_model_searches": intact_when_searching,
        "capability_lost_when_the_model_does_not_search": lost_when_blind,
        "consequence": (
            "the go/no-go axis distinguishes a model that looks from one that "
            "does not. Deferral is capability-neutral for a model that searches "
            "and capability-destroying for one that does not, so the lever this "
            "phase is pulling is not 'defer the tools' but 'make the search "
            "seam reliable' - and no number here measures the second thing."
        )
        if intact_when_searching and lost_when_blind
        else (
            "THE GO/NO-GO AXIS COULD NOT BE EXERCISED. Do not publish a "
            "discoverability number from this driver."
        ),
        "note": (
            "every model here is a scripted double. These rates measure the "
            "MECHANISM's dependence on model behaviour, not any model's "
            "behaviour."
        ),
    }


def derive_a_deferring_case_for_query(
    query: str, scorer_name: str, *, derivable: bool = False
) -> Optional[Dict[str, Any]]:
    """What does this (query, scorer) actually defer? ``None`` when: nothing.

    Read from the real decision, including the withheld set, so the measured
    need is the set of tools deferral really removed rather than a hand-picked
    one. The withheld set is passed because omitting it is how a permission
    decision gets re-admitted, and a probe that quietly differs from the
    product path is a probe of the probe.
    """
    from integrations.tool_deferral import selection_from_config

    _, _, candidates, _, _ = _with_measured_tool(scorer_name, derivable=derivable)
    policy = selection_from_config(
        {
            "tool_deferral_enabled": True,
            "tool_deferral_scorer": scorer_name,
            "tool_deferral_withheld": ["privileged_reset"],
        }
    )
    decision = policy.decide(query, candidates)
    if not decision.deferred:
        return None
    return {
        "deferred": tuple(decision.deferred),
        "deferred_count": len(decision.deferred),
        "catalog_count": len(candidates),
        "deferred_fraction": len(decision.deferred) / max(1, len(candidates)),
    }


def variance(samples: Sequence[Any]) -> Dict[str, Any]:
    """Mean, spread and sample count for one axis across repetitions.

    Reports the SPREAD rather than only the mean, because the brief asks for
    variance and a mean without one hides a bimodal result. For a deterministic
    scripted model the honest answer is usually ``spread == 0.0``, and that is
    itself the finding: it says the repetition count is measuring the driver's
    determinism, not the world. ``n_distinct`` makes that visible rather than
    leaving a reader to infer it from a suspiciously tidy number.

    Non-numeric samples (an ``unpriced`` string, a ``None``) are counted and
    reported rather than coerced - an axis that was never measured has no
    variance, and reporting ``0.0`` for it would be the same lie as reporting a
    zero.
    """
    values = [
        s for s in samples if isinstance(s, (int, float)) and not isinstance(s, bool)
    ]
    absent = len(samples) - len(values)
    if not values:
        return {
            "n": 0,
            "n_absent": absent,
            "mean": None,
            "spread": None,
            "n_distinct": 0,
            "note": "no numeric sample on this axis; variance is not computable",
        }
    lo, hi = min(values), max(values)
    mean = sum(values) / len(values)
    return {
        "n": len(values),
        "n_absent": absent,
        "mean": mean,
        "min": lo,
        "max": hi,
        "spread": hi - lo,
        "n_distinct": len(set(values)),
        "note": (
            "spread is 0.0 across repetitions because the driver is "
            "scripted-deterministic; that is a property of the measurement, not "
            "evidence about a real model"
        )
        if hi == lo
        else "spread is non-zero: the axis is not deterministic on this driver",
    }


def run_repeated(
    *,
    repeats: int = 5,
    queries: Optional[Mapping[str, str]] = None,
    arms: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run the matrix ``repeats`` times per query and report spread per axis.

    WHY A QUERY SWEEP AND NOT JUST N REPEATS
    ----------------------------------------
    Repeating one query N times measures the driver's determinism, which for a
    scripted double is exactly zero and tells nobody anything. The variation
    that actually matters is WHICH QUERY is asked, because the deferral decision
    is per-query: of the four queries in :data:`QUERIES`, three keep the needed
    tool and one defers it, and only that one enters the discovery path at all.

    So the design is ``repeats x len(queries)`` observations per arm, and the
    spread is reported BOTH across repetitions (determinism) and ACROSS QUERIES
    (the real variation). Publishing only the first would be the most flattering
    possible presentation of this driver and it is the one this brief's
    "report variance" instruction exists to prevent.

    :param repeats: repetitions per query. Must be >= 1; a value of 0 is a
        caller error and raises rather than quietly returning an empty report
        that reads as "no data, no problem".
    """
    if int(repeats) < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats!r}")
    selected = list(arms) if arms else list(ARMS)
    unknown = [a for a in selected if a not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm(s): {sorted(unknown)}")
    query_map = dict(queries) if queries is not None else dict(QUERIES)

    per_query: List[Dict[str, Any]] = []
    for label, query in query_map.items():
        runs = [run_matrix(arms=selected, query=query) for _ in range(int(repeats))]
        by_arm: Dict[str, List[Dict[str, Any]]] = {a: [] for a in selected}
        for report in runs:
            for obs in report["observations"]:
                by_arm[obs["arm"]].append(obs["metrics"])
        axes = (
            "prompt_tokens",
            "tool_schema_tokens",
            "schema_count_offered",
            "schema_count_deferred",
            "resolve_rate",
            "discoverability_failure_rate",
            "model_calls",
            "extra_round_trips_for_discovery",
            "model_searches",
        )
        stats = {
            arm: {axis: variance([m.get(axis) for m in metrics_rows]) for axis in axes}
            for arm, metrics_rows in by_arm.items()
        }
        per_query.append(
            {
                "query_label": label,
                "query": query,
                "repeats": int(repeats),
                "needed_tool_kept": _needed_tool_kept(query),
                "enters_discovery_path": not _needed_tool_kept(query),
                "verdicts": [r["verdict"] for r in runs],
                "single_variable": {r["single_variable"]["verdict"] for r in runs},
                "spread": stats,
            }
        )

    # Cross-query spread, per arm, on the two axes the verdict rests on.
    cross: Dict[str, Any] = {}
    for arm in selected:
        rows = [q for q in per_query if q["query_label"] in query_map]
        cross[arm] = {
            axis: variance(
                [
                    q["spread"][arm][axis]["mean"]
                    for q in rows
                    if q["spread"][arm][axis]["mean"] is not None
                ]
            )
            for axis in ("tool_schema_tokens", "discoverability_failure_rate")
        }
    go_no_go = vacuity_of_the_go_no_go_axis()
    deferring_queries = [
        q["query_label"] for q in per_query if q["enters_discovery_path"]
    ]
    return {
        "kind": "p3b_tools_deferral_ablation_repeated",
        "repeats": int(repeats),
        "queries": query_map,
        "arms": selected,
        "per_query": per_query,
        "cross_query": cross,
        "go_no_go_vacuity": go_no_go,
        "queries_that_enter_the_discovery_path": deferring_queries,
        "single_variable_ok": all(
            v == "SINGLE_VARIABLE" for q in per_query for v in q["single_variable"]
        ),
        "verdict": _verdict(
            {a: ArmResult(arm=a, config=ARMS[a]) for a in selected},
            single_variable_report({a: ARMS[a] for a in selected}),
            True,
            {"agrees_with_record": True},
        )
        if go_no_go["non_vacuous"]
        else "GO_NO_GO_AXIS_VACUOUS",
        "honesty": [
            "repetitions of a scripted-deterministic model measure the DRIVER, "
            "not a model. spread==0.0 on an axis is a statement about this "
            "codebase's determinism and is not evidence about any real model.",
            "the variation that matters is ACROSS QUERIES, because the deferral "
            "decision is per-query; cross_query is reported for that reason.",
            f"only {len(deferring_queries)} of {len(query_map)} queries actually "
            "enter the discovery path. A run of the shipped default query alone "
            "measures nothing about discoverability.",
            "cost_usd is the string 'unpriced' in every arm. No live provider "
            "ran and no credential was read.",
        ],
    }


#: English function words. Used ONLY by :func:`scorer_audit` to attribute a
#: match to a stopword, never by the scorer itself - changing the shipped
#: scorer's behaviour is a measured decision this wave does not get to make, and
#: silently "fixing" it would move the headline number in the flattering
#: direction without anybody having decided that.
_STOPWORDS: Tuple[str, ...] = (
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "one",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "with",
)


def scorer_audit() -> Dict[str, Any]:
    """Compare the REGISTERED scorers over the REAL catalog, per query.

    This exists because the deferral number is only as meaningful as the ranking
    behind it, and nothing had ever compared the two scorers the registry
    offers. It answers two questions with numbers:

    1. **Do the scorers agree?** Jaccard over the kept sets. Zero agreement
       means the deferral decision is a property of the scorer rather than of
       the query, and a prompt-budget number built on it inherits that.
    2. **How much of a match is a STOPWORD?** The shipped substring scorer tests
       ``term in description`` with no word boundary and no stopword list, so a
       query term like "the" matches any description containing "the".
       Measured here rather than asserted, because the size of the artefact
       decides whether it is a rounding error or the whole result.
    """
    from integrations.tool_deferral import selection_from_config
    from integrations.tool_search import rank_tools, scorer_by_name

    _, _, candidates, _, _ = _with_measured_tool("")
    by_name = {c.name: c for c in candidates}
    rows: List[Dict[str, Any]] = []
    for label, query in QUERIES.items():
        kept: Dict[str, set] = {}
        deferred: Dict[str, int] = {}
        stopword_only: Dict[str, int] = {}
        for scorer_name in _REGISTERED_SCORERS:
            hits = {
                h.name
                for h in rank_tools(
                    query,
                    candidates,
                    scorer=scorer_by_name(scorer_name),
                    limit=max(1, len(candidates)),
                )
                if h.matched
            }
            kept[scorer_name] = hits
            policy = selection_from_config(
                {
                    "tool_deferral_enabled": True,
                    "tool_deferral_scorer": scorer_name,
                    "tool_deferral_withheld": ["privileged_reset"],
                }
            )
            deferred[scorer_name] = len(policy.decide(query, candidates).deferred)
            query_terms = [
                t for t in str(query or "").lower().split() if t not in _STOPWORDS
            ]
            only_stop = 0
            for name in hits:
                description = str(by_name[name].description).lower()
                if any(t in description for t in query_terms):
                    continue
                only_stop += 1
            stopword_only[scorer_name] = only_stop
        names = list(_REGISTERED_SCORERS)
        unions, inters = [], []
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                a, b = kept[left], kept[right]
                inters.append(len(a & b))
                unions.append(len(a | b))
        rows.append(
            {
                "query_label": label,
                "query": query,
                "kept": {k: len(v) for k, v in kept.items()},
                "deferred": deferred,
                "catalog_count": len(candidates),
                "stopword_only_kept": stopword_only,
                "stopword_only_share": {
                    k: (v / len(kept[k]) if kept[k] else None)
                    for k, v in stopword_only.items()
                },
                "jaccard": (
                    (sum(inters) / sum(unions)) if unions and sum(unions) else None
                ),
                "kept_only_by": {
                    left: sorted(kept[left] - kept[names[1 - i]])
                    for i, left in enumerate(names)
                    if len(names) == 2
                },
            }
        )
    return {
        "question": (
            "is the deferral decision a measurement of query relevance, or a "
            "property of the scorer?"
        ),
        "scorers": list(_REGISTERED_SCORERS),
        "default_scorer": "substring",
        "queries": rows,
        "mean_jaccard": (
            sum(r["jaccard"] for r in rows if r["jaccard"] is not None)
            / max(1, len([r for r in rows if r["jaccard"] is not None]))
        ),
        "agree_on_anything": any((r["jaccard"] or 0.0) > 0.0 for r in rows),
        "finding": (
            "the two registered scorers disagree on most of the catalog, so the "
            "deferral number is scorer-dependent and is published with the "
            "scorer named. Under the shipped default (substring) a large share "
            "of the kept set is kept by an English function word rather than by "
            "relevance, which means the tool-schema token saving is partly a "
            "stopword artefact rather than a prompt-budget win."
        ),
        "not_changed": (
            "the shipped default scorer was NOT switched to bm25. That is a "
            "measured product decision with its own arms, and changing it here "
            "would move this phase's headline in the flattering direction "
            "without anyone having decided to move it."
        ),
    }


def render_scorer_audit(doc: Mapping[str, Any]) -> str:
    """The human table for :func:`scorer_audit`."""
    lines = [
        "scorer audit: " + doc["finding"],
        "",
        f"{'query':26} {'scorer':10} {'kept':>5} {'deferred':>9} {'stopword-only':>14}",
        "-" * 72,
    ]
    for row in doc["queries"]:
        for scorer in doc["scorers"]:
            share = row["stopword_only_share"].get(scorer)
            lines.append(
                f"{row['query_label']:26} {scorer:10} "
                f"{row['kept'].get(scorer, 0):>5} "
                f"{row['deferred'].get(scorer, 0):>9} "
                f"{(('%.0f%%' % (100 * share)) if share is not None else '-'):>14}"
            )
        lines.append(
            f"{'':26} {'jaccard':10} {row['jaccard'] if row['jaccard'] is not None else '-'}"
        )
    lines += ["", doc["not_changed"]]
    return "\n".join(lines)


def render_repeated(doc: Mapping[str, Any]) -> str:
    """The human table for :func:`run_repeated`."""
    lines = [
        f"tools ablation, {doc['repeats']} repeats x {len(doc['queries'])} queries",
        f"verdict={doc['verdict']} single_variable_ok={doc['single_variable_ok']}",
        "",
    ]
    for q in doc["per_query"]:
        lines.append(
            f"[{q['query_label']}] deferred-needed={q['needed_tool_kept']} "
            f"enters_discovery_path={q['enters_discovery_path']}"
        )
        for arm, axes in sorted(q["spread"].items()):
            for axis, stat in sorted(axes.items()):
                if stat["mean"] is None:
                    continue
                lines.append(
                    f"   {arm:9} {axis:34} mean={stat['mean']:<12} "
                    f"spread={stat['spread']:<10} n={stat['n']} "
                    f"n_distinct={stat['n_distinct']}"
                )
        lines.append("")
    lines.append("CROSS-QUERY SPREAD (the variation that matters):")
    for arm, axes in sorted(doc["cross_query"].items()):
        for axis, stat in sorted(axes.items()):
            lines.append(
                f"   {arm:9} {axis:34} mean={stat['mean']} spread={stat['spread']}"
            )
    go = doc["go_no_go_vacuity"]
    lines += [
        "",
        f"go/no-go axis non_vacuous={go['non_vacuous']} "
        f"intact_when_searching={go['capability_intact_when_the_model_searches']} "
        f"lost_when_blind={go['capability_lost_when_the_model_does_not_search']}",
        "",
        "HONESTY:",
    ]
    for line in doc["honesty"]:
        lines.append(f"  - {line}")
    return "\n".join(lines)


def _needed_tool_kept(query: str, scorer: str = "") -> bool:
    """Whether the deferral decision KEEPS the measured tool. A derived fact.

    Read from a real decision rather than asserted in a table, because the whole
    point of the query sweep is that whether a query exercises the discovery path
    is a property of the SCORER, not of the author who chose the query. A
    hand-written ``{"does_not_match_it": True}`` would be exactly the kind of
    claim this repository keeps having to retract.
    """
    from integrations.tool_deferral import selection_from_config

    _, _, candidates, _, _ = _with_measured_tool(scorer)
    policy = selection_from_config(
        {"tool_deferral_enabled": True, "tool_deferral_scorer": scorer}
        if scorer
        else {"tool_deferral_enabled": True}
    )
    decision = policy.decide(query, candidates)
    return _MEASURED_TOOL in decision.kept


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI. Exit 2 on an invalid or unsafe experiment, 0 on a measured one.

    ``--vacuity`` prints only the re-derived vacuity evidence and always exits
    0, because a finding that the shipped mock cannot exercise this mechanism is
    information rather than a build failure. A ``--json`` run prints the report
    alone, which is what CI should parse.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.tools_ablation",
        description="Ablate deferred tool-schema loading against eager loading.",
    )
    parser.add_argument("--arms", default=None, help="comma list or 'all'")
    parser.add_argument("--query", default=None)
    parser.add_argument("--scorer", default="", help="scorer registry name")
    parser.add_argument(
        "--out", default=None, help="directory to write the report into"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--vacuity", action="store_true", help="print the vacuity evidence only"
    )
    parser.add_argument(
        "--go-no-go",
        action="store_true",
        help="prove the discoverability axis can fail, and sweep it",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=0,
        help="run the query sweep N times per query and report spread",
    )
    parser.add_argument(
        "--scorer-audit",
        action="store_true",
        help="compare the registered scorers' decisions over the real catalog",
    )
    args = parser.parse_args(argv)
    if args.vacuity:
        print(json.dumps(verify_vacuity(), indent=2, default=str))
        return 0
    arms = None
    if args.arms:
        arms = list(ARMS) if args.arms == "all" else args.arms.split(",")
    if args.go_no_go:
        probe = vacuity_of_the_go_no_go_axis()
        if args.json:
            print(json.dumps(probe, indent=2, default=str))
        else:
            print("go/no-go axis: non_vacuous =", probe["non_vacuous"])
            print(
                f"  capability intact when the model searches   = "
                f"{probe['capability_intact_when_the_model_searches']}"
            )
            print(
                f"  capability lost when it does not search     = "
                f"{probe['capability_lost_when_the_model_does_not_search']}"
            )
            for row in probe["sweep"]:
                if "skipped" in row:
                    print(
                        f"  [{row['query_label']}/{row['scorer']}] SKIPPED: "
                        f"{row['skipped']}"
                    )
                    continue
                for m in row["models"]:
                    print(
                        f"  [{row['query_label']}/{row['scorer']}] "
                        f"deferred={row['deferred_count']:>2} "
                        f"{m['model_behaviour']:<20} {m['arm']:<9} "
                        f"failure_rate={m['discoverability_failure_rate']} "
                        f"round_trips={m['extra_round_trips']}"
                    )
            print()
            print(probe["consequence"])
        return 0 if probe["non_vacuous"] else 2
    if args.scorer_audit:
        audit = scorer_audit()
        if args.json:
            print(json.dumps(audit, indent=2, default=str))
        else:
            print(render_scorer_audit(audit))
        return 2 if not audit["agree_on_anything"] else 0
    if int(args.repeats) >= 1:
        try:
            report = run_repeated(repeats=int(args.repeats), arms=arms)
        except ValueError as exc:
            print(f"eval error: {exc}")
            return 2
        if args.out:
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "tools_ablation_repeated.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(render_repeated(report))
        return 0 if report["verdict"] not in {"GO_NO_GO_AXIS_VACUOUS"} else 2
    arms = None
    if args.arms:
        arms = list(ARMS) if args.arms == "all" else args.arms.split(",")
    try:
        report = run_matrix(
            arms=arms,
            query=args.query or QUERIES["matches_the_needed_tool"],
            scorer=args.scorer,
        )
    except ValueError as exc:
        print(f"eval error: {exc}")
        return 2
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "tools_ablation_report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"verdict={report['verdict']} arms={report['arms']}")
        print(
            f"single_variable={report['single_variable']['verdict']} "
            f"differing={report['single_variable']['differing_keys']}"
        )
        print(
            f"vacuity: request_axis_vacuous={report['vacuity']['request_axis_vacuous']} "
            f"success_axis_vacuous_under_shipped_mock="
            f"{report['vacuity']['success_axis_vacuous_under_shipped_mock']} "
            f"model={report['vacuity']['model_used']}"
        )
        for row in report["observations"]:
            m = row["metrics"]
            print(
                f"  [{row['arm']}] offered={m['schema_count_offered']} "
                f"deferred={m['schema_count_deferred']} "
                f"schema_tokens={m['tool_schema_tokens']} "
                f"searches={m['model_searches']} "
                f"resolve_rate={m['resolve_rate']} "
                f"discoverability_failure={m['discoverability_failure_rate']} "
                f"cost={m['cost_usd']}"
            )
        print("DELTAS (deferred - eager):")
        for axis, row in report["deltas"].items():
            print(f"  {axis}: {row.get('delta')} (eager={row.get('eager')})")
        print("HONESTY:")
        for line in report["honesty"]:
            print(f"  - {line}")
    return 2 if report["verdict"] in {"INVALID_MULTI_VARIABLE", "UNSAFE"} else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
