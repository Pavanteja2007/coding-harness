"""Build/feature mode (Modes round, Task D) — new capabilities, verifier-gated.

Unlike fix mode there is NO pre-existing failing test to verify against.
The completion criterion, made real: **the agent writes its own tests for
the requested feature FIRST (a test-authored contract), then the
UNCHANGED fix-mode pipeline — baseline / regression / flake detection /
agent-tests / self-critique / git output — verifies the build against
those tests instead of a given one.**

Two stages, both inside one call:

Stage 1 — test authoring (harness-side, one model call):
    ``render_build_tests_prompt`` carries the feature request + the repo's
    test-tree listing + the files retrieval ranked. The model returns
    JSON ``{"tests": [{filename, content}]}`` — the ACCEPTANCE tests the
    feature must satisfy. They go through the SAME sanitize as fix-mode
    agent-tests (bare *.py names, compile check, caps). The files are
    written into a WORK COPY of the repo under the reserved tests dir
    (never the original repo — never-mutate holds for build mode too),
    and the *failure* of these tests on the pristine tree is CONFIRMED
    through the real verifier: a contract that already passes means the
    feature already exists — the run honestly reports that instead of
    pretending to build anything.

Stage 2 — the loop, VERBATIM: a Task is built with the acceptance tests
    as ``target_test`` (first surviving node id) and
    ``test_command=None`` (autodetect), with ``mode="build"`` stamped
    into config so state.json/trace record it, and
    ``harness.core.run_task`` — the existing, unchanged fix-engine —
    plans, edits, verifies (target + full-suite regression + flake
    detection + agent-tests + self-critique), retries with feedback, and
    produces the git-native output. Nothing in the fix loop is rebuilt or
    special-cased; build mode is a different ENTRY, not a different loop.

Why this is a real completion criterion (not a rubber stamp):
- The tests are written BEFORE any code exists (test-first contract).
- They must demonstrably FAIL on the pristine repo (the baseline verify
  proves it — the same mislabeled-task short-circuit fix mode uses,
  pointing the other way).
- The final gate is the full fix-mode pipeline against those tests.

Config keys: build_tests_max (3), build_tests_max_chars (16000),
build_tests_dir ("tests/_build_acceptance" — reserved, transient),
build_max_wallclock_s (None = inherit max_wallclock_s).
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness import agent_tests as agent_tests_mod
from harness import prompts, retrieval
from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger
from shared.types import Task

__all__ = ["author_acceptance_tests", "run_build"]


def _get_verify():
    """Resolve the verify boundary exactly like core.run_task does."""
    try:
        from execution.verify import verify  # type: ignore

        return verify
    except ImportError:
        from harness._stubs.verify import verify

        return verify


def author_acceptance_tests(
    request_text: str,
    repo_path: str,
    config: Dict[str, Any],
    trace: TraceLogger,
    model: ModelClient,
) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """Stage 1: author the feature's acceptance tests (one model call).

    Returns (tests, note): tests is the sanitize-survivor list (possibly
    None on generation failure — the caller aborts honestly), note is a
    human-readable outcome for the trace/feedback. Assumes repo_path is
    the ORIGINAL repo (read-only: only the test-tree listing is read).
    """
    ctx = retrieval.retrieve_context(
        repo_path,
        request_text,
        max_files=int(config.get("qa_max_files", 4)),
        target_test=None,
        index_root=None,  # caller has no shared root; grep/structural
        # against the original repo is fine here (read-only)
    )
    try:
        msgs = prompts.render_build_tests_prompt(
            request_text=request_text,
            tests_tree=agent_tests_mod.list_test_files(repo_path),
            context_files=ctx["files"],
            max_tests=int(config.get("build_tests_max", 3)),
        )
        raw = model.call(msgs, step="build-tests")
        if not (raw or "").strip():
            # EMPTY authoring reply (the endpoint's documented
            # reasoning-burn flake — tokens burned as hidden reasoning,
            # content=None): ONE retry with a repair nudge (the Task-F
            # live session showed the same prompt deterministically
            # burning twice), then the honest generation-failure path.
            trace.log("build_tests_empty_reply_retry", {})
            msgs = [
                *msgs,
                {"role": "assistant", "content": ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply came back EMPTY. Output the "
                        "JSON test files now — the JSON object only, no "
                        "reasoning steps, no code fences."
                    ),
                },
            ]
            raw = model.call(msgs, step="build-tests-retry")
    except Exception as exc:
        return None, f"test authoring failed: {exc}"

    parsed = agent_tests_mod.parse_agent_tests(raw)
    if parsed is None:
        trace.log("build_tests_parse_error", {"raw": (raw or "")[:2000]})
        return None, "unparseable test-authoring reply"
    tests, drop_reasons = agent_tests_mod.sanitize_agent_tests(
        parsed,
        max_files=int(config.get("build_tests_max", 3)),
        max_chars=int(config.get("build_tests_max_chars", 16000)),
    )
    if drop_reasons:
        trace.log("build_tests_dropped", {"reasons": drop_reasons})
    if not tests:
        return None, "no acceptance tests survived sanitize"
    trace.log(
        "build_tests_generated",
        {"files": [t["filename"] for t in tests]},
    )
    return tests, f"authored {len(tests)} acceptance test file(s)"


def run_build(
    request_text: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a requested feature end-to-end; verifier-gated completion.

    Returns {"result": TaskResult, "task_id", "trace_path", "status",
    "acceptance_tests": [...], "already_passing": bool}. The status is
    the fix-engine's own TaskResult.status — success means the acceptance
    tests + full suite passed through the REAL verification pipeline on
    the working copy, with the original repo never mutated (the same
    guarantee as fix mode; stage 1 copies the repo to a private base dir
    and the fix loop snapshots from THERE, not the original).

    Assumes repo_path is a readable directory and config is the
    task/session config (unknown keys pass through).
    """
    from harness.core import run_task

    cfg = get_config(config or {})
    tid = task_id or f"build-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    trace = TraceLogger(root / tid)
    model = ModelClient(trace, cfg)
    verify = _get_verify()

    trace.log(
        "task_start",
        {
            "task_id": tid,
            "mode": "build",
            "repo_path": repo_path,
            "issue_text": request_text,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )
    trace.log("mode", {"mode": "build", "request": request_text})

    # -- stage 1: author the acceptance tests (test-first contract) -----
    tests, note = author_acceptance_tests(request_text, repo_path, cfg, trace, model)
    if tests is None:
        trace.log("task_end", {"status": "error", "reason": note})
        return {
            "result": None,
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "status": "error",
            "acceptance_tests": [],
            "already_passing": False,
            "note": note,
        }

    rel_dir = str(cfg.get("build_tests_dir", "tests/_build_acceptance"))

    # Copy the ORIGINAL repo to a private base dir; the acceptance tests
    # are written THERE. The fix loop then snapshots base->pristine->work
    # as usual: the original repo is never mutated by test authoring, and
    # the acceptance tests ride INTO the task as repo content (they must,
    # for the regression gate to cover them).
    #
    # The base dir MUST sit OUTSIDE logs/{tid}/: run_task's _fresh_paths
    # archives logs/{tid}/ (renames it to {tid}.old-*) on every fresh
    # start, which would sweep the staged copy away mid-build (a staging
    # dir inside the dir being archived = snapshot WinError/FileNotFound).
    # {tid}.base is a sibling — untouched by the archive, harness-owned
    # logs-root space, kept as staging evidence.
    base_dir = root / f"{tid}.base"
    try:
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)
        shutil.copytree(
            repo_path,
            base_dir,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "*.pyc", ".git"
            ),
        )
    except OSError as exc:
        trace.log("task_end", {"status": "error", "reason": f"base copy failed: {exc}"})
        return {
            "result": None,
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "status": "error",
            "acceptance_tests": [],
            "already_passing": False,
            "note": f"base copy failed: {exc}",
        }
    written = agent_tests_mod.write_agent_tests(str(base_dir), rel_dir, tests)
    if not written:
        trace.log(
            "task_end", {"status": "error", "reason": "writing acceptance tests failed"}
        )
        return {
            "result": None,
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "status": "error",
            "acceptance_tests": [],
            "already_passing": False,
            "note": "writing acceptance tests failed",
        }

    # -- confirm the contract FAILS on the pristine tree -----------------
    # (a passing contract means the feature already exists — report that
    # honestly instead of minting an empty build)
    target_nodes = list(written)
    target_test = target_nodes[0]
    try:
        base_v = verify(
            str(base_dir),
            target_test,
            rerun_for_flake_check=0,
            test_command=None,
            verify_timeout_s=int(cfg["verify_timeout_s"]),
        )
    except Exception as exc:
        trace.log(
            "task_end", {"status": "error", "reason": f"baseline verify crashed: {exc}"}
        )
        return {
            "result": None,
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "status": "error",
            "acceptance_tests": written,
            "already_passing": False,
            "note": f"baseline verify crashed: {exc}",
        }
    trace.log(
        "build_baseline_verify",
        {
            "target_test": target_test,
            "target_passed_on_pristine": base_v.target_test_passed,
            "raw": (base_v.raw_output or "")[-2000:],
        },
    )
    if base_v.target_test_passed:
        trace.log(
            "task_end",
            {
                "status": "error",
                "reason": "acceptance test already passes — feature exists",
            },
        )
        return {
            "result": None,
            "task_id": tid,
            "trace_path": str((root / tid / "trace.jsonl").resolve()),
            "status": "already_exists",
            "acceptance_tests": written,
            "already_passing": True,
            "note": "the authored acceptance test already passes on the "
            "current repo — nothing to build (report this honestly)",
        }

    # -- stage 2: the loop, VERBATIM ------------------------------------
    # The fix-engine sees: repo = base_dir (with acceptance tests inside),
    # target_test = the first acceptance node, mode="build" stamped into
    # config (state.json + task_start carry it; nothing branches on it
    # inside run_task — build mode is an ENTRY, not a loop variant).
    build_cfg = dict(cfg)
    build_cfg["mode"] = "build"
    build_cfg["target_test"] = target_test
    # The request text becomes the issue text the whole pipeline uses
    # (planner, step prompts, self-critique, rationale, git output) — it
    # reads as a feature request, which is exactly what it is.
    task = Task(
        task_id=tid,
        repo_path=str(base_dir),
        issue_text=request_text,
        config=build_cfg,
    )
    # run_task writes its own logs/{task_id}/ tree — the SAME tid, so its
    # archive-then-create will move our stage-1 dir aside. Keep stage-1
    # evidence: copy the trace events into the run's own dir post-hoc is
    # fragile; instead we re-log a compact summary after the run.
    pre_events = trace.read_all()
    result = run_task(task, log_root=root)

    # Re-append stage-1 events into the run's (possibly archived) trace so
    # one trace.jsonl holds the whole build story. Best-effort.
    try:
        run_trace = TraceLogger(root / tid)
        for ev in pre_events:
            run_trace.log(ev.get("kind", "unknown"), ev.get("data"))
        run_trace.log(
            "build_mode_summary",
            {
                "acceptance_tests": written,
                "already_passing": False,
                "final_status": result.status,
                "attempts": result.attempts,
            },
        )
    except Exception:
        pass

    return {
        "result": result,
        "task_id": tid,
        "trace_path": str((root / tid / "trace.jsonl").resolve()),
        "status": result.status,
        "acceptance_tests": written,
        "already_passing": False,
    }
