"""Thin wrapper around the model boundary (call_model) that:
- logs every prompt/response pair to the trace,
- snapshots usage (tokens/cost) right after each call for budget checks,
- accumulates the TaskResult.model_calls records.

The actual provider call is harness.deps.get_call_model() — real router
(Terminal 3) or stub, injected or not, is decided there.
"""
import time
from typing import Any, Callable, Dict, List, Optional

from harness.trace import TraceLogger


class ModelClient:
    """One per task run; wraps the boundary call_model with logging and
    usage accounting. Assumes TraceLogger writes to logs/{task_id}/."""

    def __init__(self, trace: TraceLogger, config: Dict[str, Any]) -> None:
        self.trace = trace
        self.config = config
        self.model_calls: List[Dict[str, Any]] = []
        self.total_cost_usd: float = 0.0
        self.total_tokens: int = 0
        self._step_counter: int = 0

    def call(
        self,
        messages: List[Dict[str, str]],
        step: str,
        difficulty_hint: Optional[str] = None,
    ) -> str:
        """Send one chat request; returns the assistant content.

        Assumes messages is OpenAI-style. step is a short label for the
        trace ("plan", "step-2", "diagnose"). On a model error, raises —
        the loop controller decides how to count it.
        """
        fn: Callable[..., str] = self._get_fn()
        self._step_counter += 1
        self.trace.log("model_request", {"step": step, "messages": messages})
        started = time.time()
        response = fn(
            messages,
            difficulty_hint=difficulty_hint,
            provider=self.config.get("provider"),
            model=self.config.get("model"),
            api_key=self.config.get("api_key"),
        )
        elapsed = time.time() - started

        usage = self._read_usage(fn)
        record = {
            "step": step,
            "model": usage.get("model", self.config.get("model")),
            "provider": usage.get("provider", self.config.get("provider")),
            "tokens": usage.get("tokens", 0),
            "cost": usage.get("cost_usd", 0.0),
            "elapsed_s": round(elapsed, 2),
            "call_index": self._step_counter,
        }
        self.model_calls.append(record)
        self.total_cost_usd += float(record["cost"] or 0.0)
        self.total_tokens += int(record["tokens"] or 0)
        self.trace.log("model_response", {
            "step": step,
            "content": response,
            "usage": record,
        })
        return response

    def _get_fn(self) -> Callable[..., str]:
        from harness.deps import get_call_model

        return get_call_model()

    def _read_usage(self, fn: Callable[..., str]) -> Dict[str, Any]:
        """Pull usage from whatever module backs call_model. Convention: a
        model module may expose get_last_usage() (the stub does). Falls
        back to zeros so budget accounting never crashes the run."""
        try:
            getter = getattr(fn, "get_last_usage", None)
            if getter is None:
                mod = __import__(fn.__module__, fromlist=["get_last_usage"])
                getter = getattr(mod, "get_last_usage", None)
            if callable(getter):
                return dict(getter() or {})
        except Exception:
            pass
        return {}

    def snapshot_usage(self) -> Dict[str, Any]:
        """Current cumulative usage for budget/stop checks."""
        return {
            "cost_usd": round(self.total_cost_usd, 6),
            "tokens": self.total_tokens,
            "calls": len(self.model_calls),
        }
