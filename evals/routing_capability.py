"""The routing/capability ablation (R2-13, item 5).

``runtime/ablation.py`` ablates ONE axis: price. A call is routed by
comparing numbers, the cheapest tier wins, and the report is a cost delta.
That is the right experiment for the mechanism that exists, and it is blind
to the axis R2-13 added: **whether a model can do the job at all.** A model
that cannot emit tool calls is not a cheap model, it is a model that cannot
run a tool-driven loop, and no amount of price comparison changes that.

So this module runs the SAME comparison with capability selection switched
as an independent variable, over a fixed table-driven task set, with the real
``runtime.model_router`` in the loop (the offline mock provider, zero network,
no credential read). The arms are:

| arm | config | what it isolates |
|---|---|---|
| ``price_only`` | (gate absent) | the pre-R2-13 behaviour: a static price ladder, and an unpriced model reads as the cheapest tier |
| ``capability`` | ``capability_gate: True`` | the shipped policy: an unpriced target is refused and a tool-incapable model is excluded, with a recorded reason |
| ``capability_strict_tools`` | ``capability_gate: True, capability_strict_tools: True`` | the same policy plus "tool support must be VERIFIED, not merely unknown" |

Every arm runs the identical case list through the identical router, and the
report answers three questions with observed receipts rather than with prose:

1. **Was the tool-incapable model excluded?** (and with which reason, from
   which arm.)
2. **Did the unpriced model stop reading as the cheapest tier?**
3. **Does the cost report distinguish "free" from "unknown"?** (``free`` is
   reachable only by a declared zero-price row.)

A REGRESSION here is not a failing assertion, it is a ``regressions`` row: a
behaviour the ``capability`` arm should have and did not. The report's
``verdict`` is ``CLEAN`` only when every expected capability behaviour was
observed in the arm that should show it AND the arm that should NOT show it
did not.

Usage::

    python -m evals.routing_capability --json
    python -m evals.routing_capability --arms capability,price_only
    python -m evals.routing_capability --out logs/evals/<ts>
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - path boot
    sys.path.insert(0, str(REPO_ROOT))

from runtime import mock_provider, model_capabilities, model_router  # noqa: E402

#: The arms, as Task.config fragments. Absent keys are the point: an arm with
#: no capability key must take the byte-identical pre-R2-13 path, so a
#: behaviour difference between arms is attributable to the declared key.
ARMS: Dict[str, Dict[str, Any]] = {
    "price_only": {},
    "capability": {"capability_gate": True},
    "capability_strict_tools": {
        "capability_gate": True,
        "capability_strict_tools": True,
    },
}


#: The fixed case set. Each case declares the tier table it routes over and
#: whether the call is tool-driven, so every arm routes the SAME economics and
#: the only difference is the capability policy.
@dataclass(frozen=True)
class Case:
    """One routing decision to make.

    Assumes ``tiers`` is a hint -> target mapping the router understands and
    ``tools`` is the provider-neutral schema list the call carries. ``expect``
    names the receipt(s) an arm must produce, which is what makes the report a
    measurement rather than a description.
    """

    name: str
    question: str
    tiers: Dict[str, Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    tool_driven: bool = False
    expect: Dict[str, Any] = field(default_factory=dict)
    allow_unpriced: bool = False


_BASH_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

#: The three cases that carry the round's claims. Model names are fictional and
#: registered through the REAL capability registry, so the ablation exercises
#: the shipping code path rather than a private table.
CASES: tuple = (
    Case(
        name="unpriced_cheap_tier_is_not_the_cheapest_tier",
        question=(
            "The cheap tier names a model with NO price row. Does the price "
            "ladder treat it as $0 (the pre-R2-13 lie), and does the "
            "capability arm refuse to route to it?"
        ),
        tiers={
            "easy": {"provider": "acme", "model": "acme-free-beacon"},
            "medium": {"provider": "openai", "model": "gpt-4o-mini"},
            "hard": {"provider": "openai", "model": "gpt-4o"},
        },
        expect={
            "price_only": {"selected": "acme-free-beacon", "unpriced_as_free": True},
            "capability": {"refused_unpriced": True, "selected": "gpt-4o-mini"},
        },
    ),
    Case(
        name="tool_incapable_model_is_excluded_from_a_tool_driven_loop",
        question=(
            "The cheap tier is the CHEAPEST model in the table and is declared "
            "unable to emit tool calls. Is it excluded regardless of price, and "
            "is the reason recorded?"
        ),
        tiers={
            "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
            "medium": {"provider": "openai", "model": "gpt-4o-mini"},
            "hard": {"provider": "openai", "model": "gpt-4o"},
        },
        tools=_BASH_TOOL,
        tool_driven=True,
        expect={
            "price_only": {"selected": "acme-cheap-no-tools"},
            "capability": {
                "refused_tools": True,
                "selected": "gpt-4o-mini",
                "routed_up": True,
            },
            "capability_strict_tools": {"refused_tools": True, "routed_up": True},
        },
    ),
    Case(
        name="a_declared_free_model_is_free_not_unknown",
        question=(
            "A model with a DECLARED zero-price row and full tool support. Is "
            "it reported as free (and therefore allowed through the gate) while "
            "an unpriced one is not?"
        ),
        tiers={
            "easy": {"provider": "acme", "model": "acme-declared-free"},
            "medium": {"provider": "openai", "model": "gpt-4o-mini"},
        },
        tools=_BASH_TOOL,
        tool_driven=True,
        expect={
            "capability": {"selected": "acme-declared-free", "price_state": "free"},
        },
    ),
    Case(
        name="an_explicitly_named_unpriced_model_is_not_routing",
        question=(
            "The caller pins an unpriced model. The gate must not refuse to "
            "ROUTE on it, and the bypass must be recorded rather than silent."
        ),
        tiers={
            "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
        },
        tools=_BASH_TOOL,
        tool_driven=True,
        expect={"capability": {"explicit_unpriced_bypass_recorded": True}},
    ),
    Case(
        name="nothing_eligible_refuses_loudly",
        question=(
            "Every tier is tool-incapable. Does the router fail closed with a "
            "named reason instead of quietly dialling a model that cannot work?"
        ),
        tiers={
            "easy": {"provider": "acme", "model": "acme-cheap-no-tools"},
            "medium": {"provider": "acme", "model": "acme-other-no-tools"},
        },
        tools=_BASH_TOOL,
        tool_driven=True,
        expect={"capability": {"raised": "tool_calling_unsupported"}},
    ),
)

#: The fictional-model capability declarations every arm shares. They go
#: through the SHIPPING registry (``register_capability``), so an arm that
#: "knows" a model only because this list declared it is a real statement
#: about the registry, not a private shortcut.
FIXTURE_CAPABILITIES: tuple = (
    {
        "provider": "acme",
        "model": "acme-free-beacon",
        "supports_tools": True,
        "supports_reasoning": False,
        "supports_streaming": True,
    },
    {
        "provider": "acme",
        "model": "acme-cheap-no-tools",
        "input_cost_per_million": 0.01,
        "output_cost_per_million": 0.02,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_streaming": True,
    },
    {
        "provider": "acme",
        "model": "acme-other-no-tools",
        "input_cost_per_million": 0.50,
        "output_cost_per_million": 1.50,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_streaming": True,
    },
    {
        "provider": "acme",
        "model": "acme-declared-free",
        "input_cost_per_million": 0.0,
        "output_cost_per_million": 0.0,
        "supports_tools": True,
        "supports_reasoning": False,
        "supports_streaming": True,
    },
)

MESSAGES = [{"role": "user", "content": "fix the failing test in app.py"}]


def _install_fixture_capabilities() -> None:
    """Register the fictional model rows and the mock responses.

    Assumes the registry may be reset first; a test that runs this twice must
    not accumulate duplicates, so it resets the caller-registered rows (the
    built-ins are never removable by design). The mock responses cover the
    BUILT-IN tier models too, because a capability escalation legitimately
    lands on one and the escalation path is the thing under test — a missing
    mock response there would read as an arm failure instead of as the
    escalation it is.
    """
    model_capabilities.reset_capability_registry()
    for row in FIXTURE_CAPABILITIES:
        model_capabilities.register_capability(row, source="registry")
    responses = {
        row["model"]: "OK from the offline mock provider"
        for row in FIXTURE_CAPABILITIES
    }
    for name in model_capabilities.MODEL_PRICES:
        responses.setdefault(name, "OK from the offline mock provider")
    mock_provider.install(responses)


def _run_case(case: Case, arm: str) -> Dict[str, Any]:
    """Run one case in one arm and return the observed receipts.

    Assumes the fixture capabilities are installed. Never raises: a refusal IS
    the observation for the "nothing eligible" case, so it is captured as
    ``raised``/``refused_reason`` rather than propagated into the report as a
    crash. An unexpected exception type is reported as ``error`` with its
    class name so a broken arm cannot masquerade as a clean refusal.
    """
    overrides = dict(ARMS.get(arm, {}))
    ctx: Dict[str, Any] = {
        "model_tiers": case.tiers,
        "use_mock_provider": True,
        "mock_responses": {row["model"]: "OK" for row in FIXTURE_CAPABILITIES},
        "adaptive_routing": True,
        "difficulty_estimator": "off",
    }
    ctx.update(overrides)
    if case.allow_unpriced:
        ctx["capability_allow_unpriced"] = True
    observed: Dict[str, Any] = {
        "case": case.name,
        "arm": arm,
        "question": case.question,
        "tool_driven": bool(case.tools) or bool(case.tool_driven),
        "config": dict(overrides),
    }
    ledger = Path(tempfile.gettempdir()) / (
        f"neo-r2-13-capability-{arm}-{case.name}.jsonl"
    )
    try:
        ledger.unlink()
    except OSError:
        pass
    explicit_model = case.name == ("an_explicitly_named_unpriced_model_is_not_routing")
    try:
        model_router.set_call_context(ctx, ledger)
        if explicit_model:
            model_router.call_model(
                MESSAGES, tools=case.tools, model="acme-free-beacon"
            )
        else:
            model_router.call_model(MESSAGES, difficulty_hint="easy", tools=case.tools)
    except model_capabilities.CapabilityRoutingRefused as refusal:
        observed["raised"] = refusal.reason
        observed["refusals"] = list(refusal.alternatives)
        observed["selected"] = None
        return observed
    except Exception as exc:
        observed["error"] = f"{type(exc).__name__}: {exc}"[:300]
        observed["selected"] = None
        return observed
    usage = model_router.get_last_usage()
    observed["selected"] = usage.get("model")
    observed["price_state"] = usage.get("price_state")
    observed["cost_usd"] = usage.get("cost_usd")
    observed["cost_priced"] = usage.get("cost_priced")
    observed["cost_source"] = usage.get("cost_source")
    receipt = usage.get("capability_gate") or {}
    observed["capability_gate_enabled"] = bool(receipt.get("enabled"))
    observed["refusal_count"] = int(receipt.get("refusal_count") or 0)
    observed["refusals"] = list(receipt.get("refusals") or [])
    observed["refusal_reasons"] = sorted(
        {str(row.get("reason")) for row in observed["refusals"]}
    )
    observed["routed_up"] = bool(receipt.get("routed_up"))
    observed["explicit_unpriced_bypass"] = bool(receipt.get("explicit_unpriced_bypass"))
    return observed


def _evaluate(observations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn observed receipts into checks and regressions.

    Assumes ``observations`` is one row per (case, arm). A check reads the
    receipt the arm produced; a missing receipt is a FAILURE, never a skip,
    because "the arm did not do the thing" and "the arm was not run" must not
    read the same way in a report.
    """
    by_case: Dict[str, Dict[str, Any]] = {}
    for row in observations:
        by_case.setdefault(str(row.get("case")), {})[str(row.get("arm"))] = row
    checks: List[Dict[str, Any]] = []
    regressions: List[Dict[str, Any]] = []

    def _check(name: str, arm: str, expected: Any, actual: Any) -> None:
        ok = expected == actual
        checks.append(
            {
                "case": name,
                "arm": arm,
                "expected": expected,
                "actual": actual,
                "ok": ok,
            }
        )
        if not ok:
            regressions.append(
                {
                    "case": name,
                    "arm": arm,
                    "expected": expected,
                    "actual": actual,
                    "detail": "behaviour the arm should have shown was not observed",
                }
            )

    for case in CASES:
        expectations = case.expect or {}
        for arm, want in expectations.items():
            row = by_case.get(case.name, {}).get(arm)
            if row is None:
                regressions.append(
                    {
                        "case": case.name,
                        "arm": arm,
                        "expected": dict(want),
                        "actual": None,
                        "detail": "arm produced no observation at all",
                    }
                )
                continue
            if "selected" in want:
                _check(case.name, arm, want["selected"], row.get("selected"))
            if "raised" in want:
                _check(case.name, arm, want["raised"], row.get("raised"))
            if want.get("refused_unpriced"):
                _check(
                    case.name,
                    arm,
                    True,
                    model_capabilities.REFUSAL_REASON_UNPRICED
                    in (row.get("refusal_reasons") or []),
                )
            if want.get("refused_tools"):
                _check(
                    case.name,
                    arm,
                    True,
                    model_capabilities.REFUSAL_REASON_TOOLS
                    in (row.get("refusal_reasons") or []),
                )
            if want.get("routed_up"):
                _check(case.name, arm, True, bool(row.get("routed_up")))
            if want.get("unpriced_as_free"):
                # The pre-R2-13 lie, pinned: the price ladder picks the model
                # that has no price row, and the cost report calls it $0.
                _check(case.name, arm, True, row.get("selected") == "acme-free-beacon")
                _check(case.name, arm, "unpriced", row.get("price_state"))
                _check(case.name, arm, False, row.get("cost_priced"))
            if want.get("price_state"):
                _check(case.name, arm, want["price_state"], row.get("price_state"))
            if want.get("explicit_unpriced_bypass_recorded"):
                _check(
                    case.name,
                    arm,
                    True,
                    bool(row.get("explicit_unpriced_bypass")),
                )
    # The gate must be OFF in the arm that declares no capability key, or the
    # ablation is comparing two identical configurations.
    for case in CASES:
        row = by_case.get(case.name, {}).get("price_only")
        if row is None:
            continue
        _check(case.name, "price_only", False, bool(row.get("capability_gate_enabled")))
    return {"checks": checks, "regressions": regressions}


def run_matrix(
    arms: Optional[List[str]] = None, *, cases: Optional[tuple] = None
) -> Dict[str, Any]:
    """Run every (case, arm) pair and return the report.

    Assumes nothing about the caller's process state: it installs the fixture
    capabilities and the mock responses, and each call runs in its own router
    context with its own ledger file so arms cannot contaminate each other.
    """
    selected = list(arms) if arms else list(ARMS)
    unknown = [arm for arm in selected if arm not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm(s): {', '.join(sorted(unknown))}")
    chosen = list(cases) if cases is not None else list(CASES)
    _install_fixture_capabilities()
    observations: List[Dict[str, Any]] = []
    for arm in selected:
        for case in chosen:
            observations.append(_run_case(case, arm))
    verdict_doc = _evaluate(observations)
    clean = not verdict_doc["regressions"]
    return {
        "kind": "r2_13_routing_capability_ablation",
        "arms": selected,
        "cases": [case.name for case in chosen],
        "observations": observations,
        **verdict_doc,
        "checks_run": len(verdict_doc["checks"]),
        "verdict": "CLEAN" if clean else "REGRESSIONS",
        "honesty": [
            "The router runs for real (offline mock provider, zero network, no "
            "credential read). Only the model RESPONSES are mocked; the target "
            "resolution, the capability screen, the price ladder and the cost "
            "report are the shipping code.",
            "The fictional acme models are registered through the real "
            "capability registry, so 'the router knew this model' is a fact "
            "about the registry and not a private shortcut.",
            "A capability exclusion is recorded as a refusal with a reason; an "
            "arm that fails to produce a receipt is a regression, never a skip.",
            "This measures WHICH MODEL GETS SELECTED and what the cost report "
            "says. It does NOT measure model quality, latency, or tokens: those "
            "need a live provider lane, which was not run.",
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Exit 0 on CLEAN, 2 on regressions or a bad selection.

    Assumes a normal argv. ``--json`` prints only the report, which is what a
    CI gate should parse; the human summary goes to stdout otherwise. The
    report is ALSO written under ``--out`` so the evidence survives the shell.
    """
    parser = argparse.ArgumentParser(
        description="Ablate capability selection against the price-only ladder."
    )
    parser.add_argument(
        "--arms", default=None, help="comma list or 'all' (default: every arm)"
    )
    parser.add_argument("--cases", default=None, help="comma list of case names")
    parser.add_argument(
        "--out", default=None, help="directory to write the report into"
    )
    parser.add_argument("--json", action="store_true", help="print the report only")
    args = parser.parse_args(argv)
    try:
        arms = None
        if args.arms:
            arms = list(ARMS) if args.arms == "all" else args.arms.split(",")
        cases = None
        if args.cases:
            wanted = {name.strip() for name in args.cases.split(",") if name.strip()}
            cases = tuple(case for case in CASES if case.name in wanted)
            if not cases:
                raise ValueError(f"no case matched {sorted(wanted)}")
        report = run_matrix(arms, cases=cases)
    except ValueError as exc:
        print(f"eval error: {exc}")
        return 2
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "routing_capability_report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(
            f"verdict={report['verdict']} arms={len(report['arms'])} "
            f"cases={len(report['cases'])} checks={report['checks_run']} "
            f"regressions={len(report['regressions'])}"
        )
        for row in report["observations"]:
            print(
                f"  [{row['arm']}] {row['case']}: selected={row.get('selected')} "
                f"price_state={row.get('price_state')} "
                f"raised={row.get('raised')} "
                f"refusals={','.join(row.get('refusal_reasons') or []) or '-'}"
            )
        for regression in report["regressions"]:
            print(
                f"  REGRESSION {regression['case']}/{regression['arm']}: "
                f"expected {regression['expected']!r} got {regression['actual']!r}"
            )
    return 0 if report["verdict"] == "CLEAN" else 2


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
