"""Dependency resolution — the single swap point for cross-module boundaries.

The harness MUST call other modules only through the contracts in
INTERFACES.md. Until the real modules exist, we call local stubs with the
exact contract signatures. This module tries the real module first and
falls back to the stub, so when Terminal 2 (execution) and Terminal 3
(runtime) land their implementations, no harness code needs to change.

The production path resolves real boundary modules first. A local stub is
selected only when the boundary package itself is absent and the caller sets
HARNESS_USE_STUBS=1 (or HARNESS_ALLOW_STUB_FALLBACK=1). Import failures from
inside a real boundary are never hidden by fallback.
"""

import os
from typing import Any, Callable, Optional

from shared.types import ExecutionResult, VerificationResult

_STUB_ENV_NAMES = ("HARNESS_USE_STUBS", "HARNESS_ALLOW_STUB_FALLBACK")


def _stub_fallback_allowed() -> bool:
    """Return whether the explicit development stub opt-in is enabled."""
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in _STUB_ENV_NAMES
    )


def _boundary_missing(exc: ModuleNotFoundError, module: str) -> bool:
    """Return whether an import error means the boundary package is absent."""
    name = getattr(exc, "name", "") or ""
    return name == module or name.startswith(module + ".")


# Contract signatures (see INTERFACES.md):
ExecuteSandboxedFn = Callable[[str, str, int], ExecutionResult]
CallModelFn = Callable[..., str]

_call_model_override: Optional[CallModelFn] = None
_execute_sandboxed_override: Optional[ExecuteSandboxedFn] = None
# HARNESS_SCRIPTED_MODEL spec path -> cached ScriptedFileModel instance
# (see get_call_model for why the cache exists).
_scripted_model_cache: "dict[str, Any]" = {}


def get_execute_sandboxed() -> ExecuteSandboxedFn:
    """Return the current execute_sandboxed callable (Boundary 1).

    Resolution order: test/runtime override -> execution.sandbox (Terminal 2,
    real) -> harness._stubs.sandbox (local subprocess stub). Assumes the
    returned callable matches the contract signature exactly.
    """
    if _execute_sandboxed_override is not None:
        return _execute_sandboxed_override
    try:
        from execution.sandbox import execute_sandboxed  # type: ignore

        return execute_sandboxed
    except ModuleNotFoundError as exc:
        if not (_stub_fallback_allowed() and _boundary_missing(exc, "execution")):
            raise
        from harness._stubs.sandbox import execute_sandboxed

        return execute_sandboxed


def get_call_model() -> CallModelFn:
    """Return the current call_model callable (Boundary 2).

    Resolution order: override -> runtime.model_router (Terminal 3, real)
    -> harness._stubs.model_router (single hardcoded provider via litellm).
    The override may be set via set_call_model() OR, for cross-process
    scenarios (the runtime's scheduler/worker spawn the harness in
    subprocesses where in-process injection cannot reach), via the
    HARNESS_SCRIPTED_MODEL env var pointing at a scripted-model JSON spec
    (harness/_stubs/scripted_model.py — a deterministic test hook).
    """
    if _call_model_override is not None:
        return _call_model_override
    spec_path = os.environ.get("HARNESS_SCRIPTED_MODEL")
    if spec_path:
        # One instance per spec path per process: ModelClient re-resolves
        # this callable on every model call, and the scripted model holds
        # per-run queue state that must survive across calls within a task.
        # (Each runtime worker IS its own process, so per-process caching
        # keeps concurrent tasks isolated; in-process tests should use
        # set_call_model instead of the env var.)
        global _scripted_model_cache
        cached = _scripted_model_cache.get(spec_path)
        if cached is None:
            from harness._stubs.scripted_model import ScriptedFileModel

            cached = ScriptedFileModel(spec_path)
            _scripted_model_cache[spec_path] = cached
        return cached  # type: ignore[return-value]
    try:
        from runtime.model_router import call_model  # type: ignore

        return call_model
    except ModuleNotFoundError as exc:
        if not (_stub_fallback_allowed() and _boundary_missing(exc, "runtime")):
            raise
        from harness._stubs.model_router import call_model

        return call_model


def get_verify() -> Callable[..., VerificationResult]:
    """Return the real verification boundary or an explicit dev stub.

    The default path never converts an import failure from inside
    execution.verify into a local subprocess verifier. Set
    HARNESS_USE_STUBS=1 only in a deliberately stubbed development
    environment where the execution package itself is absent.
    """
    try:
        from execution.verify import verify  # type: ignore

        return verify
    except ModuleNotFoundError as exc:
        if not (_stub_fallback_allowed() and _boundary_missing(exc, "execution")):
            raise
        from harness._stubs.verify import verify

        return verify


def get_code_graph_factory() -> Optional[Callable[..., Any]]:
    """Return memory.code_graph.CodeGraph if importable, else None.

    Used by harness.retrieval's structural mode (Phase 2). Returns None
    (never raises) when Terminal 4's memory module is absent — the caller
    degrades to dumb grep retrieval. Assumes the returned callable accepts
    (repo_path, root=...) per memory.code_graph.CodeGraph's public API.
    """
    try:
        from memory.code_graph import CodeGraph  # type: ignore

        return CodeGraph
    except ImportError:
        return None


def get_decision_store_factory() -> Optional[Callable[..., Any]]:
    """Return memory.decision_store.open_default_store if importable, else
    None (never raises).

    Used by the planner's decision-memory query (harness.decision_memory).
    Returns None when Terminal 4's memory module is absent — the caller
    degrades to planning without memory (trace event, never a crash).
    Assumes the returned zero-arg callable opens the shared store at
    memory.paths.decisions_db_path().
    """
    try:
        from memory.decision_store import open_default_store  # type: ignore

        return open_default_store
    except ImportError:
        return None


def set_execute_sandboxed(fn: Optional[ExecuteSandboxedFn]) -> None:
    """Inject a fake execute_sandboxed (tests / runtime). None clears it."""
    global _execute_sandboxed_override
    _execute_sandboxed_override = fn


def set_call_model(fn: Optional[CallModelFn]) -> None:
    """Inject a fake call_model (tests / runtime). None clears it.

    The fake must accept the Boundary 2 signature: (messages, difficulty_hint,
    provider, model, api_key) -> str.
    """
    global _call_model_override
    _call_model_override = fn


def reset_overrides() -> None:
    """Clear all injected overrides (test teardown)."""
    global _call_model_override, _execute_sandboxed_override
    _call_model_override = None
    _execute_sandboxed_override = None


def _unused(_: Any) -> None:  # pragma: no cover - keeps linters honest
    """Placeholder so Callable imports are not flagged; remove me never."""
