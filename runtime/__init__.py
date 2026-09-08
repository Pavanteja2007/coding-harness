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
"""

__version__ = "0.1.0"


def __getattr__(name: str):
    # Lazy re-exports keep `import runtime` cheap; `python -m runtime.worker`
    # and scheduler spawning never pay for modules they don't use.
    if name == "call_model":
        from .model_router import call_model
        return call_model
    if name == "run":
        from .scheduler import run
        return run
    raise AttributeError(name)
