"""One-task worker process — spawned by the scheduler via subprocess.

Entry: `python -m runtime.worker --task-json <path> --run-dir <path>`

Responsibilities:
  1. Load its Task from JSON (arguments file, not argv, so payloads of any
     size/content pass cleanly).
  2. Set the router call context (model cfg + per-call ledger) for THIS task.
  3. Build and install the task's budget governor (R2-14) so the dial
     boundary, the hang watchdog, and the attempt-level cap all read ONE
     budget, ONE clock, and ONE deadline.
  4. Run run_task (real harness if importable, fake otherwise), heartbeating
     throughout via a daemon thread.
  5. Enforce the approval gate when config["approval"] == "require".
  6. Write result.json (TaskResult dict) + final checkpoint; exit 0.

A killed worker leaves its last checkpoint + state.json intact — that IS
the crash-resume mechanism; the scheduler relaunches with the same
arguments file and the task resumes from completed steps.

Exit codes: 0 normal (any TaskResult.status), 70 fake-crash injection,
1 unexpected worker-level exception (logged to stderr + events).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from time import time_ns
from typing import Any, Dict, Optional

from runtime import approval as approval_mod
from runtime import budget_governor, mock_provider
from runtime.checkpoint import (
    TaskCheckpoint,
    checkpoint_identity,
    checkpoint_identity_matches,
    should_resume,
)
from runtime.config import DEFAULT_HANG_STALE_S, HEARTBEAT_INTERVAL_S, apply_defaults
from runtime.fsutil import (
    TASK_SECRETS_ENV,
    atomic_write_json,
    now_epoch,
    now_iso,
    read_json_or_none,
    restore_sensitive_config,
)
from runtime.model_router import set_call_context
from runtime.paths import harness_log_root, runtime_root, state_json_path
from runtime.redaction import redact_provider_text
from runtime.serialize import result_to_dict
from shared import tracing
from shared.types import Task, TaskResult

# --- bootstrap: make repo root importable (worker runs as __main__) ------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _state_completed_steps(task: Task, cfg: Dict[str, Any]) -> list:
    """Best-effort read of state.json's completed_steps (Boundary 4)."""
    state = read_json_or_none(state_json_path(task.task_id, cfg))
    if isinstance(state, dict):
        return list(state.get("completed_steps", []))
    return []


def _start_heartbeat(
    cp: TaskCheckpoint, stop: threading.Event, attempt_token: str
) -> None:
    def _beat() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                cp.beat({"attempt_token": attempt_token})
            except OSError:
                pass  # heartbeats are best-effort liveness, not correctness

    threading.Thread(target=_beat, daemon=True).start()


def _load_run_task(config: Dict[str, Any]):
    """Resolve the run_task callable (Boundary 3) for this worker.

    Real harness is required unless ``use_fake_harness`` explicitly pins
    the deterministic test implementation. Import failures propagate so a
    broken production harness can never masquerade as fake success.
    """
    if config.get("use_fake_harness", False):
        from runtime.fake_harness import run_task

        return run_task
    from harness.core import run_task

    return run_task


def _optional_float(config: Dict[str, Any], key: str) -> Optional[float]:
    """Read an optional float knob, treating absent/None/unusable as absent.

    Every budget, backoff, and reserve knob is OPTIONAL. "Absent" is a
    meaningful state throughout the governor: no cap means no cap, and no
    declared per-call bound means the pre-check reports ``unpriced`` rather
    than inventing a number.
    """
    if key not in config:
        return None
    value = config.get(key)
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # reject NaN


def _resolve_budget_cap_usd(config: Dict[str, Any]) -> Optional[float]:
    """Resolve the task's spend cap from its ONE documented home.

    `budget_cap_usd` is a `harness/config.py` DEFAULTS key, not a runtime
    one, because the harness is what enforces it. The worker's own
    `apply_defaults` merge therefore does not carry it, and reading only
    `config` would silently give the governor NO cap whenever the caller
    relied on the default — a governor with no cap cannot refuse anything,
    which is the failure mode this round exists to remove.

    So: an explicit `Task.config` value wins, and otherwise the harness's
    own default is read, defensively. `harness.config` is imported by
    name so a harness-less install degrades to "no cap" (the pre-round
    behaviour) rather than raising inside a worker.

    Returns ``None`` when no cap can be resolved, which reads as "no cap"
    and never as a zero budget.
    """
    explicit = _optional_float(config, "budget_cap_usd")
    if explicit is not None:
        return explicit
    try:
        from harness.config import get_config
    except ImportError:
        return None
    try:
        merged = get_config(dict(config))
    except Exception:
        return None
    return _optional_float(merged, "budget_cap_usd")


def build_governor(
    task: Task,
    cfg: Dict[str, Any],
    cp: TaskCheckpoint,
    receipt_path: Path,
) -> budget_governor.BudgetGovernor:
    """Build THIS task's budget governor and wire it to the checkpoint.

    The exemption writer reuses the approval-park mechanism rather than
    inventing a second one: it writes ``supervision_exemption`` into the
    SAME runtime checkpoint the scheduler already reads for
    ``awaiting_approval``, so the scheduler needs one exemption concept,
    not two. The approval park is re-expressed through it (with
    ``awaiting_approval`` still written, for the historical reader).

    Assumes ``cp`` is this attempt's checkpoint (so ``cp.update`` writes
    atomically over it) and ``receipt_path``'s parent exists or is
    creatable. Every callback is best-effort: a governor that cannot write
    a marker still governs the budget, it just cannot excuse itself from
    the watchdog, and the worker says so rather than dying.
    """
    failures: list[str] = []

    def mark_exemption(marker: Optional[Dict[str, Any]]) -> None:
        try:
            cp.update(supervision_exemption=marker)
        except OSError as exc:  # pragma: no cover - platform dependent
            failures.append(f"exemption marker unwritable: {exc}")

    def publish(receipt: Dict[str, Any]) -> None:
        try:
            budget_governor.write_budget_receipt(receipt_path, _ReceiptView(receipt))
        except (OSError, TypeError, ValueError) as exc:
            failures.append(f"budget receipt unwritable: {exc}")
        tracing.emit("runtime", "budget", task_id=task.task_id, **_safe_fields(receipt))

    def note_quota(failure: budget_governor.ProviderFailure) -> None:
        tracing.emit(
            "runtime",
            "quota_exhausted",
            task_id=task.task_id,
            kind=failure.kind,
            reason=failure.reason,
            billing_url=failure.billing_url,
            status_code=failure.status_code,
        )
        try:
            cp.log_event("quota_exhausted", failure.as_dict())
        except OSError:
            pass

    def emit(name: str, payload: Dict[str, Any]) -> None:
        tracing.emit("runtime", name, task_id=task.task_id, **payload)
        try:
            cp.log_event(name, payload)
        except OSError:
            pass

    governor = budget_governor.BudgetGovernor(
        task_id=task.task_id,
        cap_usd=_resolve_budget_cap_usd(cfg),
        max_wallclock_s=_optional_float(cfg, "max_wallclock_s"),
        started_epoch=now_epoch(),
        backoff_base_s=_optional_float(cfg, "rate_limit_backoff_s") or 15.0,
        # R2-14: the post-backoff grace is the WATCHDOG'S OWN staleness
        # window, read from the same config key the scheduler reads. A
        # worker whose backoff just expired still has to land the retried
        # call and write state; reusing the watchdog's number means the
        # exemption and the kill threshold can never disagree about how
        # long "no progress" is.
        backoff_grace_s=(
            _optional_float(cfg, "hang_heartbeat_stale_s")
            if "hang_heartbeat_stale_s" in cfg
            else float(DEFAULT_HANG_STALE_S)
        ),
        rate_limit_retries=int(cfg.get("rate_limit_retries", 4) or 0),
        reserve_per_call_usd=_optional_float(cfg, "budget_reserve_per_call_usd"),
        max_completion_tokens=(
            int(cfg["max_completion_tokens"])
            if isinstance(cfg.get("max_completion_tokens"), (int, float))
            and int(cfg["max_completion_tokens"]) > 0
            else None
        ),
        on_exemption=mark_exemption,
        on_receipt=publish,
        on_quota=note_quota,
        on_event=emit,
    )
    if failures:
        # Never silent: a governor that cannot reach the watchdog is
        # reported, so a reader can tell "no exemption needed" from
        # "exemption could not be recorded".
        emit("budget_governor_degraded", {"reasons": sorted(set(failures))})
    # Publish once immediately, so logs/{task_id}/budget.json exists from
    # the start of the run rather than appearing only after a charge.
    governor.publish()
    return governor


class _ReceiptView:
    """Adapter so :func:`write_budget_receipt` can persist a plain receipt.

    The governor hands its callback an already-built dict; writing it back
    through ``write_budget_receipt`` keeps ONE writer (the atomic
    tmp+replace primitive) instead of a second inline ``json.dump`` that
    could leave a torn file for a reader.
    """

    def __init__(self, receipt: Dict[str, Any]) -> None:
        self._receipt = receipt

    def report(self) -> Dict[str, Any]:
        """Return the receipt this view was built with."""
        return dict(self._receipt)


def _safe_fields(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Project a budget receipt onto scalar fields a trace row can carry."""
    quota = receipt.get("quota") or {}
    return {
        "cap_usd": receipt.get("cap_usd"),
        "spent_usd": receipt.get("spent_usd"),
        "reserved_usd": receipt.get("reserved_usd"),
        "remaining_usd": receipt.get("remaining_usd"),
        "exhausted": receipt.get("exhausted"),
        "calls_authorized": receipt.get("calls_authorized"),
        "calls_refused": receipt.get("calls_refused"),
        "quota_kind": quota.get("kind"),
    }


def _call_run_task(run_task_fn, task: Task, log_root: Path):
    """Call run_task, passing log_root when the callable accepts it.

    The real harness.core.run_task takes an optional log_root (INTERFACES
    Change Log 2026-09-07) so its logs/{task_id}/ lands inside the task's
    resume_dir — the same root the runtime reads state.json from. The fake
    harness (and any Boundary-3-pure callable) takes only (task), so we
    probe the signature once and call accordingly.
    """
    import inspect

    try:
        params = inspect.signature(run_task_fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "log_root" in params:
        return run_task_fn(task, log_root)
    return run_task_fn(task)


def _transient_task_payload() -> tuple[str, list[tuple[tuple[str, ...], Any]]]:
    """Remove and decode scheduler-only task text and credential transport."""
    raw = os.environ.pop(TASK_SECRETS_ENV, None)
    if not raw:
        return "", []
    try:
        payload = json.loads(raw)
        issue_text = payload.get("issue_text", "")
        entries = payload.get("secrets", [])
        if not isinstance(issue_text, str) or not isinstance(entries, list):
            raise ValueError
        normalized = []
        for entry in entries:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], list)
                or not all(isinstance(part, str) for part in entry[0])
            ):
                raise ValueError
            normalized.append((tuple(entry[0]), entry[1]))
        return issue_text, normalized
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid transient task payload") from exc


def run_worker(task_json_path: str, run_dir: str, attempt_token: str = "") -> int:
    """Worker main: run one task to completion (or its own demise).

    Assumes: task_json_path holds a JSON-serialized Task; run_dir is this
    attempt's private directory. ``attempt_token`` scopes heartbeats to
    this exact process launch. Returns the
    worker exit code. Never both writes a result AND crashes — a crash
    means no result.json, which the scheduler reads as "retry/resume".
    """
    with open(task_json_path, "r", encoding="utf-8") as f:
        task_dict = json.load(f)
    transient_issue, secret_entries = _transient_task_payload()
    raw_config = task_dict.get("config", {})
    if not isinstance(raw_config, dict):
        raise ValueError("task config must be an object")
    task = Task(
        task_id=str(task_dict["task_id"]),
        repo_path=str(task_dict.get("repo_path", "")),
        issue_text=transient_issue or str(task_dict.get("issue_text", "")),
        config=restore_sensitive_config(raw_config, secret_entries),
    )

    cfg = apply_defaults(task.config)
    task.config = cfg  # harness sees defaults too (max_retries etc.)

    # Runtime bookkeeping lives in logs/{task_id}.runtime/ (sibling of the
    # harness's logs/{task_id}/, which core._fresh_paths archives on every
    # relaunch — anything inside it would be swept away mid-task).
    runtime_dir = runtime_root(task.task_id, cfg)
    cp = TaskCheckpoint(str(runtime_dir))
    result_path = Path(run_dir) / "result.json"

    existing = cp.load()
    state_path = state_json_path(task.task_id, cfg)
    for directory in {cp.dir, state_path.parent}:
        for temporary in directory.glob(".*.tmp"):
            try:
                temporary.unlink()
            except OSError:
                pass
    state_completed = _state_completed_steps(task, cfg)
    identity = checkpoint_identity(task.task_id, task.repo_path, task.issue_text, cfg)
    if existing is not None and not checkpoint_identity_matches(existing, identity):
        cp.log_event("checkpoint_identity_mismatch", dict(identity))
        tracing.emit(
            "runtime",
            "checkpoint_identity_mismatch",
            task_id=task.task_id,
            checkpoint_identity=dict(identity),
        )

    if existing is not None:
        cfg.pop("fake_crash_step", None)
        cfg.pop("fake_hang_step", None)

    resuming = should_resume(cfg, existing, state_completed, identity=identity)
    if resuming:
        cfg["resume"] = True
    else:
        cfg["resume"] = False

    ledger_path = runtime_dir / "model_ledger.jsonl"
    if not resuming and ledger_path.exists():
        archived_ledger = runtime_dir / f"model_ledger.old-{time_ns()}.jsonl"
        os.replace(ledger_path, archived_ledger)
        cp.log_event("ledger_rotated", {"archive": archived_ledger.name})

    prior_checkpoint = existing if resuming else None
    attempt = int((prior_checkpoint or {}).get("attempt", -1)) + 1
    cp.log_event(
        "worker_start",
        {"task_id": task.task_id, "resume": resuming, "attempt": attempt},
    )
    tracing.emit(
        "runtime",
        "worker_start",
        task_id=task.task_id,
        resume=resuming,
        attempt=attempt,
    )
    cp.save(
        {
            "task_id": task.task_id,
            **identity,
            "attempt": attempt,
            "attempt_token": attempt_token,
            "started_at": (prior_checkpoint or {}).get("started_at", now_iso()),
            "last_heartbeat": now_iso(),
            "completed_steps": state_completed
            or (prior_checkpoint or {}).get("completed_steps", []),
            "result": None,
            "status": "running",
        }
    )
    cp.beat({"attempt_token": attempt_token})
    stop_event = threading.Event()
    _start_heartbeat(cp, stop_event, attempt_token)

    # R2-14: ONE budget/quota/deadline authority for this attempt. It is
    # installed before the router context so the dial boundary, the hang
    # watchdog, and harness.core's attempt-level check all read the same
    # numbers on the same clock. Every knob is optional: an absent cap is
    # "no cap", not a zero budget.
    governor = build_governor(
        task,
        cfg,
        cp,
        # `logs/{task_id}/budget.json` — the same directory as state.json
        # and trace.jsonl, because that is where a run is already visible.
        harness_log_root(cfg) / task.task_id / budget_governor.BUDGET_RECEIPT_NAME,
    )
    budget_governor.install_governor(governor)

    set_call_context(
        {
            "adaptive_routing": cfg.get("adaptive_routing", False),
            "model_tiers": cfg.get("model_tiers"),
            "difficulty_estimator": cfg.get("difficulty_estimator", "heuristic"),
            "difficulty_llm": cfg.get("difficulty_llm"),
            "provider_profile": cfg.get("provider_profile"),
            "model_prices": cfg.get("model_prices"),
            "provider": cfg.get("provider"),
            "model": cfg.get("model"),
            "api_key": cfg.get("api_key"),
            "api_base": cfg.get("api_base"),
            "use_mock_provider": cfg.get("use_mock_provider", False),
            "rate_limit_retries": cfg.get("rate_limit_retries", 4),
            "rate_limit_backoff_s": cfg.get("rate_limit_backoff_s", 15.0),
            # task_id rides the router context so per-call routing decisions can
            # land on the unified per-task trace stream (shared.tracing); the
            # router treats it as opaque passthrough — no routing semantics.
            "task_id": task.task_id,
            # Opt-in completion-token budget for the router (see
            # runtime/model_router.py call_model): reasoning-style endpoints
            # can exhaust an unbounded default on hidden reasoning tokens and
            # return no content. Set "max_completion_tokens" in Task.config to
            # enable; absent = endpoint default (previous behavior).
            "max_completion_tokens": cfg.get("max_completion_tokens"),
            # R2-14: the governor rides the context so the resilient dial
            # path prices each call before it is dialed. It is an OBJECT, not
            # a value, and the router treats it as opaque — no routing
            # semantics, and no new router code depends on it existing.
            "budget_governor": governor,
        },
        ledger_dir=str(runtime_dir / "model_ledger.jsonl"),
    )
    if cfg.get("use_mock_provider"):
        script_spec = cfg.get("mock_script")
        if script_spec:
            # Scripted harness-model driver (see runtime.mock_provider.
            # install_script): plan + per-step bash commands from a plain
            # dict, so the REAL harness loop runs deterministically with
            # zero network. Callables can't cross the process boundary —
            # the spec dict can.
            mock_provider.install_script(dict(script_spec))
        else:
            mock_provider.install(cfg.get("mock_responses") or {})

    run_task_fn = _load_run_task(cfg)
    quota_failure: Optional[budget_governor.ProviderFailure] = None
    try:
        result = _call_run_task(run_task_fn, task, harness_log_root(cfg))
    except budget_governor.QuotaExhausted as exc:
        # A quota wall ENDS the run. It is not a provider outage, so it is
        # not retried, not failed over, and not a crash: the worker reports
        # a real result rather than dying and being relaunched into the same
        # empty account. `TaskResult.status` stays inside the historical
        # four-value vocabulary (shared/types.py is another owner's file and
        # every consumer switches on it); the machine-readable verdict is
        # the checkpoint's `terminal_reason`, budget.json's `quota` block,
        # the `quota_exhausted` trace event, and the message itself.
        quota_failure = exc.failure
        result = TaskResult(
            task_id=task.task_id,
            status="error",
            attempts=attempt,
            diff=None,
            verification=None,
            cost_usd=round(governor.spent_usd(), 8),
            model_calls=[],
            log_path=str(harness_log_root(cfg) / "trace.jsonl"),
        )
    except budget_governor.BudgetRefused as exc:
        # The per-call pre-check refused to dial. The harness owns the
        # attempt-level backstop and normally catches this first; reaching
        # here means a caller dialled outside the harness loop, so it is
        # reported as a terminal budget outcome, not a crash.
        tracing.emit(
            "runtime",
            "budget_refused",
            task_id=task.task_id,
            **{k: v for k, v in exc.verdict.as_dict().items() if k != "cap_usd"},
        )
        cp.log_event("budget_refused", exc.verdict.as_dict())
        result = TaskResult(
            task_id=task.task_id,
            status="failed",
            attempts=attempt,
            diff=None,
            verification=None,
            cost_usd=round(governor.spent_usd(), 8),
            model_calls=[],
            log_path=str(harness_log_root(cfg) / "trace.jsonl"),
        )

    # -- approval gate (before the result is applied/reported) -----------
    # The gate can park the worker for a long human-scale time. The
    # harness isn't running, so state.json goes stale — which the
    # scheduler's hang check would misread as a hang and kill mid-gate.
    # The worker therefore records a SUPERVISION EXEMPTION in its
    # checkpoint (the same mechanism the provider backoff uses — one
    # concept, two reasons; the heartbeat daemon KEEPS beating, it stops
    # only after the gate). The marker is cleared in the FINAL checkpoint
    # write (atomically with status="finished") — a separate
    # finally-clear would reopen the race: marker=False + status=running
    # + stale state.json + not-yet-exited process = scheduler kill during
    # teardown. Heartbeat death or the wall-clock cap still kill it.
    if cfg.get("approval") == "require" and result.status == "success":
        gate_dir = str(runtime_dir / "approval")
        cp.log_event("approval_wait", {})
        tracing.emit("runtime", "approval_wait", task_id=task.task_id)
        cp.update(
            awaiting_approval=True,
            supervision_exemption={
                "reason": budget_governor.EXEMPTION_APPROVAL,
                # No deadline: the approval park is bounded by the
                # wall-clock cap, exactly as it was before this marker
                # existed. state_stale_exempt() reads the missing
                # until_epoch as "honour while the checkpoint is running".
                "until_epoch": None,
                "approval_timeout_s": cfg.get("approval_timeout_s"),
            },
        )
        try:
            approval_mod.request_approval(
                gate_dir=gate_dir,
                task_id=task.task_id,
                diff=result.diff or "",
                issue_text=task.issue_text,
                repo_path=task.repo_path,
                summary="Proposed fix for review",
                timeout_s=cfg.get("approval_timeout_s"),
            )
            cp.log_event("approval_granted", {})
            tracing.emit("runtime", "approval_granted", task_id=task.task_id)
        except approval_mod.ApprovalRejected as exc:
            # Redacted before BOTH the journal and the trace. The journal
            # (`checkpoint.log_event` -> `events.jsonl`) is not redacted at
            # the writer, so an unredacted value here is durable on disk --
            # and an approver's rejection reason is free text a human typed,
            # which is exactly the shape that can carry a pasted key.
            detail = _safe_exception_text(exc)
            cp.log_event("approval_rejected", {"error": detail})
            tracing.emit(
                "runtime", "approval_rejected", task_id=task.task_id, error=detail
            )
            result.status = "failed"
            result.diff = None
            result.verification = None
        except approval_mod.ApprovalTimeout as exc:
            detail = _safe_exception_text(exc)
            cp.log_event("approval_timeout", {"error": detail})
            tracing.emit(
                "runtime", "approval_timeout", task_id=task.task_id, error=detail
            )
            result.status = "timeout"
            result.diff = None
            result.verification = None

    stop_event.set()
    governor.reset_window()

    final_fields: Dict[str, Any] = {
        "last_heartbeat": now_iso(),
        "completed_steps": _state_completed_steps(task, cfg),
        "result": result_to_dict(result),
        "status": "finished",
        "awaiting_approval": False,  # atomically with status: no False+running
        # R2-14: the exemption is dropped in the SAME atomic write as
        # status="finished", for the same teardown race the approval marker
        # documents. A finished checkpoint is exempt anyway; carrying a live
        # backoff window past the end of the run would be a stale claim.
        "supervision_exemption": None,
    }
    if quota_failure is not None:
        # R2-14: the terminal reason travels on the checkpoint, so the
        # scheduler and any resume decision can tell "your credit is gone"
        # from "the model failed" without parsing a message.
        final_fields["terminal_reason"] = budget_governor.QUOTA_EXHAUSTED
        final_fields["quota"] = quota_failure.as_dict()
    cp.update(**final_fields)
    _emit(result_path, result)
    cp.log_event(
        "worker_finish",
        {
            "status": result.status,
            **(
                {"terminal_reason": budget_governor.QUOTA_EXHAUSTED}
                if quota_failure
                else {}
            ),
        },
    )
    tracing.emit(
        "runtime",
        "worker_finish",
        task_id=task.task_id,
        status=result.status,
        **(
            {
                "terminal_reason": budget_governor.QUOTA_EXHAUSTED,
                "billing_url": (quota_failure.billing_url if quota_failure else None),
            }
            if quota_failure
            else {}
        ),
    )
    set_call_context(None)
    budget_governor.clear_governor()
    mock_provider.reset()
    return 0


def _emit(result_path: Path, result: TaskResult) -> None:
    atomic_write_json(result_path, result_to_dict(result))


def _safe_exception_text(exc: BaseException) -> str:
    """Return bounded exception text with credentials removed.

    Delegates to :mod:`runtime.redaction` like the router's ``_safe_error``
    does. This used to be a SECOND redactor with its own regexes, and it was
    the weaker of the two in two ways that mattered:

    * it sliced to 500 characters BEFORE redacting, so a credential
      straddling the cut was no longer shape-matchable;
    * it had no URL-userinfo or query-string coverage -- the two most common
      shapes a provider SDK puts inside an auth error -- and could not mask a
      known secret VALUE because no caller had one to give it.

    It reached stderr only, so the divergence was latent rather than a live
    leak. It is now one call, and the next caller cannot reach a weaker
    sanitizer behind the runtime's back.
    """
    return redact_provider_text(exc, label="worker exception")


def main() -> int:
    parser = argparse.ArgumentParser(prog="runtime.worker")
    parser.add_argument("--task-json", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--attempt-token", default="")
    args = parser.parse_args()
    try:
        return run_worker(args.task_json, args.run_dir, args.attempt_token)
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"worker-level exception: {_safe_exception_text(exc)}", file=sys.stderr)
        return 1
    finally:
        set_call_context(None)
        budget_governor.clear_governor()
        mock_provider.reset()


if __name__ == "__main__":
    sys.exit(main())
