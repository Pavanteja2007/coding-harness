"""Long-horizon planning for build mode — multi-session feature builds.

Build mode (harness.build_mode) plans within a single session: one
feature request, one test-authored contract, one fix-loop run. For a
feature request too large for one session, this module adds the layer
ABOVE it: a PROJECT PLAN.

Three stages, all inside one call or spread across MANY calls:

Stage 1 — acceptance-criteria extraction (Task B of the round):
    BEFORE any implementation work, one model call
    (render_project_criteria_prompt) extracts explicit acceptance
    criteria from the request — what "done" means for the WHOLE
    multi-session effort, not just per-sub-task tests. Every criterion
    is one testable, snake_case-id'd behavior sentence. This list is
    the completion contract: every criterion id must be covered by at
    least one sub-task, and the project's final gate verifies criteria
    coverage plus the full suite on the accumulated tree.

Stage 2 — decomposition (Task A of the round):
    one model call (render_project_plan_prompt) decomposes the feature
    into a sequence of smaller, independently-checkpointed SUB-TASKS in
    dependency order, each mapping to the acceptance-criteria ids it
    delivers. Each sub-task becomes one SESSION's build — an unchanged
    harness.build_mode.run_build call with the sub-task's description as
    its request, carrying the previous sub-tasks' accumulated tree.

Stage 3 — execution across sessions (Task A continued):
    run_project executes sub-tasks one per invocation up to the
    session budget (project_sub_tasks_per_session, default 1), then
    checkpoints: the project state file records which sub-tasks are
    DONE, and the run PAUSES with status "checkpointed" — later
    sessions CONTINUE from the persisted plan (project_resume=True or
    an explicit project_id whose plan exists). A completed sub-task's
    accumulated work tree (its verified work/ copy) is carried forward
    as the NEXT sub-task's starting repo, so progress persists across
    sessions without ever mutating the ORIGINAL repo.

The project plan/state file: logs/{project_id}/project.json —
    {
      "project_id": str,
      "request_text": str,
      "repo_path": str,           # ORIGINAL repo (never mutated)
      "criteria": [{"id", "description"}],
      "sub_tasks": [{"id", "description", "criteria", "files_hint"}],
      "completed": [int],          # sub-task ids DONE (verified)
      "current_tree": str | null,  # repo-relative-in-logs path of the
                                  # accumulated tree the next sub-task
                                  # starts from (null = original repo)
      "sessions": [int],          # count of run_project invocations
      "status": "planning"|"active"|"checkpointed"|"success"|
                "already_exists"|"failed"
    }
Atomic tmp+replace writes, exactly the state.json discipline. A crash
mid-sub-task loses only that sub-task's run (the standard build-mode
task dir survives as evidence; the project file still marks it not
done — the next session re-runs it fresh, which is correct because an
unverified sub-task must never seed the next one).

The completion gate for the whole effort (honest, not a rubber stamp):
- every sub-task that ran succeeded through the REAL verification
  pipeline (its own run_build verifier gate), AND
- every criterion id is claimed by >=1 completed sub-task, AND
- a FINAL full-suite verify on the accumulated tree passes (the last
  sub-task's regression gate already does this on the tree that
  includes all prior work — the final check re-runs it as the
  project-level confirmation, targetless = the whole suite is the
  gate). Exception: when EVERY sub-task completed by verification
  (already_exists — its tests passed on the start tree, nothing was
  built anywhere), the whole-project verdict is "already_exists" and
  no final verify runs: the only candidate tree would be the ORIGINAL
  repo, which the sandbox mounts read-write, and each sub-task's
  tests already passed through the real verifier on the tree that
  mattered.

Config keys: build_project (False), project_max_sub_tasks (4),
project_sub_tasks_per_session (1), project_criteria_max (8),
project_resume (False).
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness import prompts, retrieval
from harness.config import get_config
from harness.model_client import ModelClient
from harness.trace import TraceLogger

__all__ = [
    "extract_acceptance_criteria",
    "plan_project",
    "read_project",
    "run_project",
]

PROJECT_FILE = "project.json"


def _get_verify():
    """Resolve the verify boundary exactly like core.run_task does."""
    try:
        from execution.verify import verify  # type: ignore

        return verify
    except ImportError:
        from harness._stubs.verify import verify

        return verify


def read_project(log_dir: Path) -> Optional[Dict[str, Any]]:
    """Read an existing project plan/state file; None if absent/broken.

    Assumes log_dir is the project's log directory
    (logs/{project_id}/). A return of None means "no resumable
    project" — the caller starts fresh.
    """
    try:
        data = json.loads((Path(log_dir) / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("project_id"):
        return None
    return data


def _write_project(log_dir: Path, data: Dict[str, Any]) -> None:
    """Atomically persist the project state (tmp + replace)."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(log_dir) / (PROJECT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(Path(log_dir) / PROJECT_FILE)


def _parse_criteria_json(raw: str) -> Optional[List[Dict[str, str]]]:
    """Parse the criteria-extraction reply; None when unparseable.

    Tolerates code fences and surrounding prose (same discipline as
    _parse_plan_json). Normalizes each entry to {id, description} with
    a snake_case-forced id; entries missing either field are dropped.
    """
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    raw_criteria = obj.get("criteria") if isinstance(obj, dict) else None
    if not isinstance(raw_criteria, list):
        return None
    out: List[Dict[str, str]] = []
    for c in raw_criteria:
        if not isinstance(c, dict):
            continue
        cid = re.sub(r"[^a-z0-9_]+", "_", str(c.get("id") or "").strip().lower())
        desc = str(c.get("description") or "").strip()
        if cid and desc:
            out.append({"id": cid.strip("_"), "description": desc})
    return out or None


def _parse_project_plan_json(raw: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Parse the decomposition reply; None when unparseable.

    Returns (analysis, sub_tasks) with each sub-task normalized to
    {id, description, criteria, files_hint}; criteria ids are
    snake_case-normalized, non-list shapes degrade to [].
    """
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    sub_tasks = obj.get("sub_tasks") if isinstance(obj, dict) else None
    if not isinstance(sub_tasks, list) or not sub_tasks:
        return None
    out: List[Dict[str, Any]] = []
    for i, st in enumerate(sub_tasks, start=1):
        if not isinstance(st, dict):
            continue
        desc = str(st.get("description") or "").strip()
        if not desc:
            continue
        criteria = [
            re.sub(r"[^a-z0-9_]+", "_", str(c).strip().lower()).strip("_")
            for c in (st.get("criteria") or [])
            if str(c).strip()
        ]
        out.append(
            {
                "id": int(st.get("id") or i),
                "description": desc,
                "criteria": [c for c in criteria if c],
                "files_hint": [str(f) for f in (st.get("files_hint") or []) if f],
            }
        )
    if not out:
        return None
    analysis = str(obj.get("analysis") or "scripted")
    return analysis, out


def extract_acceptance_criteria(
    request_text: str,
    repo_path: str,
    config: Dict[str, Any],
    trace: TraceLogger,
    model: ModelClient,
) -> Tuple[Optional[List[Dict[str, str]]], str]:
    """Stage 1 (Task B): extract the whole-effort acceptance criteria.

    Returns (criteria, note): criteria is the normalized [{id,
    description}] list (None on generation failure — the caller aborts
    honestly), note is a human-readable outcome for the trace/feedback.
    Assumes repo_path is the ORIGINAL repo (read-only: only
    retrieval's file listing is used).
    """
    max_criteria = int(config.get("project_criteria_max", 8))
    ctx = retrieval.retrieve_context(
        repo_path,
        request_text,
        max_files=int(config.get("qa_max_files", 4)),
        target_test=None,
        index_root=None,
    )
    msgs = prompts.render_project_criteria_prompt(
        request_text=request_text,
        context_files=ctx["files"],
        max_criteria=max_criteria,
    )
    try:
        raw = model.call(msgs, step="project-criteria")
        if not (raw or "").strip():
            # The endpoint's documented reasoning-burn flake (empty
            # content): ONE retry with a repair nudge, then the honest
            # generation-failure path — same discipline as build_mode.
            trace.log("project_criteria_empty_reply_retry", {})
            msgs = [
                *msgs,
                {"role": "assistant", "content": ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply came back EMPTY. Output the "
                        "JSON criteria object now — the JSON object only, "
                        "no reasoning steps, no code fences."
                    ),
                },
            ]
            raw = model.call(msgs, step="project-criteria-retry")
    except Exception as exc:
        return None, f"criteria extraction failed: {exc}"

    parsed = _parse_criteria_json(raw)
    if parsed is None:
        trace.log("project_criteria_parse_error", {"raw": (raw or "")[:2000]})
        return None, "unparseable criteria-extraction reply"
    if len(parsed) > max_criteria:
        parsed = parsed[:max_criteria]
        trace.log("project_criteria_capped", {"kept": max_criteria})
    trace.log(
        "project_criteria_extracted",
        {"criteria": [c["id"] for c in parsed]},
    )
    return parsed, f"extracted {len(parsed)} acceptance criteria"


def plan_project(
    request_text: str,
    repo_path: str,
    criteria: List[Dict[str, str]],
    config: Dict[str, Any],
    trace: TraceLogger,
    model: ModelClient,
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Stage 2 (Task A): decompose the feature into ordered sub-tasks.

    Returns (sub_tasks, note): sub_tasks is the normalized [{id,
    description, criteria, files_hint}] list (None on failure), note a
    human-readable outcome. Assumes criteria is the stage-1 output and
    repo_path the ORIGINAL repo (read-only).
    """
    max_sub_tasks = int(config.get("project_max_sub_tasks", 4))
    ctx = retrieval.retrieve_context(
        repo_path,
        request_text,
        max_files=int(config.get("qa_max_files", 4)),
        target_test=None,
        index_root=None,
    )
    msgs = prompts.render_project_plan_prompt(
        request_text=request_text,
        criteria=criteria,
        context_files=ctx["files"],
        max_sub_tasks=max_sub_tasks,
    )
    try:
        raw = model.call(msgs, step="project-plan")
        if not (raw or "").strip():
            trace.log("project_plan_empty_reply_retry", {})
            msgs = [
                *msgs,
                {"role": "assistant", "content": ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply came back EMPTY. Output the "
                        "JSON sub-task plan now — the JSON object only, no "
                        "reasoning steps, no code fences."
                    ),
                },
            ]
            raw = model.call(msgs, step="project-plan-retry")
    except Exception as exc:
        return None, f"project planning failed: {exc}"

    parsed = _parse_project_plan_json(raw)
    if parsed is None:
        trace.log("project_plan_parse_error", {"raw": (raw or "")[:2000]})
        return None, "unparseable project-plan reply"
    _analysis, sub_tasks = parsed
    if len(sub_tasks) > max_sub_tasks:
        sub_tasks = sub_tasks[:max_sub_tasks]
        trace.log("project_plan_capped", {"kept": max_sub_tasks})
    trace.log(
        "project_plan_generated",
        {
            "sub_tasks": [
                {k: st[k] for k in ("id", "description", "criteria")}
                for st in sub_tasks
            ]
        },
    )
    return sub_tasks, f"decomposed into {len(sub_tasks)} sub-tasks"


def _criteria_coverage_gap(
    criteria: List[Dict[str, str]], sub_tasks: List[Dict[str, Any]]
) -> List[str]:
    """Criterion ids no sub-task claims (the plan's coverage gaps).

    Assumes criteria is [{id, ...}] and each sub_task carries a
    "criteria" id list. Returns the uncovered ids (empty = full
    coverage).
    """
    wanted = {c["id"] for c in criteria}
    claimed = {cid for st in sub_tasks for cid in st.get("criteria", [])}
    return sorted(wanted - claimed)


def _next_sub_task(
    sub_tasks: List[Dict[str, Any]], completed: List[int]
) -> Optional[Dict[str, Any]]:
    """The first sub-task (in order) not in the completed-id set."""
    for st in sub_tasks:
        if int(st["id"]) not in completed:
            return st
    return None


def _resolve_start_tree(log_root: Path, project: Dict[str, Any]) -> Optional[str]:
    """The repo path the next sub-task starts from: the accumulated
    tree of the last completed sub-task, or the ORIGINAL repo.

    Returns None when the recorded current_tree is missing on disk
    (the caller aborts honestly rather than rebuilding from a possibly
    diverged original — an unverified continuation is worse than a
    clean error).
    """
    cur = project.get("current_tree")
    if not cur:
        return str(project["repo_path"])
    p = Path(cur)
    if not p.is_dir():
        return None
    return str(p)


def run_project(
    request_text: str,
    repo_path: str,
    config: Optional[Dict[str, Any]] = None,
    log_root: Optional[Path] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a large feature across sessions; verifier-gated completion.

    Returns {"project_id", "status", "criteria", "sub_tasks",
    "completed", "sessions", "note", "trace_path"} where status is one
    of "success" (every sub-task verified + full coverage + final
    suite green), "checkpointed" (session budget exhausted mid-plan —
    the documented multi-session pause; call again with
    project_resume=True or the same project_id to continue),
    "already_exists" (every criterion's feature already passes —
    nothing to build), "error" (planning/verification failure), or the
    forwarded sub-task failure status ("failed"/"timeout").

    Assumes repo_path is a readable directory (the ORIGINAL repo —
    never mutated; each sub-task works in its own build-mode base
    copy) and config is the session/task config dict.
    """
    from harness.build_mode import run_build

    cfg = get_config(config or {})
    pid = project_id or f"project-{uuid.uuid4().hex[:8]}"
    root = Path(log_root) if log_root else Path(cfg.get("work_subdir", "logs"))
    trace = TraceLogger(root / pid)
    model = ModelClient(trace, cfg)
    verify = _get_verify()

    resuming = False
    project: Optional[Dict[str, Any]] = None
    if cfg.get("project_resume") or (
        project_id and (root / pid / PROJECT_FILE).exists()
    ):
        project = read_project(root / pid)
        if project is not None and project.get("sub_tasks"):
            resuming = True

    trace.log(
        "project_start",
        {
            "project_id": pid,
            "resumed": resuming,
            "request_text": request_text or project.get("request_text", ""),
            "repo_path": repo_path,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    # -- resume of a FINISHED project is an honest no-op ---------------
    if resuming and project.get("status") in ("success", "already_exists"):
        trace.log("project_end", {"status": "success", "reason": "already complete"})
        return {
            "project_id": pid,
            "status": project.get("status"),
            "criteria": project.get("criteria", []),
            "sub_tasks": project.get("sub_tasks", []),
            "completed": project.get("completed", []),
            "sessions": project.get("sessions", 0),
            "note": "project already complete; nothing to resume",
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }

    # -- fresh stages 1+2: criteria extraction + decomposition --------
    if not resuming:
        criteria, note = extract_acceptance_criteria(
            request_text, repo_path, cfg, trace, model
        )
        if criteria is None:
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": [],
                "sub_tasks": [],
                "completed": [],
                "sessions": 1,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        sub_tasks, note = plan_project(
            request_text, repo_path, criteria, cfg, trace, model
        )
        if sub_tasks is None:
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": [],
                "completed": [],
                "sessions": 1,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        gap = _criteria_coverage_gap(criteria, sub_tasks)
        if gap:
            # A plan that leaves criteria uncovered cannot ever complete
            # the contract — honest error, never a partial effort.
            note = f"plan does not cover criteria: {', '.join(gap)}"
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": [],
                "sessions": 1,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        project = {
            "project_id": pid,
            "request_text": request_text,
            "repo_path": str(repo_path),
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": [],
            "current_tree": None,
            "sessions": 0,
            "status": "active",
        }
        _write_project(root / pid, project)
        trace.log(
            "project_plan_saved",
            {"sub_tasks": len(sub_tasks), "criteria": len(criteria)},
        )

    criteria = project["criteria"]
    sub_tasks = project["sub_tasks"]
    completed = list(project.get("completed", []))
    sessions = int(project.get("sessions", 0)) + 1
    budget = int(cfg.get("project_sub_tasks_per_session", 1))
    # Sub-tasks completed by an actual BUILD this session (vs completed
    # by verification — already_exists). The whole-project
    # already_exists verdict needs "nothing was built anywhere", which a
    # resumed project must judge across sessions: a pinned current_tree
    # proves at least one earlier sub-task BUILT, so it zeroes built
    # only when no accumulated tree exists.
    built = 0 if not project.get("current_tree") else 1

    # -- already-complete check for a resumed-but-unstarted project ----
    # (a project whose every criterion already passes is reported
    # honestly, exactly like build_mode's already_exists)
    if not completed:
        start = _resolve_start_tree(root, project)
        if start is None:
            note = "recorded current_tree is missing on disk"
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        # The criteria are behavioral sentences, not runnable node ids
        # — the per-sub-task acceptance tests encode them. An empty
        # completed list with criteria to cover always has work to do;
        # the first sub-task's run_build performs the honest
        # already-passing check against ITS authored tests.

    done_this_session = 0
    while True:
        st = _next_sub_task(sub_tasks, completed)
        if st is None:
            break
        if done_this_session >= budget:
            # -- session checkpoint (the multi-session pause) ----------
            project.update(
                {
                    "completed": completed,
                    "current_tree": project.get("current_tree"),
                    "sessions": sessions,
                    "status": "checkpointed",
                }
            )
            _write_project(root / pid, project)
            remaining = len(sub_tasks) - len(completed)
            trace.log(
                "project_checkpoint",
                {
                    "completed": completed,
                    "remaining": remaining,
                    "sessions": sessions,
                },
            )
            return {
                "project_id": pid,
                "status": "checkpointed",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": (
                    f"session budget reached after {done_this_session} "
                    f"sub-task(s); {remaining} remaining — resume with "
                    f"project_resume=True (project_id {pid})"
                ),
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }

        start_tree = _resolve_start_tree(root, project)
        if start_tree is None:
            project.update(
                {"completed": completed, "sessions": sessions, "status": "error"}
            )
            _write_project(root / pid, project)
            note = "recorded current_tree is missing on disk"
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }

        # -- one sub-task = one unchanged build-mode session -----------
        # The sub-task's description + its criteria sentences become the
        # sub-request: the acceptance-test authoring call sees exactly
        # what this sub-task must deliver.
        crit_descs = [
            c["description"] for c in criteria if c["id"] in (st.get("criteria") or [])
        ]
        sub_request = st["description"]
        if crit_descs:
            sub_request += (
                "\n\nAcceptance criteria this sub-task must satisfy:\n- "
                + "\n- ".join(crit_descs)
            )
        sub_tid = f"{pid}-s{int(st['id'])}"
        trace.log(
            "project_sub_task_start",
            {
                "sub_task_id": int(st["id"]),
                "description": st["description"],
                "criteria": st.get("criteria", []),
                "start_tree": start_tree,
                "build_task_id": sub_tid,
            },
        )
        sub_cfg = dict(cfg)
        sub_cfg.pop("project_resume", None)
        sub_cfg.pop("build_project", None)
        sub_cfg["mode"] = "build"
        if st.get("files_hint"):
            sub_cfg.setdefault("retrieval_hint_files", list(st["files_hint"]))
        out = run_build(
            request_text=sub_request,
            repo_path=start_tree,
            config=sub_cfg,
            log_root=root,
            task_id=sub_tid,
        )
        sub_status = str(out.get("status", "error"))

        if sub_status == "already_exists":
            # The sub-task's authored acceptance tests already pass on
            # the start tree. For sub-task 1 on the ORIGINAL repo this
            # means the feature already exists (honest report). For a
            # LATER sub-task it means the accumulated tree already
            # satisfies this sub-task's contract (e.g. a later
            # sub-task's criteria were delivered early by an earlier
            # one) — the sub-task is complete BY VERIFICATION, not
            # skipped: its tests passed on a real tree through the
            # real verifier.
            trace.log(
                "project_sub_task_already_passing",
                {"sub_task_id": int(st["id"])},
            )
            completed.append(int(st["id"]))
            # A by-verification completion is not a build: it does not
            # consume the session budget (an all-already-passing
            # project resolves in ONE session, not N).
            project.update(
                {
                    "completed": completed,
                    "sessions": sessions,
                    "status": "active",
                }
            )
            _write_project(root / pid, project)
            continue

        if sub_status != "success":
            # A sub-task failure ends the session honestly; the
            # project file keeps the completed set (a LATER session can
            # retry the failed sub-task from the same checkpoint).
            project.update(
                {
                    "completed": completed,
                    "sessions": sessions,
                    "status": sub_status,
                }
            )
            _write_project(root / pid, project)
            trace.log(
                "project_end",
                {"status": sub_status, "failed_sub_task": int(st["id"])},
            )
            return {
                "project_id": pid,
                "status": sub_status,
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": (
                    f"sub-task {st['id']} ({st['description']}) failed: "
                    f"{sub_status} — project state checkpointed; resume "
                    f"to retry this sub-task"
                ),
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }

        # -- success: carry the sub-task's verified work/ tree forward --
        sub_work = root / sub_tid / "work"
        if not sub_work.is_dir():
            project.update(
                {
                    "completed": completed,
                    "sessions": sessions,
                    "status": "error",
                }
            )
            _write_project(root / pid, project)
            note = f"sub-task {st['id']} succeeded but its work tree is missing"
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        # Pin the accumulated tree: copy the verified work/ into a
        # stable, project-owned location ({pid}.tree-s<N>) that later
        # sessions start from. The sub-task's own task dir may be
        # archived by later runs of the same sub_tid; the pinned tree
        # must not be.
        pinned = root / f"{pid}.tree-s{int(st['id'])}"
        try:
            if pinned.exists():
                shutil.rmtree(pinned, ignore_errors=True)
            shutil.copytree(
                sub_work,
                pinned,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "*.pyc", ".git"
                ),
            )
        except OSError as exc:
            project.update(
                {
                    "completed": completed,
                    "sessions": sessions,
                    "status": "error",
                }
            )
            _write_project(root / pid, project)
            note = f"pinning accumulated tree failed: {exc}"
            trace.log("project_end", {"status": "error", "reason": note})
            return {
                "project_id": pid,
                "status": "error",
                "criteria": criteria,
                "sub_tasks": sub_tasks,
                "completed": completed,
                "sessions": sessions,
                "note": note,
                "trace_path": str((root / pid / "trace.jsonl").resolve()),
            }
        completed.append(int(st["id"]))
        done_this_session += 1
        built += 1
        project.update(
            {
                "completed": completed,
                "current_tree": str(pinned),
                "sessions": sessions,
                "status": "active",
            }
        )
        _write_project(root / pid, project)
        trace.log(
            "project_sub_task_end",
            {
                "sub_task_id": int(st["id"]),
                "ok": True,
                "accumulated_tree": str(pinned),
                "completed": completed,
            },
        )

    # -- every sub-task done: the project-level completion gate --------
    # 1) coverage: every criterion id claimed by a completed sub-task.
    claimed = {
        cid
        for st in sub_tasks
        if int(st["id"]) in completed
        for cid in st.get("criteria", [])
    }
    uncovered = sorted({c["id"] for c in criteria} - claimed)
    if uncovered:
        # Cannot happen via the fresh-plan coverage gate, but a resumed
        # project file could have been edited; verify, never trust.
        project.update(
            {"completed": completed, "sessions": sessions, "status": "error"}
        )
        _write_project(root / pid, project)
        note = f"completed sub-tasks do not cover criteria: {', '.join(uncovered)}"
        trace.log("project_end", {"status": "error", "reason": note})
        return {
            "project_id": pid,
            "status": "error",
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": completed,
            "sessions": sessions,
            "note": note,
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }

    # 1b) every sub-task completed BY VERIFICATION on its start tree
    # (already_exists from run_build) — nothing was built anywhere:
    # the whole feature already exists. Honest report, never a fake
    # "built" success. No final verify runs: with no pinned tree the
    # only candidate tree is the ORIGINAL repo, which the sandbox
    # mounts READ-WRITE — verifying it would mutate the never-mutate
    # guarantee for a verdict we already hold (each sub-task's tests
    # passed through the real verifier on the tree that mattered).
    if built == 0:
        project.update(
            {"completed": completed, "sessions": sessions, "status": "already_exists"}
        )
        _write_project(root / pid, project)
        trace.log(
            "project_end",
            {"status": "already_exists", "reason": "every sub-task already passing"},
        )
        return {
            "project_id": pid,
            "status": "already_exists",
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": completed,
            "sessions": sessions,
            "note": (
                "every sub-task's acceptance tests already pass on the "
                "current repo — the whole feature exists; nothing to build"
            ),
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }

    # 2) final full-suite verify on the accumulated tree (targetless:
    # the whole suite is the gate — every prior sub-task's regression
    # already ran per-sub-task; this is the project-level confirmation
    # that the accumulated whole is green).
    final_tree = _resolve_start_tree(root, project)
    if final_tree is None:
        project.update(
            {"completed": completed, "sessions": sessions, "status": "error"}
        )
        _write_project(root / pid, project)
        note = "accumulated tree missing for the final verify"
        trace.log("project_end", {"status": "error", "reason": note})
        return {
            "project_id": pid,
            "status": "error",
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": completed,
            "sessions": sessions,
            "note": note,
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }
    try:
        final_v = verify(
            final_tree,
            None,
            rerun_for_flake_check=0,
            test_command=None,
            verify_timeout_s=int(cfg["verify_timeout_s"]),
        )
    except Exception as exc:
        project.update(
            {"completed": completed, "sessions": sessions, "status": "error"}
        )
        _write_project(root / pid, project)
        note = f"final project verify crashed: {exc}"
        trace.log("project_end", {"status": "error", "reason": note})
        return {
            "project_id": pid,
            "status": "error",
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": completed,
            "sessions": sessions,
            "note": note,
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }
    trace.log(
        "project_final_verify",
        {"tree": final_tree, "regression_passed": final_v.regression_passed},
    )
    if not final_v.regression_passed:
        project.update(
            {"completed": completed, "sessions": sessions, "status": "failed"}
        )
        _write_project(root / pid, project)
        trace.log(
            "project_end",
            {"status": "failed", "reason": "final suite verify failed"},
        )
        return {
            "project_id": pid,
            "status": "failed",
            "criteria": criteria,
            "sub_tasks": sub_tasks,
            "completed": completed,
            "sessions": sessions,
            "note": "final full-suite verify on the accumulated tree failed",
            "trace_path": str((root / pid / "trace.jsonl").resolve()),
        }

    project.update(
        {
            "completed": completed,
            "sessions": sessions,
            "status": "success",
            "current_tree": project.get("current_tree"),
        }
    )
    _write_project(root / pid, project)
    trace.log(
        "project_end",
        {
            "status": "success",
            "completed": completed,
            "criteria": len(criteria),
            "sessions": sessions,
        },
    )
    return {
        "project_id": pid,
        "status": "success",
        "criteria": criteria,
        "sub_tasks": sub_tasks,
        "completed": completed,
        "sessions": sessions,
        "note": (
            f"all {len(sub_tasks)} sub-tasks verified; "
            f"{len(criteria)} criteria covered; final suite green"
        ),
        "trace_path": str((root / pid / "trace.jsonl").resolve()),
    }
