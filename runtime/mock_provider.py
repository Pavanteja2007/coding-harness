"""Deterministic offline model responses for tests and demos.

A call_model back-end that makes no network requests: it replies with
per-model canned strings and charges synthetic token/cost numbers to the
ledger. Enabled per-task via config["use_mock_provider"] = True. Assumes
one shared mock response table per run (thread-safe for concurrent reads).
"""
from __future__ import annotations

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
