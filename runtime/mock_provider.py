"""Deterministic offline model responses for tests and demos.

A call_model back-end that makes no network requests: it replies with
per-model canned strings and charges synthetic token/cost numbers to the
ledger. Enabled per-task via config["use_mock_provider"] = True. Assumes
one shared mock response table per run (thread-safe for concurrent reads).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, Optional

_LOCK = threading.Lock()
# Populated by install(); maps model name -> canned assistant content.
_RESPONSES: Dict[str, str] = {}
# Optional callable: (messages, model) -> content; wins over _RESPONSES.
_DYNAMIC: Optional[Any] = None


def install(responses: Optional[Dict[str, str]] = None, dynamic: Optional[Any] = None) -> None:
    """Point the mock at canned responses (and optionally a callable).

    Assumes: responses keys are the model names the router will select
    (e.g. "gpt-4o-mini"); dynamic, if given, is a callable taking
    (messages, model) and returning the content string.
    """
    global _RESPONSES, _DYNAMIC
    with _LOCK:
        _RESPONSES = dict(responses or {})
        _DYNAMIC = dynamic


def reset() -> None:
    """Clear all mock state (test teardown)."""
    global _RESPONSES, _DYNAMIC
    with _LOCK:
        _RESPONSES = {}
        _DYNAMIC = None


def is_active() -> bool:
    """True if the mock has any responses installed."""
    with _LOCK:
        return bool(_RESPONSES) or _DYNAMIC is not None


def synthesize(model: str, messages: list) -> Optional[str]:
    """Return the canned/dynamic response for ``model``, or None.

    Assumes messages is an OpenAI-style list; only the dynamic callable
    inspects it. Never raises on its own account (a raising dynamic
    callable is swallowed and treated as "no response").
    """
    with _LOCK:
        dynamic = _DYNAMIC
        responses = dict(_RESPONSES)
    if dynamic is not None:
        try:
            content = dynamic(messages, model)
            if content is not None:
                return str(content)
        except Exception:
            pass
    return responses.get(model)


# ---------------------------------------------------------------------------
# Scripted-harness-model dispatch (worker-process friendly)
# ---------------------------------------------------------------------------

# Mirrors tests/fake_model.ScriptedModel's prompt dispatch, but from a
# plain-dict "script spec" so it can ride through Task.config into a
# subprocess worker (callables can't cross the process boundary). This is
# what runtime.stress's real-harness mode uses to drive the REAL
# harness.core.run_task deterministically: the planner/step prompts are
# genuinely generated and routed, only the model reply is scripted.
_PLANNER_MARKER = "planning a bug fix"
_SESSION_MARKER = "Begin. Reply with exactly ONE bash command"


def make_scripted_model_fn(script_spec: Dict[str, Any]):
    """Build a dynamic responder from a JSON-serializable script spec.

    Spec shape (same semantics as tests/fake_model.ScriptedModel):
      {"plan": [{"id": 1, "description": ..., "checkpoint": ...}, ...],
       "scripts": {"<step_id>": ["cmd1", "SUBMIT", ...]}}
    The step script repeats cyclically for every session of that step
    (i.e. after a resume, the relaunched step session replays the whole
    script from its first command — safe because the pre-crash commands
    are idempotent edit commands in the stress fixtures).

    Dispatch (content-based, mirroring a real model reading the prompt):
    planner system prompt -> the plan JSON; a step-session start -> the
    step's script queue (rebuilt per session); mid-session turns -> the
    next queued command; anything unscripted -> "SUBMIT" (ends a session
    rather than looping the harness's no-command nudge forever).
    """
    import re

    plan = list(script_spec.get("plan") or [])
    scripts = {int(k): list(v) for k, v in (script_spec.get("scripts") or {}).items()}
    queues: Dict[int, list] = {}

    def fn(messages: list, model: str) -> str:
        system = next((m.get("content", "") for m in messages
                       if isinstance(m, dict) and m.get("role") == "system"), "")
        if _PLANNER_MARKER in system:
            return json.dumps({"analysis": "scripted", "plan": plan})
        m = re.search(r"your step is #(\d+) of", system)
        step_id = int(m.group(1)) if m else 1
        users = [m for m in messages
                 if isinstance(m, dict) and m.get("role") == "user"]
        fresh = bool(users) and _SESSION_MARKER in str(users[-1].get("content", ""))
        if fresh or step_id not in queues:
            queues[step_id] = list(scripts.get(step_id) or ["SUBMIT"])
        q = queues[step_id]
        return str(q.pop(0)) if q else "SUBMIT"

    return fn


def install_script(script_spec: Dict[str, Any]) -> None:
    """install() with a scripted-model responder built from ``script_spec``.

    Assumes the router will resolve to any model while this is active
    (the responder ignores the model argument). See make_scripted_model_fn.
    """
    install(dynamic=make_scripted_model_fn(script_spec))
