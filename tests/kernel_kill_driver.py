"""Hard-kill driver for the kernel's durable per-turn state.

Runs the real ``AgentKernel`` daily strategy with a scripted model, then hard
kills the process (``os._exit``) after the requested number of turns. The
parent test asserts the pre-kill turns and their tool evidence survived on
disk, and that a resume can read them back.

Usage: ``python tests/kernel_kill_driver.py <repo> <log_root> <run_id> <kill_after_turn> <replies_json>``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.agent_kernel import AgentKernel, RunSpec
from harness.agent_kernel.gateway import ModelGateway


class KillModel:
    """A scripted model that hard-kills the process after one turn."""

    def __init__(self, replies, kill_after_turn):
        self.replies = list(replies)
        self.kill_after_turn = int(kill_after_turn)
        self.turns = 0
        self.messages = []

    def __call__(self, messages, **kwargs):
        self.turns += 1
        self.messages.append([dict(item) for item in messages])
        reply = (
            self.replies.pop(0)
            if self.replies
            else json.dumps({"tool": "read", "path": "app.py"})
        )
        if self.turns >= self.kill_after_turn:
            # Hard kill: no unwinding, no flush of anything the harness has
            # not already written and fsynced.
            os._exit(70)
        return reply


def main(argv):
    repo = argv[1]
    log_root = argv[2]
    run_id = argv[3]
    kill_after = argv[4]
    replies = json.loads(argv[5])
    model = KillModel(replies, kill_after)
    kernel = AgentKernel(
        repo_path=repo,
        log_root=log_root,
        model_gateway=ModelGateway(call_fn=model),
        config={"agent_approval": "auto", "steering_enabled": False},
    )
    spec = RunSpec(
        session_id="session-kill",
        run_id=run_id,
        request="change the value",
        repository_identity=repo,
        strategy="daily",
    )
    kernel.run(spec, strategy="daily")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
