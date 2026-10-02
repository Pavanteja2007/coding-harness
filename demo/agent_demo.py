"""60-second agent demo: question -> @mention edit -> approval -> undo -> compact.

Run:  python demo/agent_demo.py

Deterministic and offline: a scripted model (the tests' queue pattern),
no API key, no network, no Docker (the agent loop's READ/EDIT/WRITE run
locally on the live repo; no BASH/VERIFY is scripted). Everything lives
under demo/demo-work-agent/ (deleted at start, kept at exit).

What it shows, in demo order (each step prints its own evidence):
  1. QUESTION   — "what does src/app.py do?" classifies as a question
                  (read-only answer, no mutations, empty diff).
  2. @MENTION   — "fix @app.py ..." expands the file into context.
  3. PLAN       — render_agent_plan previews steps+files; auto-approved
                  and injected as steering guidance (not a contract).
  4. APPROVAL   — agent_approval=require: the EDIT needs one approval
                  (Allow once); approval_required/decided land in trace.
  5. UNDO       — agent_diff shows the change; undo_edits restores the
                  original; /copy-diff copies it (or prints when the
                  clipboard is unavailable headless).
  6. RESUME     — load_resume_history replays the prior turns and the
                  same task_id continues (history replay, not restart).
  7. COMPACT    — compact_session summarizes old turns, keeps recent.

Exit 0 = every step proved its claim. Assumes: run from the repo root.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO = ROOT / "demo" / "demo-work-agent"
REPO = DEMO / "repo"
LOGS = DEMO / "logs"

APP_PY = 'def mean(values):\n    """Arithmetic mean (bug: returns the sum)."""\n    return sum(values)\n'


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _step(msg: str) -> None:
    print(f"  {msg}")


def _fail(msg: str) -> int:
    print(f"\nDEMO FAILED: {msg}")
    return 1


class Scripted:
    """Queue-driven fake model: one reply per call (tests' pattern)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kw):
        self.calls.append(list(messages))
        assert self.replies, "model called more times than scripted"
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def setup_env() -> None:
    """Fresh demo-work-agent dir + buggy repo."""
    if DEMO.exists():
        shutil.rmtree(DEMO, onerror=lambda f, p, e: os.chmod(p, 0o700) or f(p))
    (REPO / "src").mkdir(parents=True)
    (REPO / "src" / "app.py").write_text(APP_PY, encoding="utf-8")
    (REPO / "README.md").write_text("# demo repo\n", encoding="utf-8")
    LOGS.mkdir(parents=True)
    _banner("STEP 0 — isolated demo workspace (demo/demo-work-agent/)")
    _step(f"repo:      {REPO}")
    _step("bug:       mean() returns sum(values) instead of the mean")
    _step(f"logs root: {LOGS}")


def step1_question() -> bool:
    """Classify a question; answer read-only (READ then DONE, no diff)."""
    from harness import deps
    from harness.agent_loop import classify_agent_input, run_agent

    _banner("STEP 1 — question (read-only answer, nothing launched to edit)")
    intent = classify_agent_input("what does src/app.py do?", config={})
    _step(f"classify -> {intent.kind} ({intent.reason})")
    if intent.kind != "question":
        return False
    deps.set_call_model(
        Scripted(
            [
                '{"tool": "read", "path": "src/app.py"}',
                '{"tool": "done", "answer": "src/app.py defines mean(), which sums."}',
            ]
        )
    )
    try:
        out = run_agent(
            request="what does src/app.py do?",
            repo_path=str(REPO),
            config={"steering_enabled": False, "plan_with_memory": False},
            log_root=LOGS,
            task_id="agent-demo-q",
        )
    finally:
        deps.reset_overrides()
    _step(f"status={out['status']} answer={out['answer'][:60]!r} diff={out['diff']!r}")
    ok = out["status"] == "success" and not out["diff"]
    _step(
        "PASS: answered with no file changes" if ok else "FAIL: unexpected diff/status"
    )
    return ok


def step2_mention() -> bool:
    """@app.py expands into file context."""
    from cli.session import expand_at_mentions

    _banner("STEP 2 — @mention (attach file content as context)")
    expanded, inserted = expand_at_mentions(
        "fix @app.py mean to divide", REPO, files=["src/app.py"]
    )
    _step(f"inserted: {inserted}")
    ok = bool(inserted) and "@path context" in expanded
    _step("PASS: mention resolved + snippet attached" if ok else "FAIL: no expansion")
    return ok


def step3_plan() -> str:
    """Preview steps+files; auto-approve as guidance."""
    from harness.agent_loop import render_agent_plan

    _banner("STEP 3 — /plan preview (approve runs, edit would steer)")
    plan = render_agent_plan("fix mean() in src/app.py to divide", str(REPO), {})
    for i, s in enumerate(plan["steps"], start=1):
        _step(f"{i}. {s}")
    _step(f"files: {plan['files']}")
    guidance = (
        "fix mean() in src/app.py to divide\n\nUser-approved plan:\n"
        + "\n".join(plan["steps"])
    )
    _step("approved -> injected as steering guidance (not a contract)")
    return guidance


def step4_approval(guidance: str) -> bool:
    """EDIT under require-mode: one Allow-once approval; trace proves it."""
    from harness import deps
    from harness.agent_loop import run_agent

    _banner("STEP 4 — approval (require-mode: Allow once / always / reject)")
    seen = {}

    def approver(tool, args, preview):
        _step(
            f"approval needed — {tool.upper()} {args.get('path', '')} -> allow once (y)"
        )
        seen["asked"] = True
        return True

    deps.set_call_model(
        Scripted(
            [
                '{"tool": "edit", "path": "src/app.py", '
                '"old_string": "return sum(values)", '
                '"new_string": "return sum(values) / len(values)"}',
                '{"tool": "done", "answer": "mean() now divides by len."}',
            ]
        )
    )
    try:
        out = run_agent(
            request="fix mean() in src/app.py to divide",
            repo_path=str(REPO),
            config={
                "steering_enabled": False,
                "plan_with_memory": False,
                "agent_approval": "require",
            },
            log_root=LOGS,
            task_id="agent-demo-edit",
            approve_fn=approver,
            plan_guidance=guidance,
        )
    finally:
        deps.reset_overrides()
    kinds = [
        json.loads(ln).get("kind")
        for ln in (LOGS / "agent-demo-edit" / "trace.jsonl")
        .read_text(encoding="utf-8", errors="replace")
        .splitlines()
    ]
    req, dec = "approval_required" in kinds, "approval_decided" in kinds
    _step(
        f"status={out['status']} asked={seen.get('asked')} trace: required={req} decided={dec}"
    )
    text = (REPO / "src" / "app.py").read_text(encoding="utf-8")
    ok = (
        out["status"] == "success"
        and seen.get("asked")
        and req
        and dec
        and "len(values)" in text
    )
    _step("PASS: approved edit applied" if ok else "FAIL: approval/edit did not land")
    return ok


def step5_undo() -> bool:
    """Live diff, undo one file, copy-diff."""
    from cli.session import copy_text_to_clipboard
    from harness.agent_loop import agent_diff, undo_edits

    _banner("STEP 5 — /diff, /diff undo, /copy-diff")
    diff = agent_diff("agent-demo-edit", LOGS, str(REPO))
    _step(f"live diff lines: {len(diff.splitlines())} (shows the fix)")
    if not diff or "len(values)" not in diff:
        _step("FAIL: expected the fix in the diff")
        return False
    res = undo_edits("agent-demo-edit", LOGS, str(REPO), steps=1)
    _step(
        f"undo restored={res['restored']} deleted={res['deleted']} missing={res['missing']}"
    )
    after = agent_diff("agent-demo-edit", LOGS, str(REPO))
    text = (REPO / "src" / "app.py").read_text(encoding="utf-8")
    ok = not after and text == APP_PY and res["restored"]
    _step("PASS: undo restored the original" if ok else "FAIL: undo did not restore")
    if copy_text_to_clipboard(diff):
        _step("/copy-diff: copied to clipboard")
    else:
        _step("/copy-diff: clipboard unavailable headless — diff shown instead")
    return ok


def step6_resume() -> bool:
    """Same task_id continues with prior turns replayed (not restarted)."""
    from harness import deps
    from harness.agent_loop import load_resume_history, run_agent

    _banner("STEP 6 — /resume (history replay, not restart)")
    history = load_resume_history("agent-demo-edit", LOGS)
    _step(f"replayed context chars: {len(history)}")
    if not history or ("mean" not in history.lower() and "src/app.py" not in history):
        _step("FAIL: resume history is empty")
        return False
    _step(f"history head: {history[:120]!r}")
    deps.set_call_model(
        Scripted(
            [
                '{"tool": "done", "answer": "resumed: prior fix confirmed reverted, nothing to redo."}',
            ]
        )
    )
    try:
        out = run_agent(
            request="fix mean() in src/app.py to divide",
            repo_path=str(REPO),
            config={"steering_enabled": False, "plan_with_memory": False},
            log_root=LOGS,
            task_id="agent-demo-edit",
            resume_history=history,
        )
    finally:
        deps.reset_overrides()
    # the replayed context must have reached THIS run's model input:
    # the steering event with at=agent-resume-history is the record.
    trace_lines = (
        (LOGS / "agent-demo-edit" / "trace.jsonl")
        .read_text(encoding="utf-8", errors="replace")
        .splitlines()
    )
    replayed = False
    for ln in trace_lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if (
            obj.get("kind") == "steering"
            and (obj.get("data") or {}).get("at") == "agent-resume-history"
        ):
            replayed = True
    ok = out["status"] == "success" and replayed
    _step("PASS: resumed under the same id with history" if ok else "FAIL: no replay")
    return ok


def step7_compact() -> bool:
    """Compact a conversation: old turns summarized, recent kept."""
    from cli.session import append_turn, compact_session, load_or_create, save_session

    _banner("STEP 7 — /compact (recall-backed summary, recent kept)")
    conv = load_or_create(LOGS, REPO, session_id="demo-conv")
    for i in range(14):
        append_turn(
            conv, "user" if i % 2 == 0 else "assistant", f"turn {i} about mean()"
        )
    save_session(LOGS, conv)
    summary = compact_session(conv, LOGS, keep_last=4)
    _step(f"summary chars: {len(summary)} turns kept: {len(conv['turns'])}")
    ok = bool(summary) and len(conv["turns"]) == 4
    _step("PASS: older turns summarized" if ok else "FAIL: compact did not trim")
    return ok


def main() -> int:
    t0 = time.time()
    print("neo agent demo — offline, deterministic (no key, no Docker)")
    setup_env()
    results = [
        ("question", step1_question()),
        ("mention", step2_mention()),
    ]
    guidance = step3_plan()
    results.append(("plan", bool(guidance)))
    results.append(("approval", step4_approval(guidance)))
    results.append(("undo", step5_undo()))
    results.append(("resume", step6_resume()))
    results.append(("compact", step7_compact()))
    _banner("DEMO COMPLETE")
    for name, ok in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    print(f"  {time.time() - t0:.1f}s elapsed; artifacts kept under {DEMO}")
    failed = [n for n, ok in results if not ok]
    if failed:
        return _fail(f"steps failed: {failed}")
    print("  ask -> plan -> approve -> edit -> undo -> resume -> compact: all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
