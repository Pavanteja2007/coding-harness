"""Hard-kill driver for the context-budget resume regression.

Runs the REAL agent kernel with a scripted model that grows the context until it
compacts, then hard-kills the process with ``os._exit(70)`` at the requested turn.
Nothing is flushed on the way out, so whatever survives on disk survived because
it was written durably - which is exactly what the parent test asserts.

Usage:
    python context_budget_kill_driver.py <repo> <log_root> <run_id> <kill_after_turn> [config_json]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.agent_kernel import AgentKernel, RunSpec
from harness.agent_kernel.gateway import ModelGateway

BIG = ("def helper(value):\n    return value * 2 + 1\n" * 90)[:4000]
HUGE = ("def helper(value):\n    return value * 2 + 1\n" * 1000)[:40000]


def _script(config):
    """Return ``(call, state)`` for a model that grows context, then compacts.

    The kill fires AFTER the first compaction and after two more working turns,
    so the parent test can assert that a run which had already compacted still
    resumed with its budget, its receipts, and its prior turns intact.
    """
    state = {
        "calls": 0,
        "compacting": False,
        "kill_after": int(config["kill_after"]),
        "since_compaction": 0,
    }

    def call(messages, **kwargs):
        step = str(kwargs.get("step") or "")
        if step.startswith("context-compaction"):
            state["compacting"] = True
            # Count from the COMPLETED compaction, so the kill lands a couple of
            # working turns later - after the receipt is durable on disk.
            state["since_compaction"] = 0
            return "SUMMARY: big.txt was read repeatedly; nothing changed yet."
        state["calls"] += 1
        state["since_compaction"] += 1
        if state["since_compaction"] > 20:
            return json.dumps({"tool": "finish", "answer": "done"})
        # Turn 1 creates a genuinely large file; every later turn reads it back,
        # so the request grows until the token budget compacts it.
        if state["calls"] == 1:
            return json.dumps({"tool": "write", "path": "big.txt", "content": HUGE})
        # A real mutation every third turn, so the pre-image store has content.
        if state["calls"] % 3 == 0:
            return json.dumps(
                {
                    "tool": "write",
                    "path": f"note-{state['calls']}.md",
                    "content": f"turn {state['calls']}\n{BIG}\n",
                }
            )
        return json.dumps({"tool": "read", "path": "big.txt"})

    return call, state


def main(argv):
    repo, log_root, run_id = argv[1], argv[2], argv[3]
    overrides = json.loads(argv[5]) if len(argv) > 5 else {}
    config = {
        "agent_approval": "auto",
        "steering_enabled": False,
        "context_window_tokens": 32768,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 12000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 40,
        "kill_after": 2,
    }
    config.update(overrides)
    call, state = _script(config)

    def counted(messages, **kwargs):
        response = call(messages, **kwargs)
        if state["compacting"] and state["since_compaction"] >= state["kill_after"]:
            # Hard kill: no flush, no atexit, no cleanup path.
            os._exit(70)
        return response

    kernel = AgentKernel(
        repo_path=repo,
        log_root=Path(log_root),
        model_gateway=ModelGateway(call_fn=counted),
        config=config,
    )
    kernel.run(
        RunSpec(
            session_id="session-kill",
            run_id=run_id,
            request="exercise the context budget",
            repository_identity=repo,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
