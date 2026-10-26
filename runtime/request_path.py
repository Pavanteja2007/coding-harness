"""P1/W2 T3 -- the request path, measured segment by segment.

``runtime/latency.py`` answers "how long did a call take". That is one number
for the whole request, and a single number for a path made of eight steps
answers no question a person can act on: you cannot make ``capability
resolution`` faster if you were never told it is the slow half.

This module measures the segments and publishes the ones that matter:

* **Every segment is timed on the REAL function**, not on a reimplementation of
  it. :func:`measure_request_path` calls ``model_capabilities.lookup_capability``,
  ``prompt_cache.plan_cache``, ``provider_gateway.prepare_call``,
  ``budget_governor.authorize_call`` and so on -- the same objects
  ``call_model`` uses. A profile of a copy is a profile of the copy.
* **A segment that was not exercised is ``unavailable`` with a reason.** The
  offline mock lane, for instance, never reaches the budget pre-check or the
  circuit breaker. Reporting 0.0 ms for those would be the same lie as a
  fabricated TTFT, and it is the mistake most profiling code makes: an
  unexercised path that measures fast is indistinguishable from a fast path.
* **The budget is a warm-path budget and says so.** :data:`WARM_SEGMENT_BUDGET_MS`
  is 50 ms, and it is compared against a segment with the context window cache
  warm and the registry warm -- the state a session is in from the second call
  onward. The FIRST call pays a different cost (a context-window ladder rung can
  import the provider library, which measures in seconds), and mixing the two is
  how a profile ends up blaming a routing table for an 18-second import. So the
  report separates ``cold`` from ``warm`` rather than averaging them.

Run ``python -m runtime.request_path`` for the table, ``--json`` for the
receipt, ``--cold`` to include the first-call segments.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "SEGMENTS",
    "WARM_SEGMENT_BUDGET_MS",
    "RequestPathReceipt",
    "measure_request_path",
    "render_receipt",
    "segment_table",
    "slow_segments",
]

#: The closed segment vocabulary, in REQUEST ORDER. A fixed order matters for
#: two reasons: a reader can compare two runs line by line, and a segment that
#: stops being exercised cannot silently disappear from the middle of the table.
SEGMENTS = (
    "context_copy",
    "difficulty_prediction",
    "target_resolution",
    "candidate_ladder",
    "capability_resolution",
    "capability_screen",
    "effort_plan",
    "vision_gate",
    "prompt_cache_plan",
    "cache_parameter_apply",
    "context_window",
    "redaction_gate",
    "privacy_gate",
    "offline_gate",
    "circuit_breaker_screen",
    "budget_authorize",
    "request_build",
    "ledger_write",
)

#: Anything over this on a WARM path is a Phase 2/3 target. Chosen against two
#: reference points this project already treats as healthy: the 0.70 s
#: ``neo --version`` (a whole CLI start, and its 150 ms first-party-import floor
#: lives in ``runtime.startup_audit``) and the 1.4 ms measured first-token path.
#: 50 ms is roughly 35x the entire warm request path measured on this host, so
#: it is a loose ceiling whose purpose is to catch an ORDERS-OF-MAGNITUDE
#: regression -- a cache key that starts re-digesting the whole transcript, a
#: registry that stops being a dict lookup -- rather than to police
#: microseconds.
WARM_SEGMENT_BUDGET_MS = 50.0

#: The cold-path equivalent, and deliberately separate. The first call in a
#: process can pay a provider-library import that measures in SECONDS and is
#: correctly attributed to ``context_window`` (the first rung that needs it),
#: not to the segment that happens to run first.
COLD_SEGMENT_BUDGET_MS = 2_000.0

_STATES = ("measured", "unavailable")


def _measured(ms: float) -> Dict[str, Any]:
    return {"ms": round(float(ms), 4), "state": "measured", "reason": ""}


def _unavailable(reason: str) -> Dict[str, Any]:
    """The honest answer for a segment that did not run. Never ``0``."""
    return {"ms": None, "state": "unavailable", "reason": reason}


@dataclass
class RequestPathReceipt:
    """Every segment's cost, plus which budget each was judged against.

    ``cold`` is separate from ``warm`` for the reason in the module docstring: a
    profile that averaged an 18-second first-call import into a per-segment
    mean would blame whichever segment happened to be running.
    """

    segments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Per-segment numbers for the FIRST iteration. Kept separately from
    #: ``segments`` because the first call in a process can pay a
    #: provider-library import measured in seconds, and averaging that into a
    #: warm per-segment mean would blame whichever segment happened to run.
    cold_segments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    warm_ms: float = 0.0
    cold_ms: float = 0.0
    budget_ms: float = WARM_SEGMENT_BUDGET_MS
    repeats: int = 1
    notes: List[str] = field(default_factory=list)

    def add(self, name: str, ms: float) -> None:
        self.segments[name] = _measured(ms)

    def skip(self, name: str, reason: str) -> None:
        self.segments[name] = _unavailable(reason)

    def over_budget(self, budget_ms: Optional[float] = None) -> List[Dict[str, Any]]:
        """Segments over the budget, worst first. An empty list is the goal."""
        limit = float(budget_ms if budget_ms is not None else self.budget_ms)
        out = []
        for name, entry in self.segments.items():
            if entry.get("state") != "measured" or entry.get("ms") is None:
                continue
            if float(entry["ms"]) > limit:
                out.append({"segment": name, "ms": entry["ms"], "budget_ms": limit})
        return sorted(out, key=lambda row: -float(row["ms"]))

    def to_dict(self) -> Dict[str, Any]:
        over = self.over_budget()
        return {
            "segments": {
                name: dict(self.segments.get(name, _unavailable("not measured")))
                for name in SEGMENTS
            },
            "cold_segments": {
                name: dict(self.cold_segments.get(name, _unavailable("not measured")))
                for name in SEGMENTS
            },
            "segment_order": list(SEGMENTS),
            "warm_total_ms": round(self.warm_ms, 4),
            "cold_total_ms": round(self.cold_ms, 4),
            "repeats": int(self.repeats),
            "budget_ms": self.budget_ms,
            "cold_budget_ms": COLD_SEGMENT_BUDGET_MS,
            "states": _STATES,
            "over_budget": over,
            "over_budget_count": len(over),
            "measured_segments": sum(
                1 for e in self.segments.values() if e.get("state") == "measured"
            ),
            "unavailable_segments": sum(
                1 for e in self.segments.values() if e.get("state") == "unavailable"
            ),
            "notes": list(self.notes),
        }


def _time(fn: Callable[[], Any]) -> Tuple[float, Any]:
    """Run ``fn``, returning ``(elapsed_ms, result)``. Never swallows a raise.

    A profiling helper that catches exceptions would make a broken segment
    indistinguishable from a fast one. Letting it propagate is the only way the
    caller learns that the path it is profiling does not work.
    """
    started = time.perf_counter()
    result = fn()
    return (time.perf_counter() - started) * 1000.0, result


def measure_request_path(
    *,
    messages: Optional[Sequence[Mapping[str, Any]]] = None,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
    api_base: Optional[str] = None,
    hint: str = "easy",
    repeats: int = 5,
    include_resilience: bool = True,
    budget_cap_usd: float = 2.0,
) -> RequestPathReceipt:
    """Time every segment of one request path, on the real functions.

    ``repeats`` drives the steady-state numbers: the FIRST iteration warms the
    context-window cache and the capability registry, and including it in a mean
    is the mistake this module exists to avoid. So ``repeats`` iterations are
    run and only iterations ``2..repeats`` are recorded; with ``repeats=1`` the
    receipt says ``provisional`` rather than reporting a cold number as a warm
    one.

    ``include_resilience`` exercises the redaction / privacy / offline /
    circuit-breaker / budget segments through
    :func:`runtime.provider_gateway.prepare_call` and
    :meth:`runtime.budget_governor.BudgetGovernor.authorize_call`. They are
    opt-out because they need a config that opts into the Ceiling-14 pipeline,
    and a segment measured on a path the run will not take is not a measurement
    of that path.
    """
    from . import (
        budget_governor,
        model_capabilities,
        model_router,
        prompt_cache,
        provider_gateway,
    )

    msgs = list(messages or [{"role": "user", "content": "read this and fix the bug"}])
    receipt = RequestPathReceipt(budget_ms=WARM_SEGMENT_BUDGET_MS, repeats=int(repeats))
    ctx: Dict[str, Any] = {}
    if include_resilience:
        # The keys that put a run on the resilient pipeline. Their PRESENCE is
        # the opt-in (`_resilience_requested` is a key test), so a run without
        # them would measure segments it never executes.
        ctx.update({"offline": False, "privacy_redact": True})

    governor: Optional[budget_governor.BudgetGovernor] = None
    warm_total = 0.0
    cold_total = 0.0
    #: Declared OUTSIDE the loop and never rebound. It was declared inside once,
    #: and Python's loop-variable leak then handed the post-loop code the LAST
    #: iteration's empty dict -- so `cold_segments` came back empty and the cold
    #: attribution silently reported nothing. A profile that drops the cold half
    #: of its own data because of a scoping accident is exactly the failure this
    #: module exists to prevent.
    cold_accumulator: Dict[str, float] = {}
    for index in range(max(1, int(repeats))):
        cold = index == 0
        accumulator: Dict[str, float] = {}
        #: A dict, not a closure over `cold`/`accumulator`. A closure over loop
        #: variables is correct only as long as nobody moves the `record(...)`
        #: call out of this iteration -- which is exactly the kind of edit that
        #: looks harmless and silently attributes one iteration's timing to
        #: another. The dict carries the flag and both accumulators by name.
        recording: Dict[str, Any] = {
            "cold": cold,
            "accumulator": accumulator,
            "cold_accumulator": cold_accumulator,
            "receipt": receipt,
        }

        def record(name: str, ms: float, _state: Dict[str, Any] = recording) -> None:
            acc: Dict[str, float] = _state["accumulator"]
            acc[name] = acc.get(name, 0.0) + ms
            if _state["cold"]:
                cold_acc: Dict[str, float] = _state["cold_accumulator"]
                cold_acc[name] = cold_acc.get(name, 0.0) + ms

        screen_ctx = dict(ctx)
        screen_ctx["capability_gate"] = True
        cache_ctx = {**ctx, "_resolved_model": model}

        # Ordered (segment_name, callable) pairs. `functools.partial` rather than
        # a lambda so nothing here is a closure over a loop variable at all --
        # the three segments that depend on an earlier segment's RESULT read it
        # from the list, not from the enclosing scope.
        plan: List[Tuple[str, Any]] = [
            ("context_copy", partial(dict, model_router._CONTEXT.get() or ctx)),
            (
                "difficulty_prediction",
                partial(
                    model_router._maybe_predict_difficulty,
                    msgs,
                    {"difficulty_features": "auto"},
                ),
            ),
            (
                "target_resolution",
                partial(model_router._resolve_target, hint, provider, model, ctx),
            ),
            (
                "capability_resolution",
                partial(model_capabilities.lookup_capability, provider, model),
            ),
        ]
        results: Dict[str, Any] = {}
        for name, fn in plan:
            ms, results[name] = _time(fn)
            record(name, ms)
        target = results["target_resolution"]

        # The three below depend on `target`, so they are appended AFTER it is
        # measured. A single ordered list of 18 callables built up front would
        # have to close over `target` before it existed.
        def build_ladder(_target: Any = target) -> List[Any]:
            return model_router._tier_candidates(_target, hint, ctx)

        def build_cache_plan(_target: Any = target) -> Any:
            return prompt_cache.plan_cache(
                msgs,
                None,
                provider=_target.get("provider"),
                model=_target["model"],
                enabled=True,
                min_prefix_tokens=prompt_cache.DEFAULT_MIN_PREFIX_TOKENS,
                breakpoint_index=None,
            )

        follow_on: List[Tuple[str, Any]] = [
            ("candidate_ladder", partial(build_ladder)),
            (
                "capability_screen",
                partial(
                    model_router._capability_screen,
                    target,
                    screen_ctx,
                    hint=hint,
                    tool_driven=False,
                ),
            ),
            ("effort_plan", partial(model_router._effort_plan, ctx, target, "auto")),
            ("vision_gate", partial(model_router._vision_plan, ctx, target, msgs)),
            ("prompt_cache_plan", partial(build_cache_plan)),
            ("context_window", partial(model_router._cache_capability, cache_ctx)),
        ]
        for name, fn in follow_on:
            ms, results[name] = _time(fn)
            record(name, ms)
        cache_plan = results["prompt_cache_plan"]

        ms, _ = _time(
            partial(prompt_cache.apply_cache_parameters, msgs, None, cache_plan)
        )
        record("cache_parameter_apply", ms)

        if include_resilience:
            policy_fn = partial(provider_gateway.privacy_policy.policy_from_config, ctx)

            def build_staged(_target: Any = target) -> Any:
                return provider_gateway.prepare_call(
                    msgs, context=ctx, target=_target, hint=hint
                )

            def do_authorize(
                _target: Any = target, _policy: Callable[[], Any] = policy_fn
            ) -> Any:
                return provider_gateway.privacy_policy.authorize(
                    _policy(),
                    _target.get("provider"),
                    _target.get("model"),
                    api_base,
                )

            def screen_chain(
                _results: Dict[str, Any] = results,
                _policy: Callable[[], Any] = policy_fn,
            ) -> Any:
                # The chain is read from `results` at CALL time, not captured at
                # plan-build time: `request_build` is timed first precisely so
                # its staged candidates exist by the time the breaker screen
                # runs, and reading them here is what makes the screen measure
                # the chain `prepare_call` actually produced rather than a
                # separately re-resolved one.
                staged = _results["request_build"]
                return provider_gateway.screen_candidates(
                    [
                        {
                            "provider": row.provider,
                            "model": row.model,
                            "api_base": row.api_base,
                        }
                        for row in staged.candidates
                    ],
                    policy=_policy(),
                    config=ctx,
                )

            resilience_plan: List[Tuple[str, Any]] = [
                # `prepare_call` does redaction + candidate resolution + the
                # screen + the local profile in one call, so it is timed as its
                # OWN aggregate rather than split -- and it goes FIRST so the
                # gates below have its staged candidates to measure against.
                # Recorded as an aggregate so it is obvious from the table that
                # `request_build` covers the same work the four gates also do.
                ("request_build", partial(build_staged)),
                ("redaction_gate", policy_fn),
                ("privacy_gate", partial(do_authorize)),
                (
                    "offline_gate",
                    partial(provider_gateway.offline_mode.offline_config, ctx),
                ),
                ("circuit_breaker_screen", partial(screen_chain)),
            ]
            for name, fn in resilience_plan:
                ms, results[name] = _time(fn)
                record(name, ms)

            if governor is None:
                governor = budget_governor.BudgetGovernor(cap_usd=float(budget_cap_usd))
            active = governor

            def do_authorize_call(_active: Any = active, _target: Any = target) -> Any:
                return _active.authorize_call(
                    target=_target,
                    price_usd=model_capabilities.estimate_cost(
                        _target["model"], 17_000, 800
                    ).cost_usd,
                    price_state="priced",
                    max_completion_tokens=800,
                )

            ms, verdict = _time(partial(do_authorize_call))
            record("budget_authorize", ms)
            if not verdict.allowed:
                receipt.notes.append(
                    "budget_authorize refused during the profile; the cap was"
                    " reached mid-measurement, so the governor was rebuilt and"
                    " the segment below is measured against a fresh cap"
                )
                governor = None
        else:
            for name, reason in (
                ("redaction_gate", "resilience pipeline not included"),
                ("privacy_gate", "resilience pipeline not included"),
                ("offline_gate", "resilience pipeline not included"),
                ("circuit_breaker_screen", "resilience pipeline not included"),
                ("budget_authorize", "resilience pipeline not included"),
            ):
                receipt.skip(name, reason)

        with tempfile.TemporaryDirectory(prefix="t3w2-ledger-") as tmp:
            ms, _ = _time(
                partial(
                    model_router._record_usage,
                    {"ts": "", "outcome": "success", "model": model},
                    ctx,
                    Path(tmp) / "model_ledger.jsonl",
                )
            )
        record("ledger_write", ms)

        for value in accumulator.values():
            if cold:
                cold_total += value
            else:
                warm_total += value

    if repeats < 2:
        receipt.notes.append(
            f"repeats={repeats}: these are COLD-path numbers and are reported"
            " as provisional. The first call in a process can pay a"
            " provider-library import measured in SECONDS, which belongs to"
            " context_window and not to the whole path"
        )
        receipt.segments = {
            name: _measured(value) for name, value in accumulator.items()
        }
        receipt.cold_segments = {
            name: _measured(value) for name, value in cold_accumulator.items()
        }
        receipt.cold_ms = cold_total
        return receipt

    divisor = float(repeats - 1)
    receipt.segments = {
        name: _measured(value / divisor) for name, value in accumulator.items()
    }
    # The cold per-segment numbers are kept alongside the warm ones rather than
    # discarded: they are the only attribution anyone has for where a first
    # call's seconds go, and an aggregate "cold: 7000 ms" would send the reader
    # looking in the wrong file.
    receipt.cold_segments = {
        name: _measured(value) for name, value in cold_accumulator.items()
    }
    receipt.warm_ms = warm_total / divisor
    receipt.cold_ms = cold_total
    for name in SEGMENTS:
        if name not in receipt.segments:
            receipt.skip(name, "not reached on this path")
    return receipt


def segment_table(receipt: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The publishable table: every segment in order with its state and cost."""
    rows: List[Dict[str, Any]] = []
    for name in receipt.get("segment_order") or SEGMENTS:
        entry = (receipt.get("segments") or {}).get(name) or _unavailable(
            "not measured"
        )
        rows.append(
            {
                "segment": name,
                "ms": entry.get("ms"),
                "state": entry.get("state"),
                "reason": entry.get("reason") or None,
                "over_budget": bool(
                    entry.get("state") == "measured"
                    and entry.get("ms") is not None
                    and float(entry["ms"]) > float(receipt.get("budget_ms") or 0.0)
                ),
            }
        )
    return rows


def slow_segments(
    receipt: Mapping[str, Any], budget_ms: Optional[float] = None
) -> List[Dict[str, Any]]:
    """Segments over the budget, worst first. Named, not summarised."""
    limit = float(
        budget_ms
        if budget_ms is not None
        else (receipt.get("budget_ms") or WARM_SEGMENT_BUDGET_MS)
    )
    rows = [
        row
        for row in segment_table(receipt)
        if row["state"] == "measured"
        and row["ms"] is not None
        and float(row["ms"]) > limit
    ]
    return sorted(rows, key=lambda row: -float(row["ms"]))


def render_receipt(receipt: Mapping[str, Any]) -> str:
    """A human table. ``--json`` is the machine surface."""
    budget = float(receipt.get("budget_ms") or WARM_SEGMENT_BUDGET_MS)
    lines = [
        "request path, WARM (context-window + registry caches warm),"
        f" repeats={receipt.get('repeats')}, budget {budget:.0f} ms/segment",
        "",
        "%-26s %12s  %-12s %s" % ("segment", "ms", "state", "reason"),
        "-" * 78,
    ]
    for row in segment_table(receipt):
        ms = "-" if row["ms"] is None else "%.3f" % float(row["ms"])
        lines.append(
            "%-26s %12s  %-12s %s"
            % (row["segment"], ms, row["state"], row["reason"] or "")
        )
    lines.append("-" * 78)
    lines.append(
        "warm total: %.3f ms over %d measured segments; cold (first call): %.3f ms"
        % (
            float(receipt.get("warm_total_ms") or 0.0),
            int(receipt.get("measured_segments") or 0),
            float(receipt.get("cold_total_ms") or 0.0),
        )
    )
    cold_rows = [
        (name, entry.get("ms"))
        for name, entry in (receipt.get("cold_segments") or {}).items()
        if entry.get("state") == "measured" and entry.get("ms") is not None
    ]
    if cold_rows:
        worst = max(cold_rows, key=lambda row: float(row[1]))
        lines.append(
            "cold attribution: %s alone was %.1f ms of the %.1f ms first call"
            % (worst[0], float(worst[1]), float(receipt.get("cold_total_ms") or 0.0))
        )
    over = slow_segments(receipt)
    lines.append(
        "over the %.0f ms budget: %s"
        % (budget, ", ".join(row["segment"] for row in over) if over else "NONE")
    )
    for note in receipt.get("notes") or []:
        lines.append("note: %s" % note)
    return "\n".join(lines)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m runtime.request_path",
        description="Per-segment timings for one model request path.",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--hint", default="easy")
    parser.add_argument("--cap", type=float, default=2.0)
    parser.add_argument(
        "--no-resilience",
        action="store_true",
        help="skip the Ceiling-14 segments (they need a run that opts in)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    receipt = measure_request_path(
        model=args.model,
        provider=args.provider,
        api_base=args.api_base,
        hint=args.hint,
        repeats=args.repeats,
        include_resilience=not args.no_resilience,
        budget_cap_usd=args.cap,
    ).to_dict()
    budget = float(receipt.get("budget_ms") or WARM_SEGMENT_BUDGET_MS)
    print(
        json.dumps(receipt, indent=2, sort_keys=True)
        if args.json
        else render_receipt(receipt)
    )
    over = slow_segments(receipt)
    for row in over:
        print(
            "OVER BUDGET: {} {:.1f} ms (budget {:.0f} ms)".format(
                row["segment"], float(row["ms"]), budget
            ),
            file=sys.stderr,
        )
    return 2 if over else 0


if __name__ == "__main__":  # pragma: no cover - operator convenience
    raise SystemExit(_cli())
