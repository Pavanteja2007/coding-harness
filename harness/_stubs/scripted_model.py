"""File-driven scripted model — the cross-process test hook (not a stub).

harness.deps loads this when the env var HARNESS_SCRIPTED_MODEL points at
a JSON file. In-process tests inject via deps.set_call_model (tests/
fake_model.py), but the runtime's scheduler/worker run the harness in
SUBPROCESSes where in-process injection cannot reach. A file + env var
crosses that boundary: the scheduler spawns workers, workers inherit the
env, the harness resolves this model instead of the real router. Zero
network, fully deterministic — the same scripts pattern as the e2e suite.

JSON file shape (mirrors tests/fake_model.ScriptedModel's constructor):

{
  "plan":   [ {"id": 1, "description": "...", "checkpoint": "...",
               "files_hint": ["..."]}, ... ],
  "scripts": { "1": [ ["bash command", "SUBMIT"],        # attempt 1 queue
                       ["other command", "SUBMIT"] ],    # attempt 2 (retry)
                "2": [ ... ] }
}

Scripts are consumed per (step, attempt): each new step session pops the
next attempt's queue. Never enable in production — it exists for tests,
demos, and the CLI's offline e2e path.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

_SESSION_START_MARKER = "Begin. Reply with exactly ONE bash command"


class ScriptedFileModel:
    """Callable fake for Boundary 2, scripted from a JSON file.

    Assumes the file's "plan" matches the planner prompt's schema and
    "scripts" maps step id -> list of per-attempt command queues. Raises
    OSError/ValueError at construction for a missing/unparseable file —
    a test hook must fail loudly, never silently fall back to a network
    call.
    """

    def __init__(self, spec_path: str) -> None:
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
        self.plan: List[Dict[str, Any]] = list(spec.get("plan") or [])
        self.scripts: Dict[int, List[List[str]]] = {
            int(k): [list(v) for v in vs]
            for k, vs in (spec.get("scripts") or {}).items()
        }
        self._queues: Dict[int, List[str]] = {}
        self._attempt_cursor: Dict[int, int] = {}
        self.call_count = 0

    # Boundary-2-compatible usage reporting (ModelClient reads this).
    def get_last_usage(self) -> Dict[str, object]:
        return {
            "model": "scripted-file",
            "provider": "fake",
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "tokens": 20,
            "cost_usd": 0.0001,
        }

    def __call__(
        self,
        messages: List[Dict[str, str]],
        difficulty_hint: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> str:
        self.call_count += 1
        system = next((m["content"] for m in messages
                       if m["role"] == "system"), "")
        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": self.plan})
        step_id = self._current_step(system)
        if self._is_session_start(messages) or step_id not in self._queues:
            self._queues[step_id] = list(self._next_script(step_id))
        queue = self._queues[step_id]
        if queue:
            return queue.pop(0)
        return "SUBMIT"

    # -- internals (same dispatch contract as tests/fake_model) ----------

    def _current_step(self, system: str) -> int:
        m = re.search(r"your step is #(\d+) of", system)
        return int(m.group(1)) if m else 0

    def _is_session_start(self, messages: List[Dict[str, str]]) -> bool:
        users = [m for m in messages if m["role"] == "user"]
        return bool(users) and _SESSION_START_MARKER in users[-1]["content"]

    def _next_script(self, step_id: int) -> Sequence[str]:
        attempts = self.scripts.get(step_id) or [["SUBMIT"]]
        cursor = self._attempt_cursor.get(step_id, 0)
        script = attempts[min(cursor, len(attempts) - 1)]
        self._attempt_cursor[step_id] = cursor + 1
        return script


def clear_env() -> None:
    """Remove the hook's env var (test teardown hygiene)."""
    os.environ.pop("HARNESS_SCRIPTED_MODEL", None)
