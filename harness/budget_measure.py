"""Measure the Phase-3b prompt-budget baseline through the REAL daily kernel.

Turn 1 / 10 / 40 / 80, a scripted deterministic model, the real strategy, the
real context builder, the real tool surface. No Docker, no provider, no network.

The growth script is the corpus's shape: a long session grows because it reads
real files, so every tool result is thousands of tokens and only the token budget
can bound the request. That is the shape the budget table has to be honest about.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from harness.agent_kernel.budget import TokenEstimator
from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.kernel import AgentKernel
from harness.prompt_budget import profile_session

HUGE = ("x" * 79 + "\n") * 320  # ~26 KB per read
DEPTHS = (1, 10, 40, 80)
WINDOW = 32768


def build_repo(root: Path) -> None:
    (root / "pkg" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "Repository rules:\n- Never modify tests.\n- Run `python -m pytest` from the root.\n",
        encoding="utf-8",
    )
    (root / "pkg" / "sub" / "AGENTS.md").write_text(
        "Sub-package rules:\n- This package uses tabs.\n- Public API lives in __init__.py.\n",
        encoding="utf-8",
    )
    (root / "pkg" / "sub" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "big.txt").write_text(HUGE, encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")


class Model:
    """A scripted boundary that reads, edits, touches a nested file, finishes."""

    def __init__(self, turns, on_call=None):
        self.turns = int(turns)
        self.calls = 0
        self.on_call = on_call

    def __call__(self, messages, **kwargs):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self, messages, kwargs)
        step = str(kwargs.get("step") or "")
        if step.startswith("context-compaction"):
            return (
                "## Objective\nFix the value bug.\n\n"
                "## Important Details\nNever modify tests.\n\n"
                "## Work State\nRead big.txt repeatedly.\n\n"
                "## Next Move\nEdit app.py.\n\n"
                "## Relevant Files\napp.py\n"
            )
        turn = self.calls
        if turn >= self.turns:
            return json.dumps({"tool": "finish", "answer": "done"})
        if turn == 1:
            return json.dumps(
                {"tool": "write", "path": "big.txt", "content": HUGE}
            )
        if turn % 7 == 0:
            # Touch a file in a directory that has its OWN instructions, so the
            # PathTrigger has something real to inject.
            return json.dumps({"tool": "read", "path": "pkg/sub/mod.py"})
        if turn % 3 == 0:
            return json.dumps(
                {"tool": "edit", "path": "app.py", "old_string": "value = 1\n",
                 "new_string": f"value = {turn}\n"}
            )
        return json.dumps({"tool": "read", "path": "big.txt"})


def run_arm(label, clearing_enabled, tools, turns):
    """Run one arm and return its SessionBudgetReport plus the run status."""
    import tempfile

    base = Path(tempfile.mkdtemp(prefix=f"p3a-budget-{label}-"))
    repo = base / "repo"
    repo.mkdir()
    build_repo(repo)
    log_root = base / "logs"
    captured = {}

    def witness(_model, messages, kwargs):
        step = str(kwargs.get("step") or "")
        if step.startswith("agent-"):
            captured[len(captured) + 1] = [dict(item) for item in messages]

    model = Model(turns=turns, on_call=witness)
    config = {
        "agent_approval": "auto",
        "steering_enabled": False,
        "context_window_tokens": WINDOW,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 12000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 200,
        "context_clearing_enabled": clearing_enabled,
    }
    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=log_root,
        model_gateway=ModelGateway(call_fn=model),
        config=config,
    )
    from shared.agent_contracts import RunSpec

    result = kernel.run(
        RunSpec(
            session_id=f"budget-{label}",
            run_id=f"budget-{label}",
            request="the value in app.py is wrong",
            repository_identity=str(repo),
        )
    )
    report = profile_session(
        [(depth, captured[depth], tools) for depth in DEPTHS if depth in captured],
        estimator=TokenEstimator(),
        depths=DEPTHS,
    )
    return report, result.status, model.calls, log_root


def _counts(log_root, run_id):
    """Count the events that decide what this arm actually DID.

    The kernel writes ``<log_root>/<run_id>/trace.jsonl``; reading
    ``<log_root>/trace.jsonl`` finds nothing and reports ``0`` for both, which
    reads as "this arm did nothing" rather than "this arm did nothing I could
    see" - the exact ambiguity this repo's honesty rules are about. ``_read``
    says whether the file was found at all.
    """
    path = Path(log_root) / run_id / "trace.jsonl"
    counts = {
        "context_compacted": 0,
        "context_cleared": 0,
        "path_trigger_context": 0,
    }
    if not path.is_file():
        return {**counts, "read": False}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("event") or "")
        if name in counts:
            counts[name] += 1
    return {**counts, "read": True}


def main() -> int:
    tools = []
    try:
        from harness.tools import typed_tool_schemas

        tools = list(typed_tool_schemas())
    except Exception as exc:  # pragma: no cover
        print(f"schema read failed: {exc}")

    arms = {}
    for label, clearing in (("clearing-off", False), ("clearing-on", True)):
        report, status, calls, log_root = run_arm(label, clearing, tools, turns=92)
        arms[label] = report
        counts = _counts(log_root, f"budget-{label}")
        print(f"\n########## ARM {label} (clearing={clearing}) ##########")
        print(
            f"status={status} model_calls={calls} depths={report.depths} "
            f"journal_read={counts['read']} "
            f"compactions={counts['context_compacted']} "
            f"clearings={counts['context_cleared']} "
            f"path_trigger_injections={counts['path_trigger_context']}"
        )
        print(report.table())
        growth = report.growth()
        print()
        for name in ("tool_schemas", "tool_results_live", "conversation_summary"):
            row = growth.get(name, {})
            print(
                f"  {name}: first={row.get('first')} last={row.get('last')} "
                f"growth={row.get('growth_x')} share_last={row.get('share_last')}"
            )
        out = log_root.parent / f"budget_{label}.json"
        out.write_text(json.dumps(report.as_dict(), indent=1), encoding="utf-8")
        print(f"  report: {out}")

    print("\n########## THE PHASE-3b NUMBER ##########")
    off_report = arms["clearing-off"]
    for depth in DEPTHS:
        row = off_report.at(depth)
        if row is None:
            continue
        print(
            f"turn {depth}: tool_schemas {row.tokens('tool_schemas')} tokens = "
            f"{row.schema_share:.1%} of a {row.total_tokens}-token prompt"
        )
    print()
    print("| depth | total off | total on | delta |")
    print("|---|---:|---:|---:|")
    for depth in DEPTHS:
        a = arms["clearing-off"].at(depth)
        b = arms["clearing-on"].at(depth)
        if not a or not b:
            continue
        delta = (b.total_tokens - a.total_tokens) / max(1, a.total_tokens)
        print(f"| turn {depth} | {a.total_tokens} | {b.total_tokens} | {delta:+.1%} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
