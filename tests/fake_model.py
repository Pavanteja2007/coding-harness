"""A scripted fake model for tests — injectable via harness.deps.set_call_model.

Content-based dispatch (mirrors how a real model reads the conversation):
- planner system prompt  -> returns the configured plan JSON
- step-session prompts   -> pops scripted responses for the current step

A step session is detected by its first user message ("Begin. Reply with
exactly ONE bash command…"); each new session for a step pops the next
attempt's script (so retry paths can be scripted). Exposes get_last_usage()
like the real stub, so ModelClient's usage accounting is exercised.
"""
import json
import re
from typing import Dict, List, Optional, Sequence

SESSION_START_MARKER = "Begin. Reply with exactly ONE bash command"


class ScriptedModel:
    """Callable fake for Boundary 2 with scripted planner/step responses.

    scripts: {step_id: [responses_for_attempt_1, responses_for_attempt_2, ...]}
    where each response is a bash command (bare or fenced) or "SUBMIT".
    When an attempt's list is exhausted, the last one repeats.
    """

    def __init__(self, plan: List[Dict], scripts: Dict[int, List[Sequence[str]]]) -> None:
        self.plan = plan
        self.scripts = {int(k): [list(v) for v in vs] for k, vs in scripts.items()}
        self.call_count = 0
        self.seen_system_prompts: List[str] = []
        self._queues: Dict[int, List[str]] = {}
        self._attempt_cursor: Dict[int, int] = {}

    # Boundary-2-compatible usage reporting (ModelClient reads this).
    def get_last_usage(self) -> Dict[str, object]:
        return {
            "model": "scripted-fake",
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
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        self.seen_system_prompts.append(system)

        if "planning a bug fix" in system:
            return json.dumps({"analysis": "scripted", "plan": self.plan})

        step_id = self._current_step(system)
        if self._is_session_start(messages) or step_id not in self._queues:
            self._queues[step_id] = list(self._next_script(step_id))

        queue = self._queues[step_id]
        if queue:
            return queue.pop(0)
        return "SUBMIT"

    # -- internals --------------------------------------------------------

    def _current_step(self, system: str) -> int:
        m = re.search(r"your step is #(\d+) of", system)
        return int(m.group(1)) if m else 0

    def _is_session_start(self, messages: List[Dict[str, str]]) -> bool:
        # A session's first call has the "Begin..." message as the MOST
        # RECENT user message; later turns replace it with tool output.
        users = [m for m in messages if m["role"] == "user"]
        return bool(users) and SESSION_START_MARKER in users[-1]["content"]

    def _next_script(self, step_id: int) -> Sequence[str]:
        attempts = self.scripts.get(step_id) or [["SUBMIT"]]
        cursor = self._attempt_cursor.get(step_id, 0)
        script = attempts[min(cursor, len(attempts) - 1)]
        self._attempt_cursor[step_id] = cursor + 1
        return script


class ExplodingModel:
    """A fake that fails loudly — used to prove a code path never calls
    the model (e.g. the pre-fixed early exit)."""

    def __call__(self, *args, **kwargs) -> str:
        raise AssertionError("model must not be called on this path")

    def get_last_usage(self) -> Dict[str, object]:
        raise AssertionError("model must not be called on this path")
