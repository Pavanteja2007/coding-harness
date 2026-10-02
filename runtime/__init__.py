"""Terminal 3 — runtime: concurrency, reliability, and model routing.

Submodules:
  config        — runtime-wide defaults + task.config key definitions
  fsutil        — Windows-safe atomic file primitives
  difficulty    — difficulty prediction feeding adaptive model routing
  mock_provider — deterministic offline model responses (tests, demos)
  model_router  — Boundary 2 `call_model` (litellm, cost/token ledger)
  approval      — human-in-the-loop approval gate (file protocol)
  checkpoint    — per-task resume bookkeeping
  serialize     — TaskResult <-> dict helpers
  fake_harness  — fake Boundary-3 `run_task` for fault-injection testing
  worker        — one-task worker process (entry: `python -m runtime.worker`)
  scheduler     — Boundary 6 `run(...)`: concurrent supervised task execution
  roles         — bounded role profiles for orchestration children
  worktrees     — isolated Git worktree lifecycle
  orchestration — finite DAG execution, claims, approvals, and recovery
  automation    — disabled-by-default schedule/webhook ingress
  planning      — bounded 2..12 step plan state with the replan policy
  subagents     — versioned agent definitions + the bounded task tool
  symbols       — AST symbol scopes and unclaimed-symbol edit detection
  latency       — the ONE litellm import choke point + per-call timing
"""

__version__ = "0.1.0"


def _start_litellm_preload() -> bool:
    """Begin the background provider-library import, if this process qualifies.

    P1/W1 (T3). ``import litellm`` measures **17.6-22.2 s** on this host, and
    it sits on the first REAL model call rather than at CLI startup — so
    ``neo --version`` is a healthy 0.78 s and the user's first prompt pays
    eighteen seconds. This is the "process start" half of the fix: the import
    starts here, in parallel with whatever the process does next, so the cost
    overlaps the user's own time instead of being charged to their prompt.

    Three properties this deliberately is NOT:

    * **Not a behaviour change.** ``runtime.latency.ensure_litellm`` is the
      one place any ``runtime/`` code imports litellm, and the lazy path *is*
      that function with no preload. A preload that fails is recorded and the
      first call imports it synchronously, exactly as before.
    * **Not unconditional.** ``auto`` mode requires an interactive process
      that is not a test session — see ``runtime.latency.preload_eligible``
      for why a test runner is excluded rather than trusted.
    * **Not on the critical path.** The call returns immediately; the work is
      on a daemon thread. A process that exits first simply never needed it.

    Returns whether a new preload was started. ``runtime.latency`` is imported
    lazily inside the function so ``import runtime`` does not pay for it.
    """
    try:
        from . import latency
    except Exception:  # pragma: no cover - latency is stdlib-only
        return False
    try:
        return bool(latency.start_preload(reason="runtime_import"))
    except Exception:  # pragma: no cover - a preload must never break an import
        return False


_start_litellm_preload()


def __getattr__(name: str):
    # Lazy re-exports keep `import runtime` cheap; `python -m runtime.worker`
    # and scheduler spawning never pay for modules they don't use.
    if name == "call_model":
        from .model_router import call_model

        return call_model
    if name == "run":
        from .scheduler import run

        return run
    if name == "Orchestrator":
        from .orchestration import Orchestrator

        return Orchestrator
    if name == "WorkflowSpec":
        from .orchestration import WorkflowSpec

        return WorkflowSpec
    if name == "AutomationIngress":
        from .automation import AutomationIngress

        return AutomationIngress
    if name == "SubagentSpawner":
        from .subagents import SubagentSpawner

        return SubagentSpawner
    if name == "build_plan":
        from .planning import build_plan

        return build_plan
    if name == "preload_state":
        from . import latency

        return latency.preload_state
    if name == "start_preload":
        from . import latency

        return latency.start_preload
    raise AttributeError(name)
