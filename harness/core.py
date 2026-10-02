"""The loop controller — harness.core.run_task (INTERFACES.md Boundary 3).

Flow per task:
  1. Set up logs/{task_id}/: working copy of the repo, pristine snapshot,
     trace logger, structured state file (Boundary 4 schema).
  2. Baseline verify on the pristine copy (what passes BEFORE any edit).
  3. Plan: the model decomposes the issue into 2-4 small sub-steps, each
     with a pass/fail checkpoint (spec item 14 — no one-shotting).
  4. Execute sub-steps, each in a FRESH bash session (deliberate per-step
     context reset — spec item 12), with constraint re-injection appended
     to every tool result (spec item 15).
  5. After each sub-step: validate edits (syntax / protected paths), then
     verify. Early exit: if the verifier already confirms target + no
     regression, the task is done even if planned steps remain.
  6. If the attempt ends without verification, retry with failure
     feedback (Phase 3 adds failure classification; naive retry + feedback
     is the Phase 1 baseline).
  7. Stopping conditions checked at every iteration: max retries, budget
     cap, wall-clock cap (all from task.config via harness.config).

Resume contract (crash recovery — see INTERFACES.md Change Log): when
task.config["resume"] is truthy and a prior logs/{task_id}/ holds a
state.json with completed steps plus a plan.json, run_task CONTINUES that
run instead of archiving it: the persisted plan is reused, completed
steps are skipped, the surviving work/ copy (with its partial edits) is
built upon, and the in-flight attempt + spent budget continue rather
than restart. This is what makes a scheduler-killed task survive its
relaunch (the runtime's checkpoint/resume relies on it).

Completion is ALWAYS verifier-gated (spec item 17): "success" is only set
when verify() confirms the target test passes on the working copy — never
on the model's own claim (SUBMIT only ends a step session).

On a VERIFIED fix, product-grade output runs (spec items 26/29), both
best-effort — a failure there degrades to a trace event, never taints the
verified result:
  - logs/{task_id}/rationale.md: one grounded paragraph (what was wrong /
    what changed / why) built from trace.jsonl + state.json by
    execution.rationale (written for every terminal outcome when enabled).
  - git-native output via execution.git_output in the harness's PRIVATE
    work/ copy (git init + pristine first commit + fix commit on a
    harness/fix-* branch — the original repo is never touched): branch,
    commit sha, commit message, PR description recorded in the trace
    ("git_output" event) and TaskResult.model_calls-adjacent summary
    fields (branch/commit live in the trace + logs/{task_id}/git.json;
    TaskResult's Boundary-3 shape is unchanged).

run_task holds no shared mutable state, so concurrent calls with distinct
task_ids are safe (the runtime's scheduler calls this concurrently).
"""

import hashlib
import inspect
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness import agent_tests as agent_tests_mod
from harness import context, decision_memory, editor, prompts, retrieval, tool_errors
from harness import coordination as coordination_mod
from harness import docs_lookup as docs_lookup_mod
from harness import lint as lint_mod
from harness import skills as skills_mod
from harness import steering as steering_mod
from harness import tools as tool_mod
from harness import webfetch as webfetch_mod
from harness.config import get_config
from harness.context import TaskState
from harness.model_client import ModelClient
from harness.state_machine import TaskStateMachine
from harness.trace import TraceLogger
from shared.types import Task, TaskResult, VerificationResult


def _get_verify() -> Callable[..., VerificationResult]:
    """Resolve the verification boundary through the explicit harness policy."""
    from harness.deps import get_verify

    return get_verify()


#: VEX-PF-10: the keyword-only parameters this module may hand to a resolved
#: verification boundary. They are forwarded ONLY when the boundary NAMES them,
#: the same rule ``harness.model_client`` applies to ``effort`` and
#: ``BashSession`` applies to ``cancellation_token``. Presence of ``**kwargs``
#: is not evidence: ``harness/_stubs/verify.py`` and every scripted double in
#: the suite carry the historical five-parameter signature, and handing an
#: undeclared keyword to one is a run-killing ``TypeError`` rather than a
#: degraded feature. The real boundary (``execution.verify.verify``) names all
#: three, so production is fully wired.
_VERIFY_RUNG_KWARGS = ("rung_config", "run_dir", "phase")


def _verify_rung_kwargs(
    verify: Callable[..., Any], cfg: Dict[str, Any], **extra: Any
) -> Dict[str, Any]:
    """Return the rung keywords this boundary can accept, or ``{}``.

    Assumes ``verify`` is the resolved boundary callable and ``cfg`` the run's
    merged config. An uninspectable callable (a C function, a mock whose
    signature cannot be read) gets ``{}`` — the historical call — because a
    keyword it might reject is worse than a rung that is merely not recorded.
    """
    try:
        parameters = inspect.signature(verify).parameters
    except (TypeError, ValueError):
        return {}
    accepted = {name: value for name, value in extra.items() if name in parameters}
    if not any(name in parameters for name in _VERIFY_RUNG_KWARGS):
        return {}
    accepted["rung_config"] = cfg
    return accepted


def _normalize_plan_path(value: Any) -> Optional[str]:
    """Normalize a planner file hint or reject an unsafe path."""
    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/").strip()
    if not text or "\x00" in text or text.startswith("/"):
        return None
    if re.match(r"^[A-Za-z]:/", text):
        return None
    parts: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) or None


def _parse_plan_json(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse and validate the planner's JSON response.

    Code fences and surrounding prose are tolerated. Empty, malformed,
    duplicate-id, overlong, or path-escaping plans return None so the
    caller can request a repair rather than executing an invented plan.
    """
    text = raw or ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    steps = obj.get("plan") if isinstance(obj, dict) else None
    if not isinstance(steps, list) or not steps or len(steps) > 4:
        return None
    out: List[Dict[str, Any]] = []
    seen_ids: set[int] = set()
    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict):
            return None
        try:
            step_id = int(st.get("id") or i)
        except (TypeError, ValueError):
            return None
        if step_id <= 0 or step_id in seen_ids:
            return None
        seen_ids.add(step_id)
        description = str(st.get("description") or "").strip()
        checkpoint = str(st.get("checkpoint") or "").strip()
        if not description or not checkpoint:
            return None
        raw_hints = st.get("files_hint") or []
        if not isinstance(raw_hints, list):
            return None
        hints: List[str] = []
        for value in raw_hints:
            normalized = _normalize_plan_path(value)
            if normalized is None:
                return None
            if normalized not in hints:
                hints.append(normalized)
        out.append(
            {
                "id": step_id,
                "description": description,
                "checkpoint": checkpoint,
                "files_hint": hints,
                "change_group": str(st.get("change_group") or "") or None,
            }
        )
    return out or None


def _plan_steps_valid(plan: Any) -> bool:
    """Return whether a persisted plan has the minimum safe step shape."""
    if not isinstance(plan, list) or not plan or len(plan) > 4:
        return False
    ids: set[int] = set()
    for step in plan:
        if not isinstance(step, dict):
            return False
        try:
            step_id = int(step.get("id"))
        except (TypeError, ValueError):
            return False
        if step_id <= 0 or step_id in ids:
            return False
        ids.add(step_id)
        if not str(step.get("description") or "").strip():
            return False
        if not str(step.get("checkpoint") or "").strip():
            return False
    return True


def _plan_change_groups(plan: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Union the change_group-declared file sets across a parsed plan.

    Assumes plan is _parse_plan_json output (steps may carry
    "change_group": name + files_hint). Returns {group_name: sorted
    unique posix file list}; a step without a group contributes
    nothing. Files are normalized to forward slashes."""
    groups: Dict[str, List[str]] = {}
    for st in plan or []:
        name = st.get("change_group")
        if not name:
            continue
        files = {
            normalized
            for value in (st.get("files_hint") or [])
            for normalized in [_normalize_plan_path(value)]
            if normalized is not None
        }
        groups.setdefault(str(name), set()).update(files)
    return {name: sorted(files) for name, files in groups.items()}


def _touched_group_files(
    change_groups: Dict[str, List[str]],
    changed_files: List[str],
) -> List[str]:
    """Union of EVERY file of each declared group holding >=1 changed file.

    Atomicity helper: when a coordinated change fails, the WHOLE group
    reverts (including members that didn't change) — never the one file
    a naive diff might single out. Assumes change_groups is the
    state-declared {name: [files]} map and changed_files is editor
    output (repo-relative posix)."""
    changed_set = {c.replace("\\", "/") for c in (changed_files or [])}
    out: List[str] = []
    for gfiles in (change_groups or {}).values():
        if any(f in changed_set for f in gfiles):
            out.extend(gfiles)
    return sorted(set(out))


def _safe_context_file(repo_path: str, rel: str) -> Optional[Path]:
    """Resolve a repo-relative context path without following an escape."""
    text = str(rel or "").replace("\\", "/").strip()
    if not text or "\x00" in text:
        return None
    candidate_rel = Path(text)
    if (
        candidate_rel.is_absolute()
        or ".." in candidate_rel.parts
        or re.match(r"^[A-Za-z]:[\\/]", text)
    ):
        return None
    try:
        root = Path(repo_path).resolve()
        candidate = (root / candidate_rel).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _authorized_test_target(cfg: Dict[str, Any], issue_text: str) -> bool:
    """Return whether a task explicitly authorizes its declared test target."""
    target = str(cfg.get("target_test") or "").replace("\\", "/")
    if not target.startswith("tests/") and "/" not in target:
        return False
    return "authorizes adding test files" in (issue_text or "").lower()


def _log_step_prompt_cache(
    trace: TraceLogger,
    messages: List[Dict[str, str]],
    *,
    step_id: int,
    split: bool,
    tools_sent: int = 0,
) -> Dict[str, Any]:
    """REPORT the step prompt's cacheability; never gate on it.

    Returns the receipt and writes it as a `step_prompt_cache` trace row. The
    receipt is the *input* to a cache decision - the digest of the leading
    prefix, where the breakpoint sits, and roughly how many tokens it holds -
    computed by `runtime.prompt_cache`, the module that owns that vocabulary.

    Two things this deliberately does NOT do:

    - It does not assert a hit rate. A hit rate is a property of the PROVIDER
      (whether the endpoint implements caching, and whether the prefix is above
      the provider's own floor). The provider's answer arrives on the
      `model_routed` rows and in `get_last_usage()`; this row is reported
      alongside it so a reader can see the two together, and a 0% or `unreported`
      rate is a fact about the endpoint, not a defect to fail a task over.
    - It never raises. Observability must not change a task's outcome; a broken
      or absent cache module yields an `available: false` receipt.
    """
    receipt: Dict[str, Any] = {
        "step_id": int(step_id),
        "split": bool(split),
        "message_count": len(messages or []),
        "tools_sent": int(tools_sent),
        "available": False,
    }
    try:
        from runtime.prompt_cache import prefix_digest, split_at_breakpoint

        prefix, suffix, index = split_at_breakpoint(messages)
        receipt.update(
            {
                "available": True,
                "prefix_messages": len(prefix),
                "suffix_messages": len(suffix),
                "cache_breakpoint_index": int(index),
                "prefix_digest": prefix_digest(messages),
                "prefix_chars": sum(
                    len(str(item.get("content") or "")) for item in prefix
                ),
                "system_message_count": sum(
                    1 for item in (messages or []) if item.get("role") == "system"
                ),
            }
        )
    except Exception as exc:  # observability must never kill a run
        receipt["error"] = str(exc)[:200]
    try:
        trace.log("step_prompt_cache", dict(receipt))
    except Exception:
        pass
    return receipt


def _context_block(
    repo_path: str, rel_files: List[str], max_lines: int, max_files: int
) -> str:
    """Render file contents for prompt injection (curated per step —
    spec item 16: only what's relevant to the current sub-step)."""
    parts: List[str] = []
    try:
        max_files = max(1, min(20, int(max_files)))
    except (TypeError, ValueError):
        max_files = 4
    try:
        max_lines = max(1, min(500, int(max_lines)))
    except (TypeError, ValueError):
        max_lines = 60
    for rel in rel_files[:max_files]:
        p = _safe_context_file(repo_path, rel)
        try:
            if p is None or not p.is_file() or p.stat().st_size > 200_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if len(lines) > max_lines:
            shown = [*lines[:max_lines], f"... [{len(lines) - max_lines} more lines]"]
        else:
            shown = lines
        display = str(rel).replace("\\", "/")
        parts.append(f"### {display}\n```\n" + "\n".join(shown) + "\n```")
    return "\n\n".join(parts)


def _safe_task_segment(value: Any) -> bool:
    """Return whether a task id is one safe directory segment."""
    text = str(value or "")
    if not text or text != text.strip() or "\x00" in text:
        return False
    if any(ch in text for ch in '/\\:*?"<>|'):
        return False
    normalized = text.rstrip(". ")
    return bool(normalized) and normalized not in (".", "..")


def _safe_log_segment(task_id: Any) -> str:
    """Return a contained log directory name for any task id."""
    text = str(task_id or "")
    if _safe_task_segment(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"invalid-task-{digest}"


class TaskPaths:
    """Filesystem layout for one task run under logs/{task_id}/."""

    def __init__(self, log_root: Path, task_id: str) -> None:
        if not _safe_task_segment(task_id):
            raise ValueError("task_id must be one safe path segment")
        self.log_root = log_root
        self.log_dir = log_root / task_id
        self.pristine = self.log_dir / "pristine"
        self.work = self.log_dir / "work"


def _run_task_legacy(
    task: Task,
    log_root: Optional[Path] = None,
    *,
    _trace: Optional[TraceLogger] = None,
    _reuse_run_dir: bool = False,
) -> TaskResult:
    """Run the historical fix loop behind the verified-fix kernel strategy.

    Assumes task.repo_path is a readable directory and task.config carries
    all tunables (merged over harness.config.DEFAULTS). NEVER mutates the
    original repo — the agent works in logs/{task_id}/work/. Returns a
    TaskResult whose status is "success" ONLY when the verifier confirmed
    the target test passes on the working copy (verifier-gated completion).

    With task.config["resume"] truthy and a resumable prior run of this
    task_id in log_root, continues that run (see module docstring).

    Thread-safety: no module-level mutable state; concurrent calls with
    distinct task_ids are safe.
    """
    cfg = get_config(task.config)
    started = time.time()
    deadline = started + float(cfg["max_wallclock_s"])
    log_root = Path(log_root or Path(cfg["work_subdir"])).expanduser().resolve()
    log_segment = _safe_log_segment(task.task_id)
    # Shared docs-cache root (Round 8, Task D): harness-owned, OUTSIDE
    # the repo (never-mutate guarantee), shared across tasks on this
    # logs tree (same convention as logs/_code-graph/). Seeded as a
    # private "_"-key so run_step can find it without a new parameter.
    cfg["_docs_cache_root"] = str(log_root / "_docs-cache")

    # Language awareness (multi-language round): the repo's language
    # flavors the step prompt (node/npx vs python phrasing). Detected
    # from the repo unless the task config pins it. Private "_" key —
    # harness-internal, not a config contract.
    if not cfg.get("_repo_language"):
        cfg["_repo_language"] = _detect_repo_language(task.repo_path)

    # Resume contract (INTERFACES.md Change Log 2026-09-07, Terminal 3):
    # a relaunch with task.config["resume"] truthy continues an interrupted
    # run of the SAME task_id — logs/{task_id}/ is KEPT (state.json is the
    # progress authority), completed steps are skipped, and the surviving
    # work/ dir (which holds the pre-crash edits) is continued. Without
    # resume (or with no prior state.json), the directory is archived as
    # before and the task starts fresh.
    resuming = False
    resumed_plan: Optional[List[Dict[str, Any]]] = None
    resumed_attempts = 1
    resumed_cost = 0.0
    prior_dir = log_root / log_segment
    if cfg.get("resume") and prior_dir.exists():
        prior = context.read_state(prior_dir)
        bookkeeping = context.read_plan_bookkeeping(prior_dir)
        if (
            prior is not None
            and bool(prior.get("completed_steps"))
            and bookkeeping is not None
            and _plan_steps_valid(bookkeeping[0])
        ):
            resuming = True
            resumed_plan, resumed_attempts, resumed_cost = bookkeeping

    paths = _fresh_paths(
        log_root,
        log_segment,
        resuming=resuming or _reuse_run_dir,
    )
    trace = _trace or TraceLogger(paths.log_dir)
    state = TaskState(
        paths.log_dir, task.task_id, repo_path=task.repo_path, resume=resuming
    )
    # Modes round (additive): a task routed through a non-fix mode
    # (build) carries config["mode"]; state.json records it so sessions/
    # dashboards can tell builds from fixes. Fix-mode tasks never set
    # the key (schema unchanged for them).
    _mode = str(cfg.get("mode") or "")
    if _mode and _mode != "fix":
        state.set_mode(_mode)
    verify = _get_verify()
    model = ModelClient(trace, cfg)

    # Steering round, Task B — the formal state machine finally WIRED
    # into run_task (the module + tests predate this round; the wiring
    # was lost in the documented git-hook incident and never re-landed).
    # begin() records the run-start transition (planning fresh /
    # repairing resumed); every phase change below goes through
    # machine.transition(). The hook mirrors the phase into state.json's
    # additive "phase" key (state_machine's documented live-status
    # contract) — a broken hook never kills the run.
    def _on_phase(from_state, to_state, reason):
        try:
            state.set_phase(to_state)
        except Exception:
            pass

    machine = TaskStateMachine(paths.log_dir, on_transition=_on_phase)
    machine.begin("run_task start", resuming=resuming)

    # Steering round, Task A — the mid-task steering inbox. The journal
    # (logs/{task_id}/steering.jsonl) replays on construction, so an
    # inject that arrived before a crash (or before a resume) is STILL
    # PENDING here and applies to the resumed run (Task C). Disabled
    # config = never polled (the OFF arm is exactly one code path).
    steer = (
        steering_mod.SteeringBuffer(
            paths.log_dir,
            task.task_id,
            max_pending=int(cfg.get("max_pending_steering", 16)),
        )
        if cfg.get("steering_enabled", True)
        else None
    )

    # VEX-CEILING-07: ONE recovery policy and ONE bounded model-retry budget
    # per task (not per attempt or per step), so the forbidden-path set, the
    # escalated timeout, the loop guard and the turns-to-recovery statistic
    # all span the whole run — which is what makes them a policy rather than
    # a per-turn reaction.
    recovery_policy = tool_errors.recovery_policy_from_config(cfg, root=str(paths.work))
    model_recovery = tool_errors.ModelRecovery(
        trace=trace,
        max_attempts=int(cfg.get("max_model_attempts", 3)),
        base_backoff_s=float(cfg.get("model_retry_base_s", 0.5)),
        cap_backoff_s=float(cfg.get("model_retry_cap_s", 8.0)),
        label=task.task_id,
    )

    trace.log(
        "task_start",
        {
            "task_id": task.task_id,
            "repo_path": task.repo_path,
            "issue_text": task.issue_text,
            "resumed": resuming,
            "config": {k: v for k, v in cfg.items() if k != "api_key"},
        },
    )

    def elapsed() -> float:
        return time.time() - started

    # R2-14 - the per-call budget pre-check. `budget_cap_usd` used to be
    # checked ONLY at attempt start, so one attempt could overshoot the cap
    # by a large multiple (a previous round measured 9.28x a deliberately
    # tiny cap: one-attempt-bounded, but unbounded in principle). The
    # governor prices the NEXT call before it is dialed and refuses one that
    # cannot fit, and the attempt-level check below stays as the backstop --
    # both read the SAME governor, so they cannot disagree about the cap.
    #
    # The governor is OPTIONAL and resolved defensively: a harness-only
    # install without `runtime`, or a caller that never installed one, gets
    # the historical attempt-level behaviour byte-for-byte. `current()` reads
    # the execution-context governor first and falls back to the one the
    # worker placed in the router context, because the two are installed
    # separately and either may be absent.
    governor = _resolve_budget_governor()

    def current() -> Optional[Any]:
        """Return the task's budget governor, or None when unbound."""
        return governor

    def over_budget() -> bool:
        active = current()
        if active is not None:
            # The governor is the authority when it exists: it knows both
            # the harness's own total and any provider-fallback charges the
            # harness cannot see, and it latches on the first per-call
            # refusal. Below it, the historical comparison is preserved
            # exactly, so an unbound run is unchanged.
            return bool(active.exhausted) or (
                model.total_cost_usd >= float(cfg["budget_cap_usd"])
            )
        return model.total_cost_usd >= float(cfg["budget_cap_usd"])

    if governor is not None:
        # ONE spend authority: the governor's `spent_usd` takes the maximum
        # of its own accumulated charges and this client, so neither can
        # under-report the cap. `ModelClient` never sees a provider
        # fallback's earlier charges, and the governor never sees the
        # difficulty-classifier call; the max covers both without summing
        # them (which would double-count the calls both saw).
        governor.bind_spend_source(lambda: model.total_cost_usd)
        _bind_budget_guard(governor, model, trace, task.task_id)
    else:
        # Not a silent downgrade: a run configured with a cap and no
        # governor gets attempt-level enforcement only, and says so.
        trace.log(
            "budget_governor_absent",
            {
                "reason": "no runtime.budget_governor installed for this run",
                "granularity": "attempt",
            },
        )

    def over_time() -> bool:
        return time.time() >= deadline

    # -- 1. working copy + pristine snapshot ---------------------------
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        if resuming and paths.pristine.is_dir() and paths.work.is_dir():
            # The pre-crash copies survived the kill: keep them. work/
            # holds the partial edits this resume continues from, and
            # re-snapshotting would throw both away.
            state.record_decision(
                "resumed from interrupted run; kept surviving work copy"
            )
            trace.log(
                "resume",
                {
                    "completed_steps": list(state.completed_steps),
                    "files_touched": list(state.files_touched),
                    "prior_decisions": list(state.decisions),
                },
            )
        else:
            if resuming:
                # state.json existed but the copies did not (partial
                # archive/cleanup): can't continue a half-missing run —
                # fall back to a fresh start rather than resume nonsense.
                resuming = False
                state.reset_completed()
                trace.log(
                    "resume_aborted",
                    {"reason": "pristine/work missing; starting fresh"},
                )
            editor.snapshot(task.repo_path, str(paths.pristine))
            editor.snapshot(str(paths.pristine), str(paths.work))
    except OSError as exc:
        trace.log("task_end", {"status": "error", "reason": f"snapshot failed: {exc}"})
        return _result(task, "error", 0, None, None, model, trace)

    # -- 2. baseline verify (pristine copy) ----------------------------
    # On resume the baseline was established pre-crash (a passing baseline
    # would have ended the run before any step completed — resuming only
    # happens with completed steps). Re-running it would waste a verify
    # and cannot change the outcome: baseline_target_ok must be False.
    baseline_target_ok = False
    if not resuming:
        machine.transition("testing", "baseline verify on the pristine copy")
        try:
            base_v = verify(
                str(paths.pristine),
                cfg.get("target_test"),
                rerun_for_flake_check=0,
                test_command=cfg.get("test_command"),
                verify_timeout_s=int(cfg["verify_timeout_s"]),
                # VEX-PF-10: `phase="baseline"` is this call's declaration of
                # WHICH tree it is evaluating. verify() is stateless about that
                # and guessing from repo_path would be false precision, so the
                # recorded pre-existing failure set is written only when the
                # caller says this is the pristine phase. With no
                # `baseline_set_*` key present this is byte-identical.
                **_verify_rung_kwargs(
                    verify, cfg, run_dir=str(paths.log_dir), phase="baseline"
                ),
            )
            if not _verification_evidence_ok(base_v):
                raise ValueError("verifier returned incomplete evidence")
        except Exception as exc:  # verifier crash = task error, not attempt failure
            trace.log(
                "task_end",
                {"status": "error", "reason": f"baseline verify crashed: {exc}"},
            )
            return _result(task, "error", 0, None, None, model, trace)

        baseline_target_ok = base_v.target_test_passed
        trace.log(
            "baseline_verify",
            {
                "target_test": cfg.get("target_test"),
                "target_passed_on_pristine": baseline_target_ok,
                "flaky": base_v.flaky,
                # VEX-PF-10: which mechanism produced this verdict, so a reader
                # never has to guess from the surrounding code.
                "rung": getattr(base_v, "verification_rung", ""),
                "rungs": list(getattr(base_v, "verification_rungs", ()) or ()),
                "flake_check": getattr(base_v, "flake_check", ""),
                "repetitions": getattr(base_v, "repetitions", None),
                "raw": base_v.raw_output[-3000:],
            },
        )

        if baseline_target_ok:
            # A passing target is not sufficient when the pristine full
            # suite is already red or flaky. Preserve that evidence and
            # fail honestly instead of fabricating a green regression gate.
            baseline_result = VerificationResult(
                target_test_passed=True,
                baseline_passed=True,
                regression_passed=bool(base_v.regression_passed),
                flaky=bool(base_v.flaky),
                raw_output=base_v.raw_output,
                structured_feedback=list(
                    getattr(base_v, "structured_feedback", []) or []
                ),
            )
            if base_v.regression_passed and not base_v.flaky:
                state.record_decision(
                    "target test already passed on pristine repo; no fix needed"
                )
                machine.transition("done", "target already passes on pristine")
                trace.log("task_end", {"status": "success", "reason": "passes pre-fix"})
                _record_rationale_only(paths, task, cfg, trace)
                return _result(task, "success", 0, "", baseline_result, model, trace)
            state.record_decision(
                "target passed on pristine but the pristine regression suite "
                "was not clean; refusing a success verdict"
            )
            machine.transition(
                "failed", "pristine target passed but regression evidence is not clean"
            )
            trace.log(
                "task_end",
                {
                    "status": "failed",
                    "reason": "pristine regression suite failed or was flaky",
                    "regression_passed": base_v.regression_passed,
                    "flaky": base_v.flaky,
                },
            )
            _record_rationale_only(paths, task, cfg, trace)
            return _result(task, "failed", 0, "", baseline_result, model, trace)
        # Baseline confirmed failing: back to planning (the planner call
        # is the next phase).
        machine.transition("planning", "baseline confirmed failing; planning begins")

    # -- 3. retrieval + planning ---------------------------------------
    # index_root keeps the structural graph OUT of the original repo (the
    # agent never mutates it) while letting tasks on the same repo share
    # one index (the original repo is read-only, so its mtimes don't
    # change between tasks — CodeGraph.load_or_build reuses a fresh index).
    ctx = retrieval.retrieve_context(
        task.repo_path,
        task.issue_text,
        max_files=int(cfg["context_files_cap"]),
        target_test=cfg.get("target_test"),
        index_root=log_root / "_code-graph",
    )
    context_budget = retrieval.size_context_budget(
        task.issue_text,
        task.repo_path,
        files_touched=[],
        base_files=int(cfg["context_files_cap"]),
        base_lines=int(cfg["context_lines_cap"]),
    )
    ctx["files"] = list(ctx.get("files") or [])[: context_budget["max_files"]]
    trace.log(
        "retrieval",
        {
            "strategy": ctx.get("strategy"),
            "terms": ctx["terms"],
            "files": ctx["files"],
            "context_budget": context_budget,
        },
    )
    protected = [str(p) for p in (cfg.get("protected_paths") or [])]
    if _authorized_test_target(cfg, task.issue_text):
        protected = [
            p for p in protected if p not in {"tests/*", "test_*.py", "*_test.py"}
        ]

    # Decision-memory query (Round 2, memory-informed planning): ask
    # Terminal 4's store what earlier tasks in THIS repo learned, BEFORE
    # the planner runs — past conventions, gotchas, and architecture
    # choices shape the plan instead of being re-discovered (or re-
    # tripped over) from scratch. Best-effort by contract: any failure
    # degrades to an empty section; plan_with_memory=False skips the
    # query entirely (the ablation's OFF arm).
    memory_block = "(none recorded yet)"
    if cfg.get("plan_with_memory", True):
        mem = decision_memory.query_planning_decisions(
            repo_path=task.repo_path,
            issue_text=task.issue_text,
            retrieval_terms=ctx["terms"],
            limit=int(cfg.get("memory_query_limit", 6)),
        )
        memory_block = decision_memory.render_memory_block(
            mem["decisions"], max_chars=int(cfg.get("memory_max_chars", 1500))
        )
        trace.log(
            "decision_memory",
            {
                "query": mem["query"],
                "matched": len(mem["decisions"]),
                "error": mem["error"],
                "section_chars": len(memory_block),
            },
        )
    else:
        trace.log(
            "decision_memory", {"matched": 0, "skipped": "plan_with_memory=False"}
        )

    # Skills scan (Plugins round, Task A): before planning, discover the
    # available SKILL.md packs (project .neo/skills/ + global + plugin
    # roots) and inject any whose description plausibly applies to THIS
    # task as a planner prompt section. Best-effort by contract: a broken
    # scan degrades to "(none matched)" + trace error; skills_enabled=
    # False skips the scan entirely. Deliberately AFTER the memory block
    # and BEFORE Constraints — same after-the-cut placement discipline.
    skills_block = "(none matched)"
    skill_scan: Dict[str, Any] = {
        "skills_block": skills_block,
        "matched": [],
        "considered": 0,
        "receipts": [],
        "rendered": [],
        "omitted": [],
        "skipped": "skills_enabled=False",
        "error": None,
    }
    if cfg.get("skills_enabled", True):
        skill_scan = skills_mod.scan_skills_for_task(
            repo_path=task.repo_path,
            issue_text=task.issue_text,
            retrieval_terms=ctx["terms"],
            extra_roots=[str(r) for r in (cfg.get("skills_roots") or []) if r],
            max_skills=int(cfg.get("skills_max", 3)),
            max_chars=int(cfg.get("skills_max_chars", 2500)),
        )
        skills_block = str(skill_scan.get("skills_block") or skills_block)
    skill_receipt = skills_mod.build_skill_receipt(
        skill_scan,
        model_content=bool(skill_scan.get("rendered"))
        and cfg.get("skills_enabled", True),
    )
    trace.log("skills", dict(skill_receipt))
    trace.log(
        "skill_model_content",
        {
            **skill_receipt,
            "model_step": "plan",
        },
    )

    # Coordinated-change detection result (Improvement Round 2): None on
    # resume (the persisted plan carries its own groups via state.json
    # hydration), the detection dict on a fresh plan.
    coord_detection: Optional[Dict[str, Any]] = None

    if resuming:
        # Reuse the ORIGINAL plan (saved pre-crash in plan.json): a fresh
        # planner call could decompose differently and orphan the
        # completed-step descriptions state.json already recorded.
        plan = resumed_plan
        trace.log("plan_reused", {"steps": plan, "prior_attempts": resumed_attempts})
        # The persisted plan carries its own change_groups (hydrated
        # from state.json into TaskState on resume); nothing to detect.
    else:
        # -- Coordinated-change detection (Improvement Round 2, Task A) --
        # Before the planner runs, walk the structural graph from the
        # files the issue's fix will start in (retrieval's ranked files)
        # to their call-site/importer fan-out. When the issue/plan text
        # signals a signature/rename shape AND dependents exist, this IS
        # a coordinated multi-file change: the planner is told the full
        # group up front (change_group schema + fan-out section) so the
        # plan covers every dependent file in one atomic unit. Without
        # the graph or with no dependents, nothing changes.
        coordination_block = ""
        if cfg.get("coordination_detect", True):
            coord_detection = coordination_mod.detect_coordinated_change(
                repo_path=task.repo_path,
                issue_text=task.issue_text,
                changed_files=ctx["files"],
                index_root=log_root / "_code-graph",
                protected_patterns=protected,
            )
            trace.log(
                "coordination",
                {
                    "detected": coord_detection.get("detected"),
                    "kind": coord_detection.get("kind"),
                    "reason": coord_detection.get("reason"),
                    "changed_files": coord_detection.get("changed_files"),
                    "dependent_files": coord_detection.get("dependent_files"),
                    "group_files": coord_detection.get("group_files"),
                },
            )
            coordination_block = coordination_mod.format_coordination_block(
                coord_detection
            )
            if coord_detection.get("detected"):
                state.record_decision(
                    "coordinated multi-file change detected: "
                    + coord_detection.get("reason", "")
                )
        planner_messages = prompts.render_planner_prompt(
            task.issue_text,
            _context_block(
                task.repo_path,
                ctx["files"],
                min(int(cfg["context_lines_cap"]), context_budget["max_lines"]),
                min(int(cfg["context_files_cap"]), context_budget["max_files"]),
            ),
            "\n".join(f"- {p}" for p in protected) if protected else "(none)",
            strategy=ctx.get("strategy", "grep"),
            memory_block=memory_block,
            coordination_block=coordination_block,
            skills_block=skills_block,
        )
        try:
            raw_plan = model.call(planner_messages, step="plan")
            plan = _parse_plan_json(raw_plan)
            if plan is None:
                trace.log("plan_parse_error", {"raw": raw_plan[:2000]})
                retry_messages = [
                    *planner_messages,
                    {"role": "assistant", "content": raw_plan},
                    {
                        "role": "user",
                        "content": "That was not valid JSON in the required schema. "
                        "Output ONLY the JSON object now.",
                    },
                ]
                raw_plan = model.call(retry_messages, step="plan-retry")
                plan = _parse_plan_json(raw_plan)
        except Exception as exc:
            trace.log(
                "task_end", {"status": "error", "reason": f"planner failed: {exc}"}
            )
            return _result(task, "error", 0, None, None, model, trace)

        if plan is None:
            trace.log(
                "task_end",
                {"status": "error", "reason": "unparseable plan after retry"},
            )
            return _result(task, "error", 0, None, None, model, trace)
        # attempt 1 is in flight the moment the loop starts; a crash in
        # the planning->attempt window resumes into attempt 1.
        state.save_plan_steps(plan, attempts=1, cost_usd=model.total_cost_usd)

    state.set_plan([f"{st['id']}. {st['description']}" for st in plan])
    trace.log("plan", {"plan": plan, "resumed": resuming})

    # -- Change-group bookkeeping (Improvement Round 2, Task B) --------
    # Planner steps sharing a `change_group` name declare an atomic
    # multi-file unit. Union the steps' files_hints per group name and
    # record the groups in state.json (additive key — resume hydrates
    # it; the attempt-loop gate + rollback consume them). Only
    # plan-DECLARED groups are ENFORCED — the planner explicitly
    # committing "these files change together" is the contract; the
    # structural detection stays ADVISORY (it shaped the planner prompt
    # so the declaration is informed, but a detected group the plan
    # declined to declare is never force-enforced — that would gate
    # tasks on files the planner judged unrelated, e.g. a test file or
    # __init__ that merely imports the changed module). A RESUMED run
    # keeps its persisted plan and the groups state.json already holds.
    plan_groups = _plan_change_groups(plan)
    if plan_groups:
        state.set_change_groups(plan_groups)
        trace.log("change_groups", {"groups": plan_groups})

    # -- 4-6. attempt loop ----------------------------------------------
    # Resume: continue the IN-FLIGHT attempt (a crash is an interruption,
    # not a verification failure — it must not consume a harness retry).
    # The loop's `attempts += 1` brings the counter back to the in-flight
    # number, and the first-iteration rollback exemption keeps the
    # pre-crash work in work/. Cost is seeded from plan.json so the budget
    # cap stays honest across the relaunch.
    max_retries = int(cfg["max_retries"])
    if resuming:
        attempts = min(resumed_attempts - 1, max_retries - 1)
        if resumed_cost > 0:
            model.total_cost_usd = resumed_cost
    else:
        attempts = 0
    if resuming:
        trace.log(
            "attempt_resume", {"attempt": attempts + 1, "prior_cost_usd": resumed_cost}
        )
    last_verify: Optional[VerificationResult] = None
    last_feedback = ""
    ran_steps: List[str] = []  # "N. desc" of steps EXECUTED this attempt
    # Steering round: set by the re-plan path so the NEXT attempt keeps
    # the work already in work/ (the whole point of a steering re-plan —
    # redirect, never lose progress). Same exemption shape as a resume's
    # first iteration.
    keep_work_next = False

    def _do_steering_replan(steering_text: str) -> str:
        """Shared re-plan handler for the two consume points (step
        boundary, final gate). Returns "continue" (re-plan done or
        honestly degraded — the loop re-enters), "error" (planner crash
        — the task errors out; the caller returns), or "exhausted"
        (budget/wall-clock gone mid-replan; the caller breaks).

        Machine: editing -> repairing -> planning (existing forward
        edges; repairing is the documented "prepare the next attempt"
        state). The work/ copy is NOT restored (the new plan builds on
        what already landed) and the attempt slot is given back
        (steering is an interruption, not a verification failure).
        """
        nonlocal plan, last_feedback, attempts, keep_work_next
        machine.transition(
            "repairing", "steering re-plan: attempt dismantled for a new plan"
        )
        state.record_decision(f"steering re-plan: {steering_text[:300]}")
        work_diff = editor.unified_diff(str(paths.pristine), str(paths.work)) or ""
        steering_block = prompts.render_replan_context(
            steer.steering_context(max_chars=int(cfg.get("steering_max_chars", 4000)))
        )
        replan_messages = prompts.render_planner_prompt(
            issue_text=task.issue_text,
            context_block=(
                "## Work already in the working copy (KEEP and build on "
                "this — do not re-do it):\n"
                + _context_block(
                    str(paths.work),
                    list(
                        dict.fromkeys(
                            editor.changed_files(str(paths.pristine), str(paths.work))
                            + ctx["files"]
                        )
                    )[: int(cfg["context_files_cap"])],
                    int(cfg["context_lines_cap"]),
                    int(cfg["context_files_cap"]),
                )
                + (
                    f"\n\n## Current diff (work in progress)\n```diff\n{work_diff[:6000]}\n```"
                    if work_diff
                    else ""
                )
            ),
            constraints_block=(
                "\n".join(f"- {p}" for p in protected) if protected else "(none)"
            ),
            strategy=ctx.get("strategy", "grep"),
            steering_block=steering_block,
        )
        try:
            raw_replan = model.call(replan_messages, step="replan")
            new_plan = _parse_plan_json(raw_replan)
        except Exception as exc:
            trace.log(
                "task_end",
                {"status": "error", "reason": f"steering re-plan failed: {exc}"},
            )
            return "error"
        if new_plan is None:
            # Unparseable re-plan: keep the old plan (honest degrade —
            # the task continues as it was, steering still rides the
            # next attempts' feedback).
            trace.log("steering_replan_parse_error", {"raw": (raw_replan or "")[:2000]})
            last_feedback = steering_text
        else:
            plan = new_plan
            state.set_plan([f"{s['id']}. {s['description']}" for s in plan])
            state.save_plan_steps(
                plan, attempts=attempts, cost_usd=model.total_cost_usd
            )
            plan_groups = _plan_change_groups(plan)
            if plan_groups:
                state.set_change_groups(plan_groups)
            trace.log(
                "plan_replaced",
                {"steps": plan, "steering": steering_text[:500]},
            )
            last_feedback = (
                steering_text + " — the plan has been REPLACED; follow the new plan."
            )
        machine.transition("planning", "steering re-plan complete; new plan set")
        # Steering is an interruption, not a verification failure: give
        # the attempt slot back (the resume contract's exact discipline
        # for crashes) and keep the work-in-progress.
        attempts -= 1
        keep_work_next = True
        if over_budget() or over_time():
            return "exhausted"
        return "continue"

    def _steering_abort(at: str, note: str = "aborted by user steering") -> TaskResult:
        """The clean, resumable steering stop (both consume points).

        Task B/C contract: the state machine lands in `failed` (an
        honest terminal phase for the live-status surface — without this
        the on-disk phase would stay 'editing' forever for a task that
        is not running), while the resume artifacts (work/, state.json,
        plan.json, the steering journal) all survive — the loop's
        attempt-slot discipline is not consumed, exactly like the
        KeyboardInterrupt path. `at` is the checkpoint slug for the
        trace event.
        """
        try:
            machine.transition("failed", f"aborted by user steering at {at}")
        except Exception:
            pass  # from a state with no failed edge (shouldn't happen;
            # every non-terminal phase allows failed) — the stop itself
            # must never be blocked by bookkeeping
        return _result(
            task,
            "failed",
            attempts,
            None,
            last_verify,
            model,
            trace,
            note=note,
        )

    def _steering_gate_check(where: str) -> Optional[str]:
        """The steering decision at the final gate (Task C) and before
        success minting (the re-check). Returns None when no steering
        is pending (the caller proceeds to verify/mint); otherwise the
        action the loop must take:

          "abort"   — the caller returns _steering_abort(...) immediately
          "replan"  — the re-plan was already run here; the caller
                      continues the attempt loop (or breaks on
                      "exhausted"/returns on "error" via _do_steering_replan)
          "deferred"— guide-only steering: consumed, the attempt is
                      poisoned for a re-verify AFTER incorporation;
                      the caller continues the loop
          "error"   / "exhausted" — from _do_steering_replan; the
                      caller returns/breaks respectively

        THE GUARANTEE: pending steering at a success point never lets
        success be minted — the fix is re-verified after the steering
        is incorporated (guide) or the plan is replaced (replan).
        """
        nonlocal last_feedback
        if steer is None or not steer.pending():
            return None
        if steer.has_intent("abort"):
            _taken = steer.take(f"abort-{where}")
            machine.record_event("steering_abort", {"seqs": [e.seq for e in _taken]})
            trace.log(
                "steering_abort",
                {
                    "attempt": attempts,
                    "at": where,
                    "texts": [e.text for e in _taken],
                },
            )
            state.record_decision(
                f"aborted by user steering ({where}; resumable via resume)"
            )
            trace.log("task_end", {"status": "failed", "aborted": True})
            return "abort"
        if steer.has_intent("replan"):
            _taken = steer.take(f"replan-{where}")
            machine.record_event("steering_replan", {"seqs": [e.seq for e in _taken]})
            trace.log(
                "steering_replan",
                {
                    "attempt": attempts,
                    "at": where,
                    "texts": [e.text for e in _taken],
                },
            )
            steering_text = "User steering replaced the plan: " + " | ".join(
                e.text for e in _taken
            )
            return _do_steering_replan(steering_text)
        _taken = steer.take(where)
        machine.record_event("steering", {"seqs": [e.seq for e in _taken], "at": where})
        trace.log(
            "steering",
            {
                "attempt": attempts,
                "at": where,
                "seqs": [e.seq for e in _taken],
                "intents": [e.intent for e in _taken],
                "texts": [e.text for e in _taken],
            },
        )
        state.record_decision(
            "success minting deferred: user steering arrived before "
            f"the {where}; the fix is re-verified after steering"
        )
        last_feedback = (
            "The verifier passed, but the user steered this task "
            "mid-run and the instruction has not been incorporated "
            "into the work yet:\n"
            + "\n".join(f"- {e.text}" for e in _taken)
            + "\nIncorporate it and verify again."
        )
        _attempt_rejected(
            trace,
            model,
            attempts,
            elapsed,
            paths,
            state,
            cfg,
            coord_rollback_files=(),
        )
        if over_budget() or over_time():
            return "exhausted"
        return "deferred"

    while attempts < max_retries:
        if over_budget():
            trace.log("stop", {"reason": "budget cap", "usage": model.snapshot_usage()})
            return _result(
                task,
                "failed",
                attempts,
                None,
                last_verify,
                model,
                trace,
                note="budget cap exceeded",
            )
        if over_time():
            trace.log("stop", {"reason": "wall-clock cap"})
            return _result(
                task,
                "timeout",
                attempts,
                None,
                last_verify,
                model,
                trace,
                note="wall-clock limit",
            )

        attempts += 1
        trace.log("attempt_start", {"attempt": attempts})
        _rollback_exempt = (resuming and attempts == resumed_attempts) or keep_work_next
        if attempts > 1 and not _rollback_exempt:
            # New attempt = clean slate: restore the working copy AND reset
            # completed steps (the rolled-back work no longer exists).
            # The FIRST iteration of a resumed run is exempt: it CONTINUES
            # the pre-crash attempt, whose partial work lives in work/.
            # A steering re-plan iteration is exempt the same way: the new
            # plan BUILDS ON work/ (the user redirected, not undid).
            editor.restore_dir(str(paths.pristine), str(paths.work))
            state.reset_completed()
        keep_work_next = False
        state.save_plan_steps(plan, attempts=attempts, cost_usd=model.total_cost_usd)
        # Editing starts with the attempt's first step session.
        machine.transition("editing", f"attempt {attempts}: step sessions begin")

        attempt_error: Optional[str] = None  # hard error ends the task
        intra_feedback = ""  # step-to-step feedback within this attempt
        ran_steps = []  # steps executed this attempt (reset per attempt)
        steering_replan: Optional[str] = None  # replan feedback when set
        for st in plan:
            step_id = int(st["id"])
            step_desc = f"{step_id}. {st['description']}"
            if step_desc in state.completed_steps:
                # Resume: this step already completed pre-crash and its
                # edits survive in work/ — re-running it would be waste
                # (or actively harmful).
                intra_feedback = (
                    f"Step '{step_desc}' was completed in the previous "
                    f"(interrupted) session; its edits are already in place."
                )
                trace.log(
                    "step_skipped_resume", {"attempt": attempts, "step": step_desc}
                )
                continue
            completed_descs = [
                f"{s['id']}. {s['description']}"
                for s in plan
                if int(s["id"]) < step_id
                and f"{s['id']}. {s['description']}" in state.completed_steps
            ]
            raw_retrieval_hints = cfg.get("retrieval_hint_files") or []
            if isinstance(raw_retrieval_hints, str):
                raw_retrieval_hints = [raw_retrieval_hints]
            step_candidates = list(
                dict.fromkeys(
                    [str(v) for v in raw_retrieval_hints]
                    + [str(v) for v in (st.get("files_hint") or [])]
                    + ctx["files"]
                )
            )
            step_budget = retrieval.size_context_budget(
                task.issue_text,
                str(paths.work),
                files_touched=list(state.files_touched),
                base_files=int(cfg["context_files_cap"]),
                base_lines=int(cfg["context_lines_cap"]),
            )
            step_files = retrieval.rerank_files(
                str(paths.work),
                step_candidates,
                st,
                limit=step_budget["max_files"],
            )

            ok, note, v = run_step(
                task=task,
                step=st,
                plan=plan,
                cfg=cfg,
                paths=paths,
                state=state,
                trace=trace,
                model=model,
                step_files=step_files,
                completed=completed_descs,
                feedback=last_feedback or intra_feedback,
                verify=verify,
                deadline=deadline,
                steer=steer,
                machine=machine,
                policy=recovery_policy,
                model_recovery=model_recovery,
            )
            ran_steps.append(step_desc)
            trace.log(
                "step_end",
                {
                    "attempt": attempts,
                    "step_id": step_id,
                    "description": st["description"],
                    "ok": ok,
                    "note": note[:1000],
                },
            )
            if v is not None:
                last_verify = v

            # -- STEERING: step-boundary checkpoint (steering round) ----
            # The step session just ended: a clean point to act on a
            # strong steering intent. ABORT: clean stop, resumable (the
            # resume artifacts — work/, state.json, plan.json — are all
            # intact; only the in-flight attempt ends). REPLAN: dismantle
            # the remaining plan; the re-plan (below, at the attempt
            # gate) sees all steering + the current work diff.
            if steer is not None:
                if steer.has_intent("abort"):
                    _taken = steer.take("abort-step-boundary")
                    machine.record_event(
                        "steering_abort", {"seqs": [e.seq for e in _taken]}
                    )
                    trace.log(
                        "steering_abort",
                        {
                            "attempt": attempts,
                            "at": "step-boundary",
                            "texts": [e.text for e in _taken],
                        },
                    )
                    state.record_decision(
                        "aborted by user steering at a step boundary "
                        "(resumable via resume)"
                    )
                    trace.log("task_end", {"status": "failed", "aborted": True})
                    return _steering_abort(
                        "a step boundary",
                        note="aborted by user steering",
                    )
                if steer.has_intent("replan"):
                    _taken = steer.take("replan-step-boundary")
                    machine.record_event(
                        "steering_replan", {"seqs": [e.seq for e in _taken]}
                    )
                    trace.log(
                        "steering_replan",
                        {
                            "attempt": attempts,
                            "at": "step-boundary",
                            "texts": [e.text for e in _taken],
                        },
                    )
                    steering_replan = "User steering replaced the plan: " + " | ".join(
                        e.text for e in _taken
                    )
                    break  # -> attempt gate -> re-plan path

            if not ok and note.startswith("FATAL:"):
                attempt_error = note[len("FATAL:") :].strip() or "fatal step error"
                break

            if ok:
                state.complete_step(f"{step_id}. {st['description']}")
                # Checkpoint attempt/cost bookkeeping at the same boundary
                # state.json is rewritten — a crash mid-attempt must not
                # lose the spend since attempt start.
                state.save_plan_steps(
                    plan, attempts=attempts, cost_usd=model.total_cost_usd
                )
                # Early verifier exit: target + regression already confirmed
                # mid-plan — no need to execute the remaining steps.
                if (
                    v is not None
                    and v.target_test_passed
                    and v.regression_passed
                    and not v.flaky
                ):
                    state.record_decision("target test passed before all planned steps")
                    break
                # Step done but the fix isn't fully verified yet — pass its
                # state to the next step's session.
                intra_feedback = (
                    f"Previous step ('{st['description']}') completed; its "
                    f"checkpoint status: {note}"
                )
                continue  # step session done; keep executing the plan

            # Step failed (validation / turns exhausted / still-failing
            # checkpoint): end the attempt now — later steps depend on it.
            if v is not None and v.target_test_passed and not v.regression_passed:
                last_feedback = _regression_feedback(v)
            else:
                last_feedback = note or _target_feedback(v, st)
            break

        if attempt_error is not None:
            trace.log("task_end", {"status": "error", "reason": attempt_error})
            return _result(
                task,
                "error",
                attempts,
                None,
                last_verify,
                model,
                trace,
                note=attempt_error,
            )

        # -- STEERING: the re-plan path (steering round, Task B) --------
        # A replan-intent steering event was consumed at a step boundary
        # (or the final gate): the remaining plan is dismantled and
        # re-planned with ALL steering visible plus the CURRENT work
        # diff — progress already in work/ is NEVER thrown away.
        if steering_replan is not None:
            _replan_out = _do_steering_replan(steering_replan)
            if _replan_out == "error":
                return _result(task, "error", attempts, None, last_verify, model, trace)
            if _replan_out == "exhausted":
                break
            continue

        # Attempt finished all steps (or early-verified) — the gate that
        # can end the whole task with success:

        # Round 6 adversarial fix: re-validate edits before the final
        # verify can mint a success. A step that violated a protected
        # path (or forged a VCS dir) may have ended the STEP loop with
        # ok=False, but its edits are still sitting in work/ — if the
        # violation happens to leave the suite green (e.g. a defused
        # test), the final verify alone would crown it. The violation
        # must poison the attempt: validation is a policy gate, not a
        # hypothesis the verifier gets to re-litigate.
        ok_edits, edit_msg, _ = editor.check_edits(
            str(paths.pristine), str(paths.work), protected
        )
        if not ok_edits:
            trace.log("final_edit_validation_failed", {"reason": edit_msg})
            machine.transition("repairing", "attempt rejected by edit validation")
            last_feedback = (
                f"Attempt rejected by edit validation: {edit_msg}. "
                "The working copy violates the edit policy (protected path "
                "or forged VCS state) — restore or fix without touching "
                "protected files."
            )
            _attempt_rejected(
                trace,
                model,
                attempts,
                elapsed,
                paths,
                state,
                cfg,
                coord_rollback_files=(),
            )
            if over_budget() or over_time():
                break
            continue

        # -- Coordination gate (Improvement Round 2, Task B) ------------
        # A coordinated multi-file change is ATOMIC: if a declared group
        # (plan-declared change_group, or the structural fan-out group
        # when detection fired) did not get ALL its members changed,
        # this is a PARTIAL coordinated change — a signature update
        # that missed call sites, a rename that left callers behind.
        # Landing it partially is exactly the half-updated state the
        # gate exists to prevent, so the attempt is poisoned (missing
        # members named in the feedback) and the GROUP rolls back
        # together (coordination_rollback config), never just one file.
        # The verifier is NOT asked to re-litigate group completeness —
        # it only sees test outcomes, which a suite without call-site
        # coverage would happily pass.
        if cfg.get("coordination_gate", True) and state.change_groups:
            changed_now = editor.changed_files(str(paths.pristine), str(paths.work))
            changed_set = {c.replace("\\", "/") for c in changed_now}
            partial: List[str] = []
            group_of_partial: Dict[str, List[str]] = {}
            for gname, gfiles in state.change_groups.items():
                if len(gfiles) < int(cfg.get("coordination_min_files", 2)):
                    continue  # not a real group — single-file noise
                missing = coordination_mod.missing_group_members(gfiles, changed_now)
                touched = [f for f in gfiles if f in changed_set]
                if missing and touched:
                    # partially-edited group: some members changed, others
                    # required by the same atomic unit did not
                    partial.append(gname)
                    group_of_partial[gname] = missing
            if partial:
                fb_parts = []
                for gname in partial:
                    fb_parts.append(
                        coordination_mod.format_missing_group_feedback(
                            state.change_groups[gname], group_of_partial[gname]
                        )
                    )
                last_feedback = "\n\n".join(fb_parts)
                machine.transition(
                    "repairing", "coordination gate rejected a partial group"
                )
                trace.log(
                    "coordination_gate_rejected",
                    {
                        "attempt": attempts,
                        "groups": {g: group_of_partial[g] for g in partial},
                    },
                )
                rollback_files: List[str] = []
                for gname in partial:
                    rollback_files.extend(state.change_groups[gname])
                _attempt_rejected(
                    trace,
                    model,
                    attempts,
                    elapsed,
                    paths,
                    state,
                    cfg,
                    coord_rollback_files=tuple(rollback_files),
                )
                if over_budget() or over_time():
                    break
                continue

        # Round 8 lint gate (Task C): fast host-side static analysis of
        # the CHANGED files BEFORE paying for a sandboxed verify cycle.
        # A syntax error / undefined module-level name is KNOWABLE in
        # milliseconds — the verify run would only rediscover it after
        # spinning up containers and a pytest collection. Same policy
        # semantics as the edit-validation gate above: lint failure
        # poisons the attempt (retry with the classified findings as
        # feedback); lint NEVER gates success (verifier-gated completion
        # stays absolute — spec item 17).
        if cfg.get("lint_gate", True):
            _changed = editor.changed_files(str(paths.pristine), str(paths.work))
            _findings = lint_mod.lint_changed(
                str(paths.work),
                _changed,
                check_names=bool(cfg.get("lint_names", True)),
            )
            if _findings:
                trace.log(
                    "lint_failed",
                    {
                        "attempt": attempts,
                        "findings": [
                            {
                                "file": f.file,
                                "line": f.line,
                                "kind": f.kind,
                                "message": f.message,
                            }
                            for f in _findings
                        ],
                    },
                )
                machine.transition("repairing", "lint gate short-circuited the attempt")
                last_feedback = lint_mod.render_findings(_findings)
                _attempt_rejected(
                    trace,
                    model,
                    attempts,
                    elapsed,
                    paths,
                    state,
                    cfg,
                    coord_rollback_files=(),
                )
                if over_budget() or over_time():
                    break
                continue

        if over_time():
            trace.log("stop", {"reason": "wall-clock cap before final verify"})
            break

        # -- STEERING: pending steering blocks success minting --------
        # (steering round, Task C — THE guarantee.) The steps may have
        # produced a verifiable fix, but if the user redirected the task
        # mid-run and that instruction has not been incorporated, the
        # attempt does NOT reflect the user's latest intent: minting
        # success now would shortcut the verifier gate (success before
        # steering == an unverified direction claim). _steering_gate_check
        # consumes the steering and routes: abort -> clean resumable
        # stop; replan -> the shared re-plan path; guide -> the attempt
        # is poisoned and re-verified after incorporation. Verifier-gated
        # completion is never shortcut.
        _gate = _steering_gate_check("final-gate")
        if _gate == "abort":
            return _steering_abort("the final gate")
        if _gate in ("error",):
            return _result(task, "error", attempts, None, last_verify, model, trace)
        if _gate == "exhausted":
            break
        if _gate is not None:  # "deferred" or the replan "continue"
            continue

        machine.transition("testing", f"attempt {attempts}: final verifier gate")
        try:
            final_v = verify(
                str(paths.work),
                cfg.get("target_test"),
                rerun_for_flake_check=int(cfg.get("baseline_reruns", 1)),
                test_command=cfg.get("test_command"),
                verify_timeout_s=int(cfg["verify_timeout_s"]),
                # VEX-PF-10: the FINAL GATE is where flake detection has to be
                # able to fire — this is the run that mints the completion
                # claim. `rung_config=cfg` is what lets execution.verify resolve
                # `post_fix_reruns` through execution.flake_gate and attach the
                # three-valued verdict; with that key absent the repetition
                # count is still `baseline_reruns` and the boolean is identical.
                **_verify_rung_kwargs(
                    verify, cfg, run_dir=str(paths.log_dir), phase="postfix"
                ),
            )
            if not _verification_evidence_ok(final_v):
                raise ValueError("verifier returned incomplete evidence")
        except Exception as exc:
            trace.log(
                "task_end",
                {"status": "error", "reason": f"final verify crashed: {exc}"},
            )
            return _result(
                task,
                "error",
                attempts,
                None,
                last_verify,
                model,
                trace,
                note=f"final verify crashed: {exc}",
            )
        # baseline_passed semantics: did the target pass BEFORE any edit?
        # We only reach this point when it did NOT (else we exited earlier),
        # so the field is False — set explicitly for clarity.
        final_v = _with_baseline(final_v, baseline_passed_field=False)
        trace.log(
            "final_verify",
            {
                "attempt": attempts,
                "target_passed": final_v.target_test_passed,
                "regression_passed": final_v.regression_passed,
                "flaky": final_v.flaky,
                # VEX-PF-10: the rung(s) that produced this verdict. A reader
                # asking "which gate claimed this was verified?" answers this
                # row, not a guess from the surrounding code.
                "rung": getattr(final_v, "verification_rung", ""),
                "rungs": list(getattr(final_v, "verification_rungs", ()) or ()),
                "flake_check": getattr(final_v, "flake_check", ""),
                "repetitions": getattr(final_v, "repetitions", None),
                "observed_outcomes": list(
                    getattr(final_v, "observed_outcomes", ()) or ()
                ),
                "raw": final_v.raw_output[-3000:],
            },
        )
        last_verify = final_v

        if over_time():
            trace.log("stop", {"reason": "wall-clock cap after final verify"})
            break

        if (
            final_v.target_test_passed
            and final_v.regression_passed
            and not final_v.flaky
        ):
            diff = editor.unified_diff(str(paths.pristine), str(paths.work)) or ""
            changed = editor.changed_files(str(paths.pristine), str(paths.work))

            # Agent-written edge-case gate (Improvement Round 2): before
            # success is minted, have the model write tests probing the
            # issue's implied edges and run them through the SAME verify()
            # pipeline (baseline stage + post-fix stage with flake-rerun +
            # suite regression). A failure poisons the attempt — the fix is
            # incomplete for an issue-implied edge; a GENERATION problem
            # skips the gate (never overturn a verified fix over
            # test-writing quality). Files_touched is recorded only AFTER
            # the gate passes so a poisoned attempt's state stays honest.
            if cfg.get("agent_tests", True):
                at_ok, at_feedback = _agent_tests_gate(
                    task,
                    cfg,
                    paths,
                    trace,
                    model,
                    attempts,
                    verify,
                    diff,
                )
                if not at_ok:
                    last_feedback = at_feedback
                    _attempt_rejected(
                        trace,
                        model,
                        attempts,
                        elapsed,
                        paths,
                        state,
                        cfg,
                        coord_rollback_files=(),
                    )
                    if over_budget() or over_time():
                        break
                    continue

            # Self-critique gate (Agent Intelligence round, Task A):
            # before this verifier-passed diff is FINALIZED as the fix,
            # one extra model call reviews it against the ORIGINAL issue
            # text — "does this diff actually address what was reported,
            # not just make tests pass." This catches the failure class
            # verification alone misses (a technically-green diff that
            # dodges the complaint). A "no" verdict poisons the attempt
            # (retry with the critique's reason as feedback) — same
            # policy semantics as the final edit-validation gate above.
            # Best-effort on model errors: a crashed critique call must
            # not kill a verified fix (it degrades to approve).
            if cfg.get("self_critique", True) and (diff or changed):
                try:
                    critique_ok, critique_reason = _self_critique(
                        task, cfg, model, trace, diff, final_v, last_feedback
                    )
                except Exception as exc:
                    trace.log("self_critique_failed", {"error": str(exc)})
                    critique_ok, critique_reason = True, ""
                if not critique_ok:
                    trace.log(
                        "self_critique_reject",
                        {"attempt": attempts, "reason": critique_reason[:1000]},
                    )
                    state.record_decision(
                        "attempt rejected by self-critique (diff does not "
                        "address the reported issue)"
                    )
                    last_feedback = (
                        "The verifier passed, but a review of your diff "
                        "against the ORIGINAL issue says it does not "
                        f"address what was reported: {critique_reason} "
                        "Fix the actual reported behavior, not just the "
                        "tests."
                    )
                    _attempt_rejected(
                        trace,
                        model,
                        attempts,
                        elapsed,
                        paths,
                        state,
                        cfg,
                        coord_rollback_files=(),
                    )
                    if over_budget() or over_time():
                        break
                    continue

            # -- STEERING: pre-mint re-check (Task C, the second ------
            # consume point of _steering_gate_check). The final verify,
            # agent-tests gate and self-critique above each take real
            # time (verify runs + model calls); steering injected DURING
            # that window is pending RIGHT NOW and the checks above
            # only polled BEFORE the verify. Re-poll before minting:
            # an instruction that arrived while the verifier ran must
            # still block success (the same guarantee as the pre-verify
            # gate — the fix must be re-verified after incorporation).
            _gate = _steering_gate_check("pre-mint")
            if _gate == "abort":
                return _steering_abort("the pre-mint checkpoint")
            if _gate in ("error",):
                return _result(task, "error", attempts, None, last_verify, model, trace)
            if _gate == "exhausted":
                break
            if _gate is not None:
                continue

            for rel in changed:
                state.record_file_touched(rel)
            # The fix as a whole is verified: every plan step that RAN in
            # this attempt has had its work subsumed by the verified diff
            # (steps can end without SUBMIT — e.g. exhausting turns after
            # their commands already made the edits — and early-exit means
            # later steps never needed to run). Record them as completed so
            # state.json (the progress authority read by `harness status`/
            # dashboard/resume) agrees with the verified result instead of
            # claiming steps remain.
            state.complete_all_ran_steps(ran_steps)
            state.record_decision("fix verified by test suite (target + regression)")
            if cfg.get("approval") == "require":
                # Awaiting-approval park (state machine's documented
                # write-ahead): the worker gates AFTER run_task returns —
                # recording the phase here keeps the on-disk live view
                # correct during the whole park window.
                machine.transition(
                    "awaiting_approval",
                    "verified fix parked for human approval",
                )
                state.record_decision(
                    "verified fix parked for human approval "
                    "(worker-level gate; approve -> done)"
                )
            else:
                machine.transition("done", "fix verified (target + regression)")
            trace.log("task_end", {"status": "success", "attempt": attempts})
            # VEX-CEILING-07: recovery statistics ride the SUCCESS receipt
            # too — a run that recovered from three timeouts is exactly the
            # run whose recovery cost should be visible.
            _log_recovery_stats(trace, recovery_policy, model_recovery)
            # AFTER task_end: build_rationale keys its verdict off the
            # task_end event, and state.json is complete by here.
            _record_product_output(
                paths,
                task,
                cfg,
                trace,
                attempts,
                changed,
                diff,
                final_v,
            )
            return _result(task, "success", attempts, diff, final_v, model, trace)

        if final_v.flaky:
            last_feedback = (
                "The target test shows FLAKY behavior (different outcomes across "
                "reruns). Look for order dependence, unseeded randomness, or shared "
                "state; make the fix deterministic."
            )
        elif final_v.target_test_passed and not final_v.regression_passed:
            last_feedback = _regression_feedback(final_v)
        else:
            last_feedback = _target_feedback(final_v)

        # VEX-CEILING-07: the `verification_failed` policy — PRESERVE the
        # evidence and REPLAN against it. The evidence the policy keeps is
        # what the next attempt is actually seeded with, so the two can never
        # drift apart: `last_feedback` is taken from the action, not from a
        # separate re-derivation of the verifier output.
        _verification_action = recovery_policy.on_verification_failure(
            final_v, attempt=attempts
        )
        _policy_feedback = recovery_policy.feedback(_verification_action)
        if _policy_feedback:
            last_feedback = f"{last_feedback}\n\n{_policy_feedback}"
        trace.log(
            "recovery_action",
            {
                "attempt": attempts,
                "kind": _verification_action.kind,
                "action": _verification_action.action,
                "replan": _verification_action.replan,
                "recovery": _verification_action.to_dict(),
            },
        )

        # Final verify failed: repair the attempt (work rolled back,
        # feedback seeded for the next one).
        machine.transition("repairing", f"final verify failed on attempt {attempts}")
        # A FAILED verified attempt with coordinated groups: the groups'
        # files roll back together (atomic unit), per the same policy as
        # the coordination gate — the next attempt must not inherit a
        # half-updated coordinated change it didn't plan.
        _rollback = _touched_group_files(
            state.change_groups,
            editor.changed_files(str(paths.pristine), str(paths.work)),
        )
        _attempt_rejected(
            trace,
            model,
            attempts,
            elapsed,
            paths,
            state,
            cfg,
            coord_rollback_files=tuple(_rollback),
        )
        if over_budget() or over_time():
            break

    status = "timeout" if over_time() else "failed"
    machine.transition("failed", f"retries/budget/wall-clock exhausted ({status})")
    diff = editor.unified_diff(str(paths.pristine), str(paths.work)) or ""
    trace.log("task_end", {"status": status, "attempts": attempts})
    _log_recovery_stats(trace, recovery_policy, model_recovery)
    _record_rationale_only(paths, task, cfg, trace)
    return _result(task, status, attempts, diff or None, last_verify, model, trace)


def run_task(task: Task, log_root: Optional[Path] = None) -> TaskResult:
    """Run verified fixing through the authoritative agent-kernel strategy.

    Assumes ``task`` satisfies the stable shared ``Task`` contract. The
    historical implementation remains available as ``_run_task_legacy`` and
    is invoked only by ``VerifiedFixStrategy``; callers retain the exact
    ``TaskResult`` and ``logs/{task_id}/state.json`` compatibility contract.
    """
    from harness.agent_kernel import AgentKernel, CompletionStatus, RunSpec

    root = (
        Path(log_root or Path(get_config(task.config)["work_subdir"]))
        .expanduser()
        .resolve()
    )
    spec = RunSpec(
        session_id=f"session-{task.task_id}",
        run_id=task.task_id,
        request=task.issue_text,
        repository_identity=task.repo_path,
        strategy="verified_fix",
        metadata={"config": dict(task.config)},
    )
    kernel = AgentKernel(repo_path=task.repo_path, log_root=root, config=task.config)
    result = kernel.run(
        spec, strategy="verified_fix", resume=bool(task.config.get("resume"))
    )
    legacy = kernel.last_legacy_result
    if isinstance(legacy, TaskResult):
        return legacy
    evidence = next(
        (
            dict(item)
            for item in result.verification_evidence
            if item.get("kind") == "verification"
        ),
        {},
    )
    verification = None
    if evidence:
        verification = VerificationResult(
            target_test_passed=bool(
                evidence.get("target_passed", evidence.get("target_test_passed", False))
            ),
            baseline_passed=bool(evidence.get("baseline_passed", False)),
            regression_passed=bool(evidence.get("regression_passed", False)),
            flaky=bool(evidence.get("flaky", False)),
            raw_output=str(evidence.get("raw", "")),
        )
    status = {
        CompletionStatus.COMPLETED_VERIFIED.value: "success",
        CompletionStatus.TIMEOUT.value: "timeout",
    }.get(result.status, "failed")
    return TaskResult(
        task_id=task.task_id,
        status=status,
        attempts=result.attempts,
        diff=result.diff or None,
        verification=verification,
        cost_usd=result.cost,
        model_calls=list(result.model_calls),
        log_path=result.trace_path
        or str((root / _safe_log_segment(task.task_id) / "trace.jsonl").resolve()),
    )


def _fresh_paths(log_root: Path, task_id: str, resuming: bool = False) -> TaskPaths:
    """Create (or reuse, or archive-then-create) logs/{task_id}/.

    A stale directory from a previous run of the same task_id would
    corrupt state.json reads and diffs, so on a FRESH start it is archived
    as {task_id}.old-HHMMSS rather than deleted (history preserved,
    current run starts clean).

    When `resuming` is True (a relaunch continuing an interrupted run —
    see the resume contract in the module docstring), the directory is
    KEPT: state.json/trace.jsonl/pristine/work are the run's surviving
    progress. Assumes the caller only sets resuming when a prior
    state.json with completed steps exists.
    """
    paths = TaskPaths(log_root, task_id)
    if paths.log_dir.exists():
        if resuming:
            return paths  # never archive the very state we resume from
        arch = paths.log_dir.with_name(
            f"{task_id}.old-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.rmtree(arch, ignore_errors=True)
        paths.log_dir.rename(arch)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _attempt_rejected(
    trace: TraceLogger,
    model: ModelClient,
    attempts: int,
    elapsed: Callable[[], float],
    paths: TaskPaths,
    state: TaskState,
    cfg: Dict[str, Any],
    coord_rollback_files: Tuple[str, ...] = (),
) -> None:
    """Shared tail for a REJECTED attempt (edit-validation failure,
    coordination-gate partial group, lint failure): log attempt_end and
    roll the coordinated groups back as atomic units.

    Coordination rollback policy (Improvement Round 2, Task B —
    config coordination_rollback):
      "group" (default) — restore ONLY the rejected groups' files via
        editor.restore_group; other steps' work in work/ survives (the
        next attempt keeps unrelated progress);
      "all"             — full editor.restore_dir (the pre-Round-2
        behavior — every step's work is discarded);
      "none"            — leave work/ untouched (the verifier-gate
        feedback loop relies on restore; "none" is for debugging).
    Group members restored together — the whole coordinated change
    reverts, never one file. Logs a coordination_rollback trace event
    with the restored file set (observability: dashboards can show
    WHAT rolled back and why). Assumes coord_rollback_files is empty
    for non-coordination rejections (no group rollback applies).
    """
    mode = str(cfg.get("coordination_rollback", "group") or "group").lower()
    if coord_rollback_files and mode == "group":
        restored = editor.restore_group(
            str(paths.pristine), str(paths.work), list(coord_rollback_files)
        )
        editor.group_orphans(str(paths.work), list(coord_rollback_files))
        # The rolled-back files no longer count as touched work.
        state.clear_files_touched(restored)
        trace.log(
            "coordination_rollback",
            {
                "attempt": attempts,
                "mode": mode,
                "groups": [
                    g
                    for g, fs in (state.change_groups or {}).items()
                    if any(f in coord_rollback_files for f in fs)
                ],
                "restored": restored,
            },
        )
        state.record_decision(
            f"attempt {attempts}: coordinated change rolled back as one "
            f"unit ({len(restored)} files)"
        )
    elif coord_rollback_files and mode == "all":
        editor.restore_dir(str(paths.pristine), str(paths.work))
        trace.log(
            "coordination_rollback",
            {
                "attempt": attempts,
                "mode": mode,
                "files": list(coord_rollback_files),
                "restored": "all",
            },
        )
    # mode "none" (or no group files): no rollback — the next attempt
    # starts from whatever is in work/.
    trace.log(
        "attempt_end",
        {
            "attempt": attempts,
            "usage": model.snapshot_usage(),
            "elapsed_s": round(elapsed(), 1),
        },
    )


# ----------------------------------------------------------------------------
# Agent-written edge-case tests (Improvement Round 2, Tasks A+B)
# ----------------------------------------------------------------------------


def _self_critique(
    task: Task,
    cfg: Dict[str, Any],
    model: ModelClient,
    trace: TraceLogger,
    diff: str,
    final_v: VerificationResult,
    last_feedback: str,
) -> Tuple[bool, str]:
    """Review a VERIFIER-PASSED diff against the original issue (Task A).

    Makes one model call: the self-critique prompt (harness/prompts.
    render_self_critique_prompt) carries the original issue, the full
    diff (capped), the verification summary, and — when Boundary 7
    structured feedback is available — what the verifier reported on the
    run's FAILING attempts (the complaint the diff must not dodge;
    INTERFACES.md Boundary 7's recommended consumption). Returns
    (addresses_issue, reason); reason is "" on approval. Assumes the
    caller already confirmed final_v passed and only calls this when
    cfg["self_critique"] is truthy and there is a diff to review. A
    reply without a parseable verdict counts as APPROVED (critique is a
    quality gate, never a task-killer on its own parse failure).
    """
    capped = diff[: int(cfg.get("self_critique_max_chars", 8000))]
    failing_feedback = list(getattr(final_v, "structured_feedback", None) or [])
    # Historical results (pre-Boundary-7 serialization) replay identically
    # through feedback_from_result; on any gap the raw tail still rides
    # last_feedback into the prompt via verification_summary.
    if not failing_feedback and last_feedback:
        try:
            from execution.feedback import feedback_from_result

            failing_feedback = [
                f.to_dict()
                for f in feedback_from_result(final_v)
                if f.failure_type != "unparseable"
            ]
        except ImportError:
            pass
    messages = prompts.render_self_critique_prompt(
        issue_text=task.issue_text,
        diff=capped,
        verification_summary=(
            f"Final verify: target passed, full suite green, not flaky "
            f"({len(final_v.raw_output or '')} chars of output)."
            + (
                f" Prior failing-attempt feedback: {last_feedback[-800:]}"
                if last_feedback
                else ""
            )
        ),
        feedback_objects=failing_feedback or None,
    )
    raw = model.call(messages, step="self-critique")
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("addresses_issue"), bool):
            trace.log(
                "self_critique",
                {
                    "verdict": obj["addresses_issue"],
                    "reason": str(obj.get("reason", ""))[:1000],
                },
            )
            return bool(obj["addresses_issue"]), str(obj.get("reason", ""))
    trace.log(
        "self_critique",
        {
            "verdict": True,
            "reason": "unparseable reply; approved by default",
            "raw": (raw or "")[:500],
        },
    )
    return True, ""


def _agent_tests_feedback(v: VerificationResult, node: str) -> str:
    """Feedback rendered from a FAILED agent-written test run.

    Prefers the structured-feedback convention (Boundary 7:
    v.structured_feedback objects, execution.feedback.format_objects)
    when present, else the raw tail — same graceful adoption as
    _target_feedback. Assumes v is the failing post-fix verify() result
    for the agent-test target `node`.
    """
    objs = getattr(v, "structured_feedback", None)
    if objs:
        try:
            from execution.feedback import format_objects

            return format_objects(list(objs))
        except Exception:
            pass
    return (v.raw_output or "")[-1500:] or (
        f"agent test {node} failed post-fix (see trace)"
    )


def _agent_tests_gate(
    task: Task,
    cfg: Dict[str, Any],
    paths: TaskPaths,
    trace: TraceLogger,
    model: ModelClient,
    attempts: int,
    verify: Callable[..., VerificationResult],
    diff: str,
) -> Tuple[bool, str]:
    """The pre-success edge-case gate — run AFTER final verify passes,
    BEFORE success is minted.

    Returns (passed, feedback):
    - (True, ""): the fix survived every agent-written edge-case test
      (or the gate legitimately skipped — see below); caller mints success.
    - (False, msg): at least one generated test that FAILED on the
      pre-fix tree now fails on the fixed tree — the fix is incomplete
      for an issue-implied edge. msg is the next attempt's feedback.
      The caller poisons the attempt (rollback + retry).

    SKIP (never overturn a verified fix over test-writing quality):
    model call crash, unparseable reply, zero tests surviving
    sanitize, zero tests surviving the baseline filter, or a copy/
    verify crash mid-gate — every skip path returns (True, "") with a
    trace event carrying the exact reason; nothing is silent.

    Rigor parity (Task B): both stages go through the SAME verify()
    everything else uses — baseline with rerun_for_flake_check=0
    (exactly the task-baseline convention) and post-fix with the final
    gate's rerun count and test_command. The agent tests are the TARGET
    (node id = the generated file), so flake-rerun + full-suite
    regression apply to them identically.

    Assumes: final verify already passed on work/ (the candidate fix);
    cfg carries the agent_tests* keys; paths.pristine is the untouched
    snapshot; the gate NEVER writes into work/ (transient trees live
    under logs/{task_id}/agent_tests/), so a crash mid-gate cannot
    corrupt the candidate.
    """
    gate_dir = paths.log_dir / "agent_tests"
    attempt_dir = gate_dir / f"attempt_{attempts}"
    saved_dir = gate_dir / "saved"
    rel_dir = str(cfg.get("agent_tests_dir", "tests/_agent_generated"))
    max_files = int(cfg.get("agent_tests_max", 3))
    max_chars = int(cfg.get("agent_tests_max_chars", 12000))

    def _skip(reason: str) -> Tuple[bool, str]:
        trace.log("agent_tests_skip", {"attempt": attempts, "reason": reason})
        return True, ""

    # -- 1. GENERATE ----------------------------------------------------
    try:
        msgs = prompts.render_agent_tests_prompt(
            issue_text=task.issue_text,
            diff=diff,
            tests_tree=agent_tests_mod.list_test_files(str(paths.pristine)),
            max_tests=max_files,
        )
        raw = model.call(msgs, step=f"agent-tests-{attempts}")
    except Exception as exc:
        return _skip(f"generation failed: {exc}")

    parsed = agent_tests_mod.parse_agent_tests(raw)
    if parsed is None:
        trace.log("agent_tests_parse_error", {"raw": (raw or "")[:2000]})
        return _skip("unparseable generation reply")

    tests, drop_reasons = agent_tests_mod.sanitize_agent_tests(
        parsed, max_files=max_files, max_chars=max_chars
    )
    if drop_reasons:
        trace.log("agent_tests_dropped", {"attempt": attempts, "reasons": drop_reasons})
    if not tests:
        return _skip("no tests survived sanitize")
    trace.log(
        "agent_tests_generated",
        {
            "attempt": attempts,
            "files": [t["filename"] for t in tests],
        },
    )

    # -- 2. BASELINE STAGE (same verify(), pristine tree) ----------------
    # A generated test that PASSES pre-fix probes nothing the final gate
    # didn't already cover; it is dropped (evidence kept) and only the
    # still-failing set proceeds. This mirrors how the task itself treats
    # a pre-passing target (mislabeled probe -> not a gate).
    base_tree = agent_tests_mod.copy_with_tests(
        str(paths.pristine), str(attempt_dir / "baseline"), rel_dir, tests
    )
    if base_tree is None:
        return _skip("baseline copy failed")

    survivors: List[Dict[str, str]] = []
    for t in tests:
        node = f"{rel_dir}/{t['filename']}"
        try:
            bv = verify(
                base_tree,
                node,
                rerun_for_flake_check=0,
                test_command=cfg.get("test_command"),
                verify_timeout_s=int(cfg["verify_timeout_s"]),
                # VEX-PF-10: this is a PRISTINE tree, so the recorded
                # pre-existing failure set is the honest phase. It is written
                # under the attempt directory, never under the run root, so a
                # per-node baseline cannot overwrite the run's own.
                **_verify_rung_kwargs(
                    verify, cfg, run_dir=str(attempt_dir / "baseline"), phase="baseline"
                ),
            )
        except Exception as exc:
            return _skip(f"baseline verify crashed on {node}: {exc}")
        if bv.target_test_passed:
            trace.log(
                "agent_tests_baseline_pass",
                {
                    "attempt": attempts,
                    "node": node,
                    "note": "probes nothing pre-fix; dropped",
                },
            )
        else:
            survivors.append(t)

    if not survivors:
        trace.log(
            "agent_tests_all_baseline_pass",
            {
                "attempt": attempts,
                "note": "every generated test passed on the pre-fix tree; "
                "none probe the issue; gate skipped",
            },
        )
        shutil.rmtree(attempt_dir, ignore_errors=True)
        return True, ""

    # -- 3. POST-FIX STAGE (same verify(), fixed tree + tests) -----------
    # Same rigor as the final gate: the generated file is the TARGET, so
    # this runs it baseline_reruns times (flake detection) plus the full
    # suite (regression) — not a single lighter pass.
    fix_tree = agent_tests_mod.copy_with_tests(
        str(paths.work), str(attempt_dir / "postfix"), rel_dir, survivors
    )
    if fix_tree is None:
        return _skip("post-fix copy failed")

    failures: List[str] = []
    last_v: Optional[VerificationResult] = None
    for t in survivors:
        node = f"{rel_dir}/{t['filename']}"
        try:
            fv = verify(
                fix_tree,
                node,
                rerun_for_flake_check=int(cfg.get("baseline_reruns", 1)),
                test_command=cfg.get("test_command"),
                verify_timeout_s=int(cfg["verify_timeout_s"]),
                # VEX-PF-10: the agent-written tests are a GATING post-fix gate
                # (a post-fix failure poisons the attempt), so the flake rung
                # must be able to fire here exactly as it does at the final
                # gate. The baseline set is read from the run root, which the
                # baseline stage above recorded.
                **_verify_rung_kwargs(
                    verify, cfg, run_dir=str(paths.log_dir), phase="postfix"
                ),
            )
        except Exception as exc:
            return _skip(f"post-fix verify crashed on {node}: {exc}")
        last_v = fv
        trace.log(
            "agent_tests_verify",
            {
                "attempt": attempts,
                "node": node,
                "passed": fv.target_test_passed,
                "regression_passed": fv.regression_passed,
                "flaky": fv.flaky,
                "rung": getattr(fv, "verification_rung", ""),
                "rungs": list(getattr(fv, "verification_rungs", ()) or ()),
                "flake_check": getattr(fv, "flake_check", ""),
                "repetitions": getattr(fv, "repetitions", None),
                "raw": (fv.raw_output or "")[-2000:],
            },
        )
        if not fv.target_test_passed or not fv.regression_passed or fv.flaky:
            failures.append(node)
        else:
            # save the survivor for human review (deliberately OUTSIDE
            # work/; never part of the delivered diff)
            try:
                saved_dir.mkdir(parents=True, exist_ok=True)
                (saved_dir / f"attempt{attempts}_{t['filename']}").write_text(
                    t["content"], encoding="utf-8"
                )
            except OSError:
                pass

    # Transient trees are evidence but bulky; keep the last attempt's
    # only. The saved/ copies + trace carry what a human needs.
    shutil.rmtree(attempt_dir, ignore_errors=True)

    if failures:
        trace.log(
            "agent_tests_failed",
            {
                "attempt": attempts,
                "failed_nodes": failures,
            },
        )
        fb = (
            "The fix passed the given test, but agent-written EDGE-CASE "
            f"tests (implied by the issue) still FAIL: {', '.join(failures)}. "
            "The fix is incomplete for a case the issue implies. Failing "
            "test output:\n"
        )
        if last_v is not None:
            fb += _agent_tests_feedback(last_v, failures[-1])
        return False, fb

    trace.log(
        "agent_tests_passed",
        {
            "attempt": attempts,
            "nodes": [f"{rel_dir}/{t['filename']}" for t in survivors],
        },
    )
    return True, ""


# ----------------------------------------------------------------------------
# Product-grade output on verified completion (spec items 26/29)
# ----------------------------------------------------------------------------


def _verification_summary(v: VerificationResult, attempts: int) -> str:
    """One-line verifier evidence line for the commit message / PR body."""
    return (
        f"target test passed + full suite green (no regressions, not "
        f"flaky) after {attempts} attempt(s); verifier-gated completion"
    )


def _record_product_output(
    paths: TaskPaths,
    task: Task,
    cfg: Dict[str, Any],
    trace: TraceLogger,
    attempts: int,
    changed_files: List[str],
    diff: str,
    final_v: VerificationResult,
) -> None:
    """Rationale + git-native output for a VERIFIED fix (best-effort).

    Both features are product polish layered on a verified result, so a
    failure in either degrades to a trace event — it must NEVER change
    the task outcome (that's what "verifier-gated" means; spec items 26/29).

    - rationale (execution.rationale.build_rationale) reads
      logs/{task_id}/trace.jsonl + state.json and writes rationale.md.
    - git output (execution.git_output.produce_git_output) runs in the
      harness's PRIVATE work/ copy — git init (if needed), pristine-state
      first commit, then the fix as its own commit on a harness/fix-*
      branch; the original repo is untouched by construction. The result
      dict (branch, commit_sha, commit_message, pr_description) is saved
      to logs/{task_id}/git.json and the trace for downstream consumers
      (CLI/PR tooling).

    Assumes it is called BEFORE the "task_end" success event (so the
    rationale paragraph can cite the completed run) and after all
    files_touched are recorded (so state.json is complete for rationale).
    """
    summary = _verification_summary(final_v, attempts)
    rationale_text: Optional[str] = None

    if cfg.get("rationale_log", True):
        try:
            from execution.rationale import build_rationale

            rationale_text = (
                build_rationale(str(paths.log_dir), issue_text=task.issue_text) or None
            )
            if rationale_text:
                (paths.log_dir / "rationale.md").write_text(
                    f"# Rationale — {task.task_id}\n\n{rationale_text}\n",
                    encoding="utf-8",
                )
                trace.log("rationale", {"paragraph": rationale_text})
        except Exception as exc:  # best-effort by contract
            trace.log("rationale_failed", {"error": str(exc)})

    if cfg.get("git_output", True) and changed_files:
        try:
            from execution.git_output import produce_git_output

            out = produce_git_output(
                work_dir=str(paths.work),
                issue_text=task.issue_text,
                changed_files=changed_files,
                diff=diff,
                verification_summary=summary,
                rationale=rationale_text,
                branch_name=cfg.get("branch_name"),
                pristine_dir=str(paths.pristine),
            )
            (paths.log_dir / "git.json").write_text(
                json.dumps(out, indent=2), encoding="utf-8"
            )
            trace.log("git_output", out)
        except Exception as exc:  # best-effort by contract
            trace.log("git_output_failed", {"error": str(exc)})


def _record_rationale_only(
    paths: TaskPaths,
    task: Task,
    cfg: Dict[str, Any],
    trace: TraceLogger,
) -> None:
    """Write rationale.md for a NON-success terminal outcome (failed/
    timeout): the grounded "what happened / why it ended this way"
    paragraph is valuable on failures too (spec item 29 — a rationale
    alongside the trace, not just on wins). No git output: an unverified
    diff never gets a branch/commit/PR description. Best-effort, same
    contract as _record_product_output. Assumes task_end was logged.
    """
    if not cfg.get("rationale_log", True):
        return
    try:
        from execution.rationale import build_rationale

        paragraph = (
            build_rationale(str(paths.log_dir), issue_text=task.issue_text) or None
        )
        if paragraph:
            (paths.log_dir / "rationale.md").write_text(
                f"# Rationale — {task.task_id}\n\n{paragraph}\n", encoding="utf-8"
            )
            trace.log("rationale", {"paragraph": paragraph})
    except Exception as exc:  # best-effort by contract
        trace.log("rationale_failed", {"error": str(exc)})


# ----------------------------------------------------------------------------
# Step execution (one sub-step bash session)
# ----------------------------------------------------------------------------


def _detect_repo_language(repo_path: str) -> str:
    """'python' | 'js' for the task's repo (prompt flavor only).

    Delegates to execution.sandbox's detector when importable (the
    authoritative one — it also picks the dep image), else a local
    fallback with the same policy: Python markers win; package.json
    means js; a tests/ dir of JS/TS files without manifests means js.
    Never raises; 'python' is the default.
    """
    try:
        from execution.sandbox import _detect_repo_language as _det

        return _det(repo_path)
    except Exception:
        pass
    try:
        import os as _os

        py_markers = (
            "requirements.txt",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "Pipfile",
            "poetry.lock",
            "tox.ini",
            "environment.yml",
        )
        if any(_os.path.isfile(_os.path.join(repo_path, f)) for f in py_markers):
            return "python"
        if _os.path.isfile(_os.path.join(repo_path, "package.json")):
            return "js"
        tdir = _os.path.join(repo_path, "tests")
        if _os.path.isdir(tdir) and any(
            n.endswith((".js", ".ts", ".jsx", ".tsx", ".mjs"))
            for n in _os.listdir(tdir)
        ):
            return "js"
    except OSError:
        pass
    return "python"


def _log_recovery_stats(
    trace: Any,
    policy: Optional["tool_errors.RecoveryPolicy"],
    model_recovery: Optional["tool_errors.ModelRecovery"],
) -> Dict[str, Any]:
    """Emit the run's `recovery_stats` receipt and return the same payload.

    Records, per error kind, the occurrence count, how many recovered, and
    the MEAN TURNS-TO-RECOVERY — the measured statistic the recovery work is
    accountable for. An error still pending at task end is reported with
    `"pending": true` and contributes no recovery, so an unrecovered failure
    can never be reported as a fast recovery. Best-effort by contract: a
    trace-sink failure degrades to the returned payload, never a raised run.
    """
    payload: Dict[str, Any] = {"policy": {}, "model": {}}
    try:
        if policy is not None:
            payload["policy"] = policy.stats()
        if model_recovery is not None:
            payload["model"] = model_recovery.report()
    except Exception as exc:  # pragma: no cover — defensive
        payload["error"] = f"{type(exc).__name__}: {exc}"
    try:
        if trace is not None:
            trace.log("recovery_stats", payload)
    except Exception:  # pragma: no cover — observability must not kill a run
        pass
    return payload


def _new_cancel_token() -> Any:
    """A cancellation token for an in-flight command, best effort.

    Prefers `execution.workspace.CancellationToken` — the token type the real
    sandbox and the local execution handle both understand — and falls back to
    a local equivalent (the same `is_cancelled` / `cancel` surface) when the
    execution module is unavailable. It must never raise: an uncancellable
    command is strictly worse than a lost token, but a token failure must
    not be the thing that kills a run.
    """
    try:
        from execution.workspace import CancellationToken

        return CancellationToken()
    except Exception:  # pragma: no cover — defensive
        return threading.Event()


def run_step(
    task: Task,
    step: Dict[str, Any],
    plan: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    paths: TaskPaths,
    state: TaskState,
    trace: TraceLogger,
    model: ModelClient,
    step_files: List[str],
    completed: List[str],
    feedback: str,
    verify: Callable[..., VerificationResult],
    deadline: float,
    steer: Optional["steering_mod.SteeringBuffer"] = None,
    machine: Optional["TaskStateMachine"] = None,
    policy: Optional["tool_errors.RecoveryPolicy"] = None,
    model_recovery: Optional["tool_errors.ModelRecovery"] = None,
) -> Tuple[bool, str, Optional[VerificationResult]]:
    """Run ONE planner sub-step in its own fresh bash session.

    Returns (ok, note, verification):
    - ok=True: the session ended via SUBMIT and edits validated cleanly.
      verification carries the post-step verify() result (which may or may
      not show the target passing — mid-plan steps often don't yet).
    - ok=False: edits failed validation, turns exhausted, wall-clock hit,
      or the model call failed unrecoverably. note explains why (feeds the next
      attempt); "FATAL:" prefix means the whole task should error out, and is
      now reserved for TERMINAL model/provider failures only — a transient
      provider failure is retried with bounded backoff (G15) instead.
      STEERING (steering round): a "STEER-REPLAN:" / "STEER-ABORT:"
      note prefix means a strong steering intent arrived at a TURN
      boundary — the caller's step-boundary handler consumes it (the
      step ends not-ok; the strong intent is still pending on purpose).
      "LOOP-GUARD:" means repeated identical commands tripped doom-loop
      protection; the note carries the offending command and the step ended
      rather than burning the remaining turns on it.
    Assumes the caller owns the pristine/work layout and retry policy.

    steer/machine (optional, default None = pre-steering behavior for
    every existing direct caller): the task's steering inbox and state
    machine. At each TURN boundary (before the next model call) pending
    steering is consumed: guide events inject a USER STEERING message
    into the LIVE session (the model continues with the instruction
    visible); strong intents end the step immediately so the loop's
    step-boundary handler can act on them.

    policy/model_recovery (optional): the task-scoped recovery machinery
    (VEX-CEILING-07). Passing the task's own instances is what makes the
    recovery STATISTICS span the whole task instead of one step; a direct
    caller that omits them gets a fresh pair bound to this step, so the
    single-code-path contract holds for every existing call site.
    """
    step_id = int(step["id"])
    total_steps = len(plan)
    if policy is None:
        policy = tool_errors.recovery_policy_from_config(cfg, root=str(paths.work))
    if model_recovery is None:
        model_recovery = tool_errors.ModelRecovery(
            trace=trace,
            max_attempts=int(cfg.get("max_model_attempts", 3)),
            base_backoff_s=float(cfg.get("model_retry_base_s", 0.5)),
            cap_backoff_s=float(cfg.get("model_retry_cap_s", 8.0)),
            label=f"step-{step_id}",
        )

    protected = [str(p) for p in (cfg.get("protected_paths") or [])]
    if _authorized_test_target(cfg, task.issue_text):
        protected = [
            p for p in protected if p not in {"tests/*", "test_*.py", "*_test.py"}
        ]
    step_budget = retrieval.size_context_budget(
        task.issue_text,
        str(paths.work),
        files_touched=list(state.files_touched),
        base_files=int(cfg.get("context_files_cap", 4)),
        base_lines=int(cfg.get("context_lines_cap", 60)),
    )
    context_files_cap = min(
        int(cfg.get("context_files_cap", 4)), step_budget["max_files"]
    )
    context_lines_cap = min(
        int(cfg.get("context_lines_cap", 60)), step_budget["max_lines"]
    )

    # The legacy step session keeps its historical SINGLE-system-message shape
    # unless `step_prompt_cache_split` explicitly asks for the cacheable one.
    # Splitting it into [frozen system, per-turn system, user] makes the leading
    # segment byte-identical across every turn of every step, which is what a
    # provider prompt cache needs - but it changes the message shape the step
    # loops' scripted models dispatch on (tests/test_webfetch.py,
    # tests/test_batch_docs_lint.py, tests/test_build_plan_e2e.py, evals run.py,
    # harness/_stubs/scripted_model.py all read the FIRST system message, which
    # under the split no longer carries "your step is #N of"). So this is an
    # explicit single key, defaulted to None, rather than a silent switch.
    #
    # Any dispatcher migrating to it must use `prompts.step_system_text`, which
    # joins EVERY system message and is therefore correct for both shapes.
    split_step_prompt = bool(cfg.get("step_prompt_cache_split"))
    step_context_block = _context_block(
        str(paths.work),
        step_files,
        context_lines_cap,
        context_files_cap,
    )
    first_user = (
        (f"## Feedback from the previous attempt\n{feedback}\n\n" if feedback else "")
        + "Begin. Reply with exactly ONE bash command, or SUBMIT if this "
        "step is already done."
    )
    step_prompt_kwargs = dict(
        issue_text=task.issue_text,
        plan=plan,
        step_id=step_id,
        total_steps=total_steps,
        context_block=step_context_block,
        max_output_chars=int(cfg["max_output_chars"]),
        language=cfg.get("_repo_language"),
    )
    if split_step_prompt:
        messages: List[Dict[str, str]] = prompts.render_step_messages(
            completed_block="\n".join(completed) or "(none yet)",
            first_user=first_user,
            **step_prompt_kwargs,
        )
    else:
        messages = [
            {
                "role": "system",
                "content": prompts.render_step_system(
                    completed_block="\n".join(completed) or "(none yet)",
                    **step_prompt_kwargs,
                ),
            },
            {"role": "user", "content": first_user},
        ]
    _log_step_prompt_cache(
        trace,
        messages,
        step_id=step_id,
        split=split_step_prompt,
        tools_sent=0,
    )

    # VEX-CEILING-07: a cancellation token makes an in-flight command
    # interruptible, so a hard steering abort terminates a long-running child
    # in seconds instead of after the whole command_timeout_s budget. The
    # token comes from the execution module when available and degrades to a
    # plain local event otherwise — a same-semantics fallback, never a gap.
    cancel_token = _new_cancel_token()
    session = tool_mod.BashSession(
        repo_path=str(paths.work),
        timeout_s=int(cfg["command_timeout_s"]),
        max_output_chars=int(cfg["max_output_chars"]),
        cancellation_token=cancel_token,
    )
    reinject = prompts.render_constraint_reinjection(
        issue_text=task.issue_text,
        plan=plan,
        step_id=step_id,
        total_steps=total_steps,
        completed=completed,
        protected_paths=protected,
    )

    max_turns = int(cfg["max_step_turns"])
    recalls_used = 0
    max_recalls = int(cfg.get("max_recalls_per_step", 3))
    docs_used = 0
    max_docs = int(cfg.get("max_docs_per_step", 3))
    fetches_used = 0
    max_fetches = int(cfg.get("max_fetches_per_step", 3))
    # Shared docs-cache root (Round 8, Task D) seeded by run_task.
    logs_root_docs = cfg.get("_docs_cache_root")
    for turn in range(max_turns):
        if time.time() >= deadline:
            return False, "wall-clock limit hit mid-step", None
        policy.set_turn(turn)

        # -- STEERING: TURN boundary checkpoint (tool boundary) ----------
        # G14: this is a safe boundary because the message list is at a clean,
        # well-defined state. A hard intent detected here, OR left pending by
        # the in-flight watcher below, yields the step WITHOUT consuming — the
        # loop's step-boundary handler owns the single consume point.
        if steer is not None and steer.pending():
            if steer.has_intent("abort") or steer.has_intent("replan"):
                which = "abort" if steer.has_intent("abort") else "replan"
                # NOT consumed here: the loop's step-boundary handler
                # owns the strong intents (single consume point).
                trace.log(
                    "steering_step_yield",
                    {
                        "step_id": step_id,
                        "turn": turn,
                        "intent": which,
                        "at": "tool-boundary",
                    },
                )
                if machine is not None:
                    machine.record_event(
                        "steering",
                        {"at": "tool-boundary", "step": step_id, "intent": which},
                    )
                return (
                    False,
                    f"STEER-{which.upper()}: user steering requires a "
                    f"{which} at the task level",
                    None,
                )
            taken = steer.take(f"step-{step_id}-turn-{turn}")
            if machine is not None:
                machine.record_event(
                    "steering",
                    {"seqs": [e.seq for e in taken], "at": "turn-boundary"},
                )
            trace.log(
                "steering",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "at": "turn-boundary",
                    "seqs": [e.seq for e in taken],
                    "intents": [e.intent for e in taken],
                    "texts": [e.text for e in taken],
                },
            )
            # Re-inject the conversation: the steering message becomes
            # the next user message the model sees (in place of a tool
            # result — there is no in-flight command at this point).
            messages.append(
                {
                    "role": "user",
                    "content": prompts.render_steering_msg([e.text for e in taken]),
                }
            )
            # Do NOT continue the loop here — fall through to the model
            # call so this turn is the steering turn.

        # -- MODEL FAILURE TOLERANCE (G15) -----------------------------
        # A 429/5xx/connection/timeout is retried with bounded backoff
        # inside ModelRecovery (emitting `model_recovery` per decision).
        # Only a TERMINAL failure (auth, bad request, harness-internal)
        # reaches the FATAL path — a transient provider blip can no longer
        # end an otherwise healthy run.
        try:
            reply = model_recovery.call(
                lambda: model.call(messages, step=f"step-{step_id}"),
                step=f"step-{step_id}",
            )
        except Exception as exc:
            failure = model_recovery.last_failure
            kind = failure.kind if failure is not None else "model_internal"
            trace.log(
                "model_failure_terminal",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "kind": kind,
                    "error": str(exc)[:500],
                    "attempts": model_recovery.attempts,
                },
            )
            return (
                False,
                f"FATAL: terminal model failure ({kind}) during step "
                f"{step_id} after {model_recovery.attempts} attempt(s): {exc}",
                None,
            )
        if time.time() >= deadline:
            return False, "wall-clock limit hit after model call", None
        if tool_mod.is_submit(reply):
            ok, msg, changed = editor.check_edits(
                str(paths.pristine), str(paths.work), protected
            )
            if not ok:
                return False, f"edit validation failed: {msg}", None
            # Round 8 lint gate (Task C): BEFORE paying for the verify
            # cycle (sandboxed pytest), a fast host-side static pass over
            # the CHANGED files. check_edits above already refused syntax
            # errors; the NEW value here is the undefined-name class plus
            # the in-session fix loop. A finding does NOT end the step:
            # the model is mid-session with full context — it gets the
            # classified findings and can fix and re-SUBMIT within this
            # session (cheapest fix loop; the turn budget bounds it).
            # Lint never gates success.
            if cfg.get("lint_gate", True):
                findings = lint_mod.lint_changed(
                    str(paths.work),
                    changed,
                    check_names=bool(cfg.get("lint_names", True)),
                )
                if findings:
                    trace.log(
                        "lint_failed",
                        {
                            "step_id": step_id,
                            "turn": turn,
                            "findings": [
                                {
                                    "file": f.file,
                                    "line": f.line,
                                    "kind": f.kind,
                                    "message": f.message,
                                }
                                for f in findings
                            ],
                        },
                    )
                    messages.append({"role": "assistant", "content": reply})
                    messages.append(
                        {
                            "role": "user",
                            "content": lint_mod.render_findings(findings)
                            + "\nFix these, then SUBMIT again (or continue "
                            "with bash commands).",
                        }
                    )
                    continue
            for rel in changed:
                state.record_file_touched(rel)
            try:
                v = verify(
                    str(paths.work),
                    cfg.get("target_test"),
                    rerun_for_flake_check=int(cfg.get("baseline_reruns", 1)),
                    test_command=cfg.get("test_command"),
                    verify_timeout_s=int(cfg["verify_timeout_s"]),
                    # VEX-PF-10: this is the per-step SUBMIT checkpoint and it
                    # is NOT a completion gate — success is minted only at the
                    # final gate. It still passes `rung_config` so the receipt
                    # NAMES its rung, but it is deliberately NOT a baseline-set
                    # phase: recording a "preexisting" set from a mid-run
                    # checkpoint would attribute an inherited failure to the
                    # pristine tree, which is false precision. An operator who
                    # configures `post_fix_reruns` therefore pays 2 target runs
                    # per step turn here too; that is the configured cost, and
                    # `execution/AGENTS.md` (R2-02) measured it.
                    **_verify_rung_kwargs(verify, cfg),
                )
                if not _verification_evidence_ok(v):
                    raise ValueError("verifier returned incomplete evidence")
            except Exception as exc:
                return (
                    False,
                    f"FATAL: verifier crashed after edit validation: {exc}",
                    None,
                )
            trace.log(
                "verify",
                {
                    "step_id": step_id,
                    "target_passed": v.target_test_passed,
                    "regression_passed": v.regression_passed,
                    "flaky": v.flaky,
                    "rung": getattr(v, "verification_rung", ""),
                    "rungs": list(getattr(v, "verification_rungs", ()) or ()),
                    "flake_check": getattr(v, "flake_check", ""),
                    "repetitions": getattr(v, "repetitions", None),
                    "raw": v.raw_output[-3000:],
                },
            )
            if v.target_test_passed:
                return True, "checkpoint passed", v
            return True, _target_feedback(v, step), v

        # RECALL: on-demand reinjection of compacted detail (spec item 13).
        # state.json is the compacted view; trace.jsonl keeps everything;
        # this is the hook that pulls older detail back into the live
        # session when a later step realizes it matters. Parsed on BOTH the
        # raw reply and its fence-stripped form: a fenced "```bash\nRECALL
        # x\n```" must still be a RECALL, never a bash command to execute.
        recall_query = tool_mod.parse_recall(reply)
        if recall_query is None:
            extracted = _extract_command(reply)
            if extracted is not None and tool_mod.parse_recall(extracted) is not None:
                recall_query = tool_mod.parse_recall(extracted)
        if recall_query is not None:
            if recalls_used >= max_recalls:
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": "RECALL budget for this step is exhausted; proceed with "
                        "bash commands (re-run a command if you need fresh "
                        "output), or SUBMIT if the step is done.",
                    }
                )
                continue
            recalls_used += 1
            entries = trace.find_events(
                recall_query,
                limit=int(cfg.get("recall_results_cap", 5)),
                max_chars=int(cfg.get("recall_max_chars", 4000)),
            )
            trace.log(
                "recall",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "query": recall_query,
                    "matched": len(entries),
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": prompts.render_recall_result(recall_query, entries)
                    + "\n\n"
                    + reinject,
                }
            )
            continue

        command = _extract_command(reply)

        # BATCH: several independent READ-ONLY commands in one turn
        # (Round 8, Task B). Parsed on the raw reply AND the extracted
        # command (a fenced BATCH must never reach the shell either).
        batch_cmds = tool_mod.parse_batch(reply)
        if batch_cmds is None and command is not None:
            batch_cmds = tool_mod.parse_batch(command)
        if batch_cmds is not None:
            bad = tool_mod.validate_batch(batch_cmds)
            if bad is not None:
                trace.log(
                    "batch_rejected", {"step_id": step_id, "turn": turn, "entry": bad}
                )
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": f"BATCH REJECTED: entry {bad!r} is not a simple "
                        "read-only command (no composition operators, no "
                        "side effects). Re-issue as individual commands, or "
                        "BATCH with only simple read-only entries.",
                    }
                )
                continue
            started = time.time()
            records, rendered = tool_mod.run_batch(
                str(paths.work),
                batch_cmds,
                timeout_s=int(cfg["command_timeout_s"]),
                max_output_chars=int(cfg["max_output_chars"]),
            )
            trace.log(
                "batch_call",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "commands": list(batch_cmds),
                    "elapsed_s": round(time.time() - started, 2),
                },
            )
            for rec in records:
                trace.log(
                    "tool_call",
                    {
                        "step_id": step_id,
                        "turn": turn,
                        "command": rec.get("command"),
                        "batch": True,
                    },
                )
                trace.log(
                    "tool_result",
                    {
                        "step_id": step_id,
                        "turn": turn,
                        "output": rec.get("output"),
                        "batch": True,
                    },
                )
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": rendered + reinject})
            continue

        # DOCS: documentation/API lookup (Round 8, Task D). Same
        # raw+fence-stripped parse discipline as RECALL/BATCH.
        docs_query = tool_mod.parse_docs(reply)
        if docs_query is None and command is not None:
            docs_query = tool_mod.parse_docs(command)
        if docs_query is not None:
            if not cfg.get("docs_lookup_enabled", True):
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": "DOCS lookups are disabled for this task. Proceed "
                        "with bash commands, or SUBMIT if the step is done.",
                    }
                )
                continue
            if docs_used >= max_docs:
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": "DOCS budget for this step is exhausted; proceed with "
                        "bash commands, or SUBMIT if the step is done.",
                    }
                )
                continue
            docs_used += 1
            msg, res = docs_lookup_mod.lookup_and_render(
                docs_query,
                Path(logs_root_docs)
                if logs_root_docs
                else Path(cfg.get("work_subdir", "logs")) / "_docs-cache",
                max_chars=int(cfg.get("docs_max_chars", 3000)),
                allow_remote=bool(cfg.get("docs_lookup_allow_remote", False)),
            )
            trace.log(
                "docs_lookup",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "query": docs_query,
                    "source": res.source,
                    "ok": res.ok,
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": msg + "\n\n" + reinject})
            continue

        # FETCH: general-purpose web-page reading (generalizes DOCS to
        # anything on the web — an unfamiliar library's docs page, a
        # stdlib HOWTO, an error explanation). Same raw+fence-stripped
        # parse discipline as RECALL/BATCH/DOCS; the URL is never
        # executed as shell. Read-only GET with SSRF guards, timeout,
        # size caps; every fetch trace-logged for auditability.
        fetch_url = webfetch_mod.parse_fetch(reply)
        if fetch_url is None and command is not None:
            fetch_url = webfetch_mod.parse_fetch(command)
        if fetch_url is not None:
            if not cfg.get("web_fetch_enabled", True):
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": "FETCH is disabled for this task. Proceed with bash "
                        "commands, or SUBMIT if the step is done.",
                    }
                )
                continue
            if fetches_used >= max_fetches:
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": "FETCH budget for this step is exhausted; proceed with "
                        "bash commands, or SUBMIT if the step is done.",
                    }
                )
                continue
            fetches_used += 1

            def _audit_fetch(res, _sid=step_id, _turn=turn, _url=fetch_url):
                # Every fetch is auditable from the task's trace alone:
                # URL + outcome (+ timestamp, like any tool call).
                trace.log(
                    "web_fetch",
                    {
                        "step_id": _sid,
                        "turn": _turn,
                        "url": _url,
                        "ok": res.ok,
                        "status": res.status,
                        "chars": len(res.text),
                    },
                )
                # Unified cross-module overlay (shared.tracing): a
                # host-side fetch is a harness-layer event other modules'
                # dashboards should see; opt-in env, never raises.
                try:
                    from shared import tracing as _st

                    _st.emit(
                        "harness",
                        "web_fetch",
                        task_id=task.task_id,
                        url=_url,
                        ok=res.ok,
                        status=res.status,
                    )
                except Exception:
                    pass

            msg, res = webfetch_mod.fetch_and_render(
                fetch_url,
                timeout_s=int(cfg.get("webfetch_timeout_s", 15)),
                max_bytes=int(cfg.get("webfetch_max_bytes", 1_048_576)),
                max_chars=int(cfg.get("webfetch_max_chars", 3000)),
                max_redirects=int(cfg.get("webfetch_max_redirects", 3)),
                audit_hook=_audit_fetch,
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": msg + "\n\n" + reinject})
            continue

        if not command:
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": "Your last reply contained no runnable bash command. Reply "
                    "with exactly ONE bash command (no prose, no code fences), "
                    "or SUBMIT if the step is done.",
                }
            )
            continue

        # -- TOOL BOUNDARY: three checks run BEFORE the command does -----
        # (1) hard steering abort that arrived while the model was thinking;
        # (2) a path the permission policy already forbade; (3) a repeated
        # identical command (doom-loop protection). Each one changes what
        # HAPPENS, not just what the model is told.
        if steer is not None and steer.has_intent("abort"):
            trace.log(
                "steering_step_yield",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "intent": "abort",
                    "at": "pre-dispatch",
                },
            )
            return (
                False,
                "STEER-ABORT: user steering requires an abort at the task level",
                None,
            )

        refusal = policy.rejects(command)
        if refusal:
            trace.log(
                "command_refused",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "command": command[:500],
                    "reason": refusal,
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"RECOVERY [{tool_errors.KIND_PERMISSION_DENIED}] -> "
                        f"forbid_path\n{refusal}\n"
                        "Pick different paths, or state that the task cannot "
                        "proceed without them."
                    )
                    + reinject,
                }
            )
            continue

        loop_action = policy.note_command(command)
        if loop_action is not None:
            trace.log(
                "loop_guard",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "command": command[:500],
                    "repeats": loop_action.attempts,
                    "action": loop_action.action,
                },
            )
            blocked = policy.release_blocked_command()
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": policy.feedback(loop_action) + "\n" + reinject,
                }
            )
            return (
                False,
                f"LOOP-GUARD: repeated identical command "
                f"({loop_action.attempts}x) stopped the step: {(blocked or command)[:200]}",
                None,
            )

        # -- IN-FLIGHT ABORT (G14) --------------------------------------
        # A hard abort injected while this command runs must terminate the
        # child process, not be noticed only after the command returns. The
        # watcher polls the steering journal and cancels the session, whose
        # token the real sandbox polls every 50ms (killing the container) and
        # the local handle acts on by killing the process tree.
        watcher = steering_mod.HardAbortWatcher(steer, session.cancel).start()
        try:
            output = session.run(command)
        except PermissionError as exc:
            output = None
            policy_error = tool_errors.ToolError(
                tool_errors.KIND_COMMAND_REJECTED, str(exc)
            )
            action = policy.on_tool_error(
                policy_error,
                command=command,
                files_touched=sorted(session.files_touched),
            )
            trace.log(
                "tool_error",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "kind": action.kind,
                    "action": action.action,
                    "detail": policy_error.detail[:500],
                    "recovery": action.to_dict(),
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {"role": "user", "content": policy.feedback(action) + "\n" + reinject}
            )
            continue
        except tool_mod.ToolExecutionError as exc:
            # Round 8 (Task A): a classified harness-side tool failure —
            # structured feedback instead of a traceback into the model's
            # context. VEX-CEILING-07: the failure now selects a RECOVERY
            # ACTION (see harness.tool_errors.POLICY), not just prose.
            action = policy.on_tool_error(
                tool_errors.ToolError(exc.kind, exc.detail),
                command=command,
                files_touched=sorted(session.files_touched),
            )
            if action.timeout_s:
                session.set_timeout(action.timeout_s)
            trace.log(
                "tool_error",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "kind": action.kind,
                    "action": action.action,
                    "detail": exc.detail[:500],
                    "recovery": action.to_dict(),
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {"role": "user", "content": policy.feedback(action) + "\n" + reinject}
            )
            continue
        finally:
            joined = watcher.stop()

        if watcher.errors:
            # A watcher that could not interrupt must say so. An abort that
            # silently failed to abort is worse than one that reported the
            # failure, so the errors ride the trace.
            trace.log(
                "steering_abort_watcher_error",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "errors": watcher.errors[:4],
                    "aborted": watcher.aborted,
                },
            )

        trace.log("tool_call", {"step_id": step_id, "turn": turn, "command": command})
        trace.log("tool_result", {"step_id": step_id, "turn": turn, "output": output})

        # The in-flight RESULT is never silently discarded: the command's
        # output is traced and appended to the conversation BEFORE the hard
        # abort ends the step, so a resumed run can still see what it found.
        aborted_in_flight = watcher.aborted
        if aborted_in_flight:
            trace.log(
                "steering_abort_in_flight",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "at": "tool-boundary",
                    "command": command[:500],
                    "elapsed_s": watcher.elapsed_to_abort_s(),
                    "result_preserved_chars": len(output or ""),
                    "watcher_joined": joined,
                    "texts": watcher.abort_texts[:4],
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"COMMAND ABORTED BY USER STEERING: {command[:200]}\n"
                        f"Its partial output is preserved here:\n"
                        f"{output or '(no output)'}\n"
                        "The task will stop cleanly and stay resumable."
                    ),
                }
            )
            return (
                False,
                "STEER-ABORT: user steering aborted the in-flight command "
                f"after {watcher.elapsed_to_abort_s()}s; its result was preserved",
                None,
            )

        # A command that FAILED is where the per-kind recovery policy acts.
        # The rendered TOOL ERROR text is still in `output` (so the model sees
        # the same diagnostic as before); the action adds the evidence and
        # changes what the loop does next.
        if session.last_error is not None:
            action = policy.on_tool_error(
                session.last_error,
                command=command,
                files_touched=sorted(session.files_touched),
            )
            if action.timeout_s:
                session.set_timeout(action.timeout_s)
            trace.log(
                "command_recovery",
                {
                    "step_id": step_id,
                    "turn": turn,
                    "command": command[:500],
                    "recovery": action.to_dict(),
                },
            )
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": f"{output}\n\n{policy.feedback(action)}\n{reinject}",
                }
            )
            continue

        # Constraint re-injection at the point of max recency (spec item 15).
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": output + reinject})

    return (
        False,
        f"step {step_id} exhausted its {max_turns} command turns without SUBMIT",
        None,
    )


def _extract_command(reply: str) -> Optional[str]:
    """Extract a single bash command from a model reply.

    Handles bare commands, fenced ```bash blocks (whole block, so
    multi-line commands survive), COMMAND:/RUN: prefixes, and bare
    multi-line heredocs (<<'EOF' ... EOF — returned whole). Returns None
    for prose-only replies (caller nudges the model).
    """
    text = (reply or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:bash|sh|shell)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    m = re.match(r"^\s*(?:COMMAND|CMD|RUN)\s*[:=]\s*(.+)$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Bare heredoc (multi-line python/shell script fed via stdin): the
    # whole reply IS the command — extracting one line would behead it.
    if re.search(r"<<\s*['\"]?EOF['\"]?\s*$", text.splitlines()[0]):
        return text
    lines = [l for l in text.splitlines() if l.strip()]
    # Single-line bare command: no prose markers (no ". ", "! ", "? ",
    # no sentence-final period — but "cd .." style tails are allowed).
    if (
        len(lines) == 1
        and len(text) < 500
        and not re.search(r"[.!?] +", text)
        and not re.search(r"[a-z][.!?]$", text)
    ):
        return text
    # Multi-line without fences: take the first line that looks like a
    # command (the common "prose intro + command" reply shape).
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "//")) or len(line) >= 500:
            continue
        first = line.split()[0]
        if first in _SHELL_VERBS or "/" in first or first.endswith(".py"):
            return line
    return None


_SHELL_VERBS = {
    "cat",
    "ls",
    "dir",
    "grep",
    "rg",
    "find",
    "sed",
    "awk",
    "echo",
    "cd",
    "pwd",
    "python",
    "python3",
    "pytest",
    "pip",
    "git",
    "head",
    "tail",
    "wc",
    "touch",
    "mkdir",
    "rm",
    "mv",
    "cp",
    "diff",
    "export",
    "source",
    "which",
    "where",
    "chmod",
    "tee",
    "printf",
    "sort",
    "uniq",
    "xargs",
    "true",
    "false",
    "sleep",
    "date",
    "env",
    "set",
    "unset",
}


def _resolve_budget_governor() -> Optional[Any]:
    """Return this run's budget governor, or ``None`` when there is none.

    R2-14. Resolution order:

    1. ``runtime.budget_governor.current_governor()`` -- the execution
       context the worker installed.
    2. the ``budget_governor`` key on the router context -- the same
       governor, installed separately so the dial path can reach it
       without importing this module.

    Returns ``None`` (never raises) when ``runtime`` is absent, when
    neither is installed, or when the installed value is not a
    ``BudgetGovernor``. A harness-only install therefore keeps the
    historical attempt-level budget check, which is why absence is
    reported by the caller rather than being silently indistinguishable
    from a cap that was never configured.
    """
    try:
        from runtime import budget_governor as governor_mod
    except ImportError:
        return None
    candidate = governor_mod.current_governor()
    if isinstance(candidate, governor_mod.BudgetGovernor):
        return candidate
    try:
        from runtime import model_router

        routed = (model_router._CONTEXT.get() or {}).get("budget_governor")
    except Exception:
        return None
    if isinstance(routed, governor_mod.BudgetGovernor):
        return routed
    return None


def _bind_budget_guard(
    governor: Any,
    model: Any,
    trace: Any,
    task_id: str,
) -> None:
    """Wrap this run's model client so every call is priced before it dials.

    R2-14. The wrapper is installed on the INSTANCE, not on the class, and
    it is idempotent (``_neo_budget_guarded``): a run that already has a
    guard is left alone, so a re-entrant ``run_task`` cannot stack them.

    A refusal raises the governor's ``BudgetRefused``. Callers already
    treat a model-call failure as a failed step, and ``over_budget()`` is
    re-evaluated at the top of the next attempt -- where the governor's
    sticky ``exhausted`` flag makes the run end honestly. The wrapper
    never converts a refusal into a success and never swallows one.

    Assumes ``model`` exposes ``call``; a client without it is left
    untouched rather than raising here.
    """
    if getattr(model, "_neo_budget_guarded", False):
        return
    original = getattr(model, "call", None)
    if not callable(original):
        return
    client_config = getattr(model, "config", None)
    if not isinstance(client_config, dict):
        client_config = {}

    def _guarded_call(messages: Any, *args: Any, **kwargs: Any) -> Any:
        from runtime import budget_governor as governor_module

        verdict = governor.authorize_call(
            messages=messages,
            target=_governor_target(client_config, kwargs),
            # The run's own DECLARED completion bound. Supplying it makes
            # the reservation a sound upper bound; without one the enforced
            # guarantee is "cap + the price of the final call", which is
            # what the handoff states.
            max_completion_tokens=client_config.get("max_completion_tokens"),
        )
        trace.log("budget_check", {"phase": "pre_call", **verdict.as_dict()})
        if not verdict.allowed:
            trace.log(
                "stop",
                {
                    "reason": "budget cap (per-call pre-check)",
                    "usage": model.snapshot_usage(),
                    **verdict.as_dict(),
                },
            )
            raise governor_module.BudgetRefused(verdict)
        reservation = verdict.reserved_usd
        try:
            return original(messages, *args, **kwargs)
        finally:
            governor.release(reservation)

    _guarded_call._neo_budget_guarded = True  # type: ignore[attr-defined]
    _guarded_call.__doc__ = (
        "ModelClient.call with the R2-14 per-call budget pre-check "
        "(installed by harness.core._bind_budget_guard)."
    )
    model.call = _guarded_call  # type: ignore[method-assign]
    model._neo_budget_guarded = True  # type: ignore[attr-defined]


def _governor_target(
    client_config: Dict[str, Any], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the price target for the call the model client is about to make.

    The harness does not resolve routes -- that is the router's job -- so
    this reads only what is already DECLARED: the client's own config (what
    ``ModelClient.call`` forwards to the boundary) plus any per-call
    override. When no model is declared, the target is empty and the
    governor reports ``unpriced`` -- a refusal cannot be manufactured from
    a guess, and the receipt says so instead of pricing the call at zero.
    """
    target: Dict[str, Any] = {}
    for source in (client_config, kwargs):
        model = source.get("model")
        provider = source.get("provider")
        if isinstance(model, str) and model:
            target["model"] = model
        if isinstance(provider, str) and provider:
            target["provider"] = provider
    return target


def _verification_evidence_ok(value: Any) -> bool:
    """Return whether a verifier result contains the required evidence fields."""
    return (
        value is not None
        and all(
            hasattr(value, name)
            for name in (
                "target_test_passed",
                "regression_passed",
                "flaky",
                "raw_output",
            )
        )
        and isinstance(value.target_test_passed, bool)
        and isinstance(value.regression_passed, bool)
        and isinstance(value.flaky, bool)
        and isinstance(value.raw_output, str)
    )


def _with_baseline(
    v: VerificationResult, baseline_passed_field: bool
) -> VerificationResult:
    """Fill baseline_passed on a VerificationResult ("did the target test
    pass BEFORE any edit?" — known by the caller from the pristine run)."""
    return VerificationResult(
        target_test_passed=v.target_test_passed,
        baseline_passed=baseline_passed_field,
        regression_passed=v.regression_passed,
        flaky=v.flaky,
        raw_output=v.raw_output,
        structured_feedback=list(getattr(v, "structured_feedback", []) or []),
    )


def _structured_or_tail(v: VerificationResult, tail_chars: int = 1500) -> str:
    """Render a failing verification as model-facing feedback text.

    Boundary 7 consumption (INTERFACES.md): when the result carries
    structured_feedback (parsed FeedbackObject dicts — the new verify()
    fills it on failing runs), render format_objects() — the compact
    "which test / what kind / expected vs actual / where" shape —
    instead of a raw stdout tail. Falls back to the raw tail for
    results without the field (stub-produced, historical serialization)
    so adoption is graceful; feedback_from_result replays identical
    objects from raw_output alone when only the trace's copy exists.
    Assumes v is a VerificationResult from any verify() implementation.
    """
    fb = getattr(v, "structured_feedback", None) or []
    if fb:
        try:
            from execution.feedback import FeedbackObject, format_objects

            objs = [
                FeedbackObject(
                    test_id=d.get("test_id"),
                    failure_type=d.get("failure_type", "unparseable"),
                    summary=d.get("summary", ""),
                    expected=d.get("expected"),
                    actual=d.get("actual"),
                    file=d.get("file"),
                    line=d.get("line"),
                    traceback_summary=d.get("traceback_summary", ""),
                )
                for d in fb
                if isinstance(d, dict)
            ]
            if objs:
                return format_objects(objs)
        except Exception:
            pass
    return (v.raw_output or "")[-tail_chars:]


def _target_feedback(
    v: VerificationResult, step: Optional[Dict[str, Any]] = None
) -> str:
    """Feedback when the target test still fails after a step SUBMIT."""
    desc = (step or {}).get("description", "?")
    tail = _structured_or_tail(v)
    return (
        f"Step '{desc}' submitted, but the target test STILL FAILS "
        f"(regression_passed={v.regression_passed}, flaky={v.flaky}). "
        f"Structured failures:\n{tail}"
    )


def _regression_feedback(v: VerificationResult) -> str:
    """Feedback when the full suite regressed (spec item 5)."""
    tail = _structured_or_tail(v)
    return (
        "The target test passed BUT the full suite regressed — your change "
        f"likely broke something else (regression_passed={v.regression_passed}). "
        f"Structured failures:\n{tail}"
    )


def _result(
    task: Task,
    status: str,
    attempts: int,
    diff: Optional[str],
    verification: Optional[VerificationResult],
    client: ModelClient,
    trace: TraceLogger,
    note: str = "",
) -> TaskResult:
    """Assemble the TaskResult (Boundary 3) and log it."""
    trace.log(
        "result",
        {
            "status": status,
            "attempts": attempts,
            "note": note,
            "cost_usd": client.total_cost_usd,
        },
    )
    return TaskResult(
        task_id=task.task_id,
        status=status,
        attempts=attempts,
        diff=diff,
        verification=verification,
        cost_usd=round(client.total_cost_usd, 6),
        model_calls=list(client.model_calls),
        log_path=str((trace.log_dir / "trace.jsonl").resolve()),
    )
