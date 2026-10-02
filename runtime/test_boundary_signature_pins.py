"""Runtime-local pins for boundary-signature discipline (P0/W2 T3).

The rule this module exists to make unforgettable: **a permissive signature is
not evidence of support.** A boundary that accepts ``**kwargs`` will accept an
optional keyword and then hand it to something that rejects it, which is a
run-killing ``TypeError`` rather than a degraded feature. So an optional
keyword is forwarded only when the callee EXPLICITLY NAMES it.

Two halves:

* the two live gates in ``harness/model_client.py`` (``_boundary_streams`` for
  ``stream``, ``_boundary_names`` for ``effort``) and the third in
  ``harness/agent_loop.py`` (``_verify_boundary_names`` for the verification
  rungs) — pinned BEHAVIOURALLY, by reading, because those files are another
  owner's and are not edited here;
* a STANDING AST pin over ``runtime/**``: every call site that forwards an
  optional keyword to a callable which arrived as a PARAMETER is enumerated and
  must be a recorded entry, with its kind re-verified. A new unguarded site
  fails; a recorded ``signature``-guarded site that loses its guard fails.

Requires no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Dict, FrozenSet, List, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_DIR = _REPO_ROOT / "runtime"

#: Attribute names that count as reading a callee's signature, plus the one
#: module-local helper that wraps one.
_GUARD_ATTRIBUTES: FrozenSet[str] = frozenset(
    {"signature", "getfullargspec", "co_varnames"}
)
_GUARD_NAMES: FrozenSet[str] = frozenset({"_probe_arity", "getfullargspec"})


# -- the two live harness gates -------------------------------------------


def _client() -> object:
    from harness.model_client import ModelClient
    from harness.trace import TraceLogger

    return ModelClient(TraceLogger(Path("logs/_pin_boundary")), {})


def _names(fn: object, keyword: str) -> bool:
    return _client()._boundary_names(fn, keyword)


def _streams(fn: object) -> bool:
    return _client()._boundary_streams(fn)


def test_a_kwargs_only_boundary_is_not_evidence_of_effort_support() -> None:
    """`_boundary_names`: presence of `**kwargs` must not forward a keyword."""

    def permissive(*args: object, **kwargs: object) -> str:
        return "ok"

    def declared(messages: object, step: str, *, effort: str = "") -> str:
        return "ok"

    assert _names(permissive, "effort") is False
    assert _names(declared, "effort") is True
    assert _names(declared, "never_declared") is False


def test_a_kwargs_only_boundary_is_not_evidence_of_streaming_support() -> None:
    """`_boundary_streams`: both `stream` AND `on_delta` must be named."""

    def permissive(*args: object, **kwargs: object) -> str:
        return "ok"

    def half_declared(messages: object, *, stream: bool = False) -> str:
        return "ok"

    def fully_declared(
        messages: object, *, stream: bool = False, on_delta: object = None
    ) -> str:
        return "ok"

    assert _streams(permissive) is False
    assert _streams(half_declared) is False
    assert _streams(fully_declared) is True


def test_an_uninspectable_callable_is_refused_rather_than_forwarded_to() -> None:
    """A boundary whose signature cannot be read must not be guessed at."""

    class Opaque:
        def __call__(self, *args: object) -> str:  # pragma: no cover - never called
            return "ok"

    opaque = Opaque()
    opaque.__signature__ = "not a signature"
    assert _names(opaque, "effort") is False
    assert _streams(opaque) is False


def test_the_real_runtime_boundary_names_every_keyword_the_gates_send() -> None:
    """Production must be unaffected: the gates only degrade for test doubles."""
    from runtime import model_router

    parameters = set(inspect.signature(model_router.call_model).parameters)
    for keyword in ("stream", "on_delta", "effort"):
        assert keyword in parameters, (
            f"runtime.model_router.call_model no longer names {keyword!r}, so "
            "_boundary_names/_boundary_streams would silently stop forwarding it "
            "on the PRODUCTION path"
        )
    assert _streams(model_router.call_model) is True
    assert _names(model_router.call_model, "effort") is True


def test_the_real_verify_boundary_names_every_rung_the_gate_forwards() -> None:
    """`execution.verify.verify` must name all three rungs, or Phase-10 is dark."""
    from execution import verify as verify_module
    from harness import agent_loop

    parameters = set(inspect.signature(verify_module.verify).parameters)
    for keyword in ("rung_config", "run_dir", "phase"):
        assert keyword in parameters, (
            f"execution.verify.verify no longer names {keyword!r}; the harness "
            "gates would degrade and the rung would be silently unrecorded"
        )
        assert agent_loop._verify_boundary_names(verify_module.verify, keyword) is True


def test_the_agent_loop_rung_gate_exists_and_is_the_same_rule() -> None:
    """`_verify_boundary_names` is the third gate, added for the verify rungs."""
    from harness import agent_loop

    gate = getattr(agent_loop, "_verify_boundary_names", None)
    assert callable(gate), (
        "harness.agent_loop._verify_boundary_names is missing; the rung keywords "
        "run_dir/phase are forwarded to execution.verify through it"
    )

    def permissive(*args: object, **kwargs: object) -> object:
        return {}

    def runged(
        repo: object,
        target: object = None,
        *,
        rung_config: object = None,
        run_dir: object = None,
        phase: object = None,
    ) -> object:
        return {}

    assert gate(permissive, "rung_config") is False
    assert gate(runged, "rung_config") is True
    assert gate(runged, "never_declared") is False


# -- the standing AST pin over runtime/** ---------------------------------


def _calls_forwarding_optional_keywords(
    tree: ast.AST, source: str
) -> List[Tuple[str, str, str, List[str]]]:
    """Return ``(enclosing_function, callee_param, lineno, keywords)`` triples.

    A candidate is a call whose callee resolves to a NAME that is a parameter of
    the enclosing function AND which passes at least one keyword or one
    ``*args``/``**kwargs`` unpacking. Positional-only calls are excluded: they
    cannot carry an optional keyword the callee might not declare.
    """
    found: List[Tuple[str, str, str, List[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in node.args.args + node.args.kwonlyargs}
        params.discard("self")
        params.discard("cls")
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if not isinstance(sub.func, ast.Name):
                continue
            if sub.func.id not in params:
                continue
            keywords = [kw.arg for kw in sub.keywords]
            unpacks = any(kw.arg is None for kw in sub.keywords)
            star_args = any(isinstance(a, ast.Starred) for a in sub.args)
            if not keywords and not unpacks and not star_args:
                continue
            found.append(
                (
                    node.name,
                    sub.func.id,
                    str(sub.lineno),
                    keywords or (["**kwargs"] if unpacks else ["*args"]),
                )
            )
    return found


def _reads_a_signature(function: ast.AST) -> bool:
    """Whether this function body reads a callee's declared signature."""
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in _GUARD_ATTRIBUTES:
            return True
        if isinstance(node, ast.Name) and node.id in _GUARD_NAMES:
            return True
    return False


def _guard_present(function: ast.AST, module_level: Dict[str, ast.AST]) -> bool:
    """Whether the enclosing function reaches a signature read, one hop deep."""
    if _reads_a_signature(function):
        return True
    called: Set[str] = set()
    for sub in ast.walk(function):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            called.add(sub.func.id)
    return any(
        name in module_level and _reads_a_signature(module_level[name])
        for name in called
    )


#: Every optional-keyword forwarding site in `runtime/**`, with its kind.
#:
#: ``signature`` — the enclosing function reads the callee's signature, so a
#: keyword the callee does not declare is never sent.
#:
#: ``contract`` — the keyword is NOT optional: it IS the function's request
#: shape, and the existing test pins that it is always sent. One entry today.
FORWARDING_SITES: Dict[Tuple[str, str], str] = {
    ("streaming.py", "stream_call"): "contract",
    ("model_capabilities.py", "_from_probe"): "signature",
    ("worker.py", "_call_run_task"): "signature",
}


def test_every_optional_keyword_forwarding_site_in_runtime_is_recorded() -> None:
    """The standing pin: a new boundary site cannot appear unclassified."""
    unrecorded: List[str] = []
    for path in sorted(_RUNTIME_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for function, callee, lineno, keywords in _calls_forwarding_optional_keywords(
            tree, source
        ):
            key = (path.name, function)
            if key not in FORWARDING_SITES:
                unrecorded.append(
                    f"{path.name}:{lineno} {function}() forwards {keywords} to its "
                    f"{callee!r} parameter and is not in FORWARDING_SITES"
                )
    assert unrecorded == []


def test_a_site_recorded_as_signature_guarded_still_reads_a_signature() -> None:
    """A guard that was deleted must fail, not silently downgrade the site."""
    unguarded: List[str] = []
    for path in sorted(_RUNTIME_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_level = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if FORWARDING_SITES.get((path.name, node.name)) != "signature":
                continue
            if not _guard_present(node, module_level):
                unguarded.append(f"{path.name}:{node.lineno} {node.name}")
    assert unguarded == []


def test_the_recorded_site_set_names_only_files_that_exist() -> None:
    """A renamed or deleted file must not leave a phantom pin behind."""
    existing = {path.name for path in _RUNTIME_DIR.glob("*.py")}
    assert {name for name, _ in FORWARDING_SITES} <= existing


def test_the_streaming_contract_sends_stream_even_to_a_permissive_double() -> None:
    """`stream=True` is the request shape, not an optional capability keyword.

    ``runtime/streaming.py`` is the ONE site in ``runtime/`` that forwards a
    keyword to an injected boundary without a signature read. It is recorded as
    ``contract`` because degrading it would be a different lie: the caller
    believed it streamed. The existing pin in ``tests/test_streaming_live.py``
    requires the keyword to be present, and this module requires the same, so
    the decision is visible in both directions instead of implicit.
    """
    from runtime import streaming

    seen: List[Dict[str, object]] = []

    def permissive(**kwargs: object) -> object:
        seen.append(dict(kwargs))
        return iter(())

    streaming.stream_call(permissive, {"model": "acme/pin"}, on_delta=lambda _d: None)
    assert seen[0]["stream"] is True


def test_the_probe_arity_helper_really_refuses_an_undeclared_arity() -> None:
    """`signature`-guarded entry: the guard is behavioural, not decorative."""
    from runtime import model_capabilities as mc

    calls: List[int] = []

    def zero_arg() -> int:
        calls.append(0)
        return 4096

    def three_arg(model: object, provider: object, api_base: object) -> int:
        calls.append(3)
        return 8192

    assert (
        mc.context_window_info(
            "acme/pin", provider="acme", probe=zero_arg, use_cache=False
        )["context_window"]
        == 4096
    )
    assert (
        mc.context_window_info(
            "acme/pin", provider="acme", probe=three_arg, use_cache=False
        )["context_window"]
        == 8192
    )
    assert calls == [0, 3]


def test_the_worker_boundary_probe_does_not_invent_a_log_root() -> None:
    """`signature`-guarded entry: a strict double must not get a keyword."""
    from runtime import worker

    seen: List[tuple] = []

    def strict(task: object) -> str:
        seen.append((task,))
        return "ok"

    def with_log_root(task: object, log_root: object) -> str:
        seen.append((task, log_root))
        return "ok"

    assert worker._call_run_task(strict, {"id": "pin"}, "logs") == "ok"
    assert len(seen[-1]) == 1
    assert worker._call_run_task(with_log_root, {"id": "pin"}, "logs") == "ok"
    assert len(seen[-1]) == 2


# -- the vocabulary the gates depend on ------------------------------------


def test_the_effort_status_vocabulary_is_closed_and_every_status_is_distinct() -> None:
    """A shared status string is a contract between the router and the receipt."""
    from runtime import model_capabilities as mc

    assert len(set(mc.EFFORT_STATUSES)) == len(mc.EFFORT_STATUSES)


def test_the_cache_status_vocabulary_separates_unreported_from_miss_and_unsupported() -> (
    None
):
    """The three non-decisions must be three words, not one."""
    from runtime import prompt_cache as pc

    assert len(set(pc.CACHE_STATUSES)) == len(pc.CACHE_STATUSES)
    assert pc.CACHE_UNREPORTED not in pc._DECIDED_STATUSES
    assert pc.CACHE_UNSUPPORTED not in pc._DECIDED_STATUSES
    assert pc.CACHE_MISS in pc._DECIDED_STATUSES


def test_the_quota_vocabulary_is_not_part_of_the_rate_limit_vocabulary() -> None:
    """Quota is not a rate limit, and the marker sets must not overlap."""
    from runtime import budget_governor as bg
    from runtime import provider_resilience as pr

    quota_markers: FrozenSet[str] = frozenset(getattr(pr, "QUOTA_MARKERS", ()))
    rate_markers: FrozenSet[str] = frozenset(getattr(pr, "_RATE_LIMIT_MARKERS", ()))
    assert quota_markers, "provider_resilience exposes no quota marker set"
    assert rate_markers, "provider_resilience exposes no rate-limit marker set"
    assert not quota_markers & rate_markers, sorted(quota_markers & rate_markers)
    assert bg.QUOTA_EXHAUSTED in bg.TERMINAL_KINDS
    assert bg.RATE_LIMITED not in bg.TERMINAL_KINDS
