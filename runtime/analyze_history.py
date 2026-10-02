"""Cross-task history analysis — the aggregate usage-data job (Task A).

Reads ACCUMULATED task logs from across the whole logs/ tree and produces
aggregate statistics for three questions the per-run summaries never
answer:

1. Where did the difficulty predictor's estimates DIVERGE from actual
   outcomes? (predicted hint per task vs realized outcome: a "hard"
   prediction on a task the cheap tier solved first-try is a FALSE
   ESCALATION; an "easy" prediction on a task that needed retries is a
   missed escalation opportunity.)
2. Which retrieval strategies led to more/fewer repair attempts?
   (retrieval event strategy string vs attempts / repair-fire counts)
3. What are the common FAILURE patterns? (task_end status x reason,
   grouped across every run that has a trace.)

What it reads (ALL existing, documented formats — nothing new is written
by any producer; this module is a pure consumer):
  - logs/{task_id}/trace.jsonl  — harness trace (task_start carries
    issue_text + config; task_end carries status; result carries the
    final status; retrieval carries strategy; verify events carry
    pass/fail; attempt_start count = repair attempts)
  - logs/{task_id}.runtime/model_ledger.jsonl — per-call routing ledger
    (model, difficulty_hint, routed_via_hint, cost, tokens)
  - ablation summaries: logs/ablations/*/summary.json (arm-level
    per_task records incl. the ensemble arm's task-level predicted hint
    + features)

What it deliberately EXCLUDES (scripted-model runs — the scripted model
makes outcomes about the script, not about routing/difficulty):
  - logs/evals/** (every eval arm runs a scripted model by design)
  - tasks whose task_start config has use_mock_provider/mock_script
  - smoke/stress/soak/abuse harness runs (fake harness by design)

The job is READ-ONLY over the logs tree and writes ONE report JSON to
logs/analyze-history/<ts>/report.json. Never raises on a malformed
individual log (skips it with a note) — an aggregation over hundreds of
heterogeneous dirs must survive any one of them being truncated.

Run:  python -m runtime.analyze_history  (or: neo analyze-history)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from runtime.fsutil import atomic_write_json

# Directories under logs/ that are run-group containers (task dirs may
# nest inside them, e.g. ablations/<run>/tasklogs/{task_id}/) or known
# non-task state. Everything else that HOLDS a trace.jsonl or a
# state.json is treated as a task dir.
_SKIP_DIRS = {
    "pristine",
    "work",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "node_modules",
    ".runtime",
    "_code-graph",
    "_docs-cache",
    "agent_tests",
    "_trace",
}

# Archived task dirs: the harness's _fresh_paths renames prior runs to
# {task_id}.old-<ts>/ and build_mode stages {task_id}.base/ siblings.
# They are EARLIER ATTEMPTS of the same task (their traces are strict
# prefixes of the live dir's trace at best, stale partial data at
# worst) — scanning them double-counts tasks (measured: 77 duplicated
# task_ids on the real tree before this filter).
_ARCHIVE_RE = re.compile(r"\.old-\d{8}-\d{6}$|\.base$")


def _is_archive_dir(name: str) -> bool:
    """Return whether a directory matches the producer's archive grammar."""
    return bool(_ARCHIVE_RE.search(name))


# Runs whose task logs are scripted-model (fake harness / eval arms /
# smoke suites): their outcome data measures the SCRIPT, not routing.
# Matching is on path SEGMENTS (any depth), so ablations/<run>/tasklogs/
# is fine but evals/<ts>/<arm>/<slug>/<slug>/ is excluded wholesale.
_SCRIPTED_SEGMENTS = {
    "evals",
    "stress",
    "soak",
    "abuse",
    "dod",
    "sandbox-stress",
    "sandbox-perf",
    "sandbox-sustained",
    "memplan-pilot",
    "demo",
    "demo-work",
}


def _is_scripted(rel_parts: tuple, cfg: Dict[str, Any]) -> bool:
    """True when this task's outcome was scripted, not model-driven."""
    if any(seg in _SCRIPTED_SEGMENTS for seg in rel_parts):
        return True
    if cfg.get("use_mock_provider") or cfg.get("mock_script") is not None:
        return True
    return bool(cfg.get("use_fake_harness"))


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|credential)"
)
_SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key|token|password|authorization)\s*[:=]\s*[^\s,;]+"),
)


def _collect_secret_values(value: Any) -> tuple[str, ...]:
    """Collect sensitive scalar values from nested configuration data."""
    found: list[str] = []

    def visit(node: Any, sensitive: bool = False) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, bool(_SENSITIVE_KEY_RE.search(str(key))))
        elif isinstance(node, list):
            for child in node:
                visit(child, sensitive)
        elif sensitive and node is not None:
            text = str(node)
            if len(text) >= 4:
                found.append(text)

    visit(value)
    return tuple(dict.fromkeys(found))


def _safe_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """Redact known credentials and common credential formats from text."""
    text = str(value or "")
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub("[REDACTED]", text)
    return text


def _repo_key(repo: Any) -> Optional[str]:
    """Return a deterministic non-path repository identity for grouping."""
    if not isinstance(repo, str) or not repo.strip():
        return None
    try:
        normalized = os.path.normcase(str(Path(repo).resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iter_trace(p: Path) -> Iterable[Dict[str, Any]]:
    """Yield trace events from a trace.jsonl; malformed lines skipped."""
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict) and "kind" in ev:
                        yield ev
                except ValueError:
                    continue
    except OSError:
        return


def _nonnegative_number(value: Any) -> Optional[float]:
    """Return a finite non-negative number, or None for invalid input."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _load_ledger(
    task_dir: Path, diagnostics: Optional[Dict[str, int]] = None
) -> List[Dict[str, Any]]:
    """Load valid per-call ledger rows, preserving valid rows around corruption."""
    runtime_dir = task_dir.with_name(task_dir.name + ".runtime")
    path = runtime_dir / "model_ledger.jsonl"
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if diagnostics is not None:
                diagnostics["ledger_invalid"] = diagnostics.get("ledger_invalid", 0) + 1
            continue
        tokens = (
            _nonnegative_number(record.get("tokens"))
            if isinstance(record, dict)
            else None
        )
        cost = (
            _nonnegative_number(record.get("cost_usd", 0.0))
            if isinstance(record, dict)
            else None
        )
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("model"), str)
            or not record.get("model")
            or tokens is None
            or cost is None
        ):
            if diagnostics is not None:
                diagnostics["ledger_invalid"] = diagnostics.get("ledger_invalid", 0) + 1
            continue
        normalized = dict(record)
        normalized["tokens"] = int(tokens)
        normalized["cost_usd"] = cost
        records.append(normalized)
    return records


_VALID_STATUSES = {"success", "failed", "error", "timeout"}


def _validated_lifecycle(
    trace: List[Dict[str, Any]],
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]:
    """Return validated start/outcome data plus an exclusion reason."""
    starts: list[tuple[int, Dict[str, Any]]] = []
    ends: list[tuple[int, Dict[str, Any]]] = []
    results: list[Dict[str, Any]] = []
    mode_signals: list[str] = []
    for index, event in enumerate(trace):
        data = event.get("data")
        if not isinstance(data, dict):
            return None, None, "malformed"
        kind = event.get("kind")
        if kind == "task_start":
            if not isinstance(data.get("config", {}), dict):
                return None, None, "malformed"
            starts.append((index, data))
            mode = data.get("config", {}).get("mode", data.get("mode"))
            if mode is not None:
                mode_signals.append(str(mode))
        elif kind == "mode":
            mode = data.get("mode")
            if mode is not None:
                mode_signals.append(str(mode))
        elif kind == "task_end":
            ends.append((index, data))
            if data.get("mode") is not None:
                mode_signals.append(str(data["mode"]))
        elif kind == "result":
            results.append(data)
    if not starts:
        return None, None, "incomplete"
    if mode_signals and (len(set(mode_signals)) != 1 or mode_signals[0] != "fix"):
        return None, None, "non_fix"
    if not ends:
        return None, None, "incomplete"
    end_index, end_data = ends[-1]
    start_index, start_data = max(
        (item for item in starts if item[0] < end_index),
        default=starts[-1],
        key=lambda item: item[0],
    )
    status = end_data.get("status")
    if status not in _VALID_STATUSES:
        return None, None, "incomplete"
    attempts = end_data.get("attempts")
    if attempts is None:
        attempts = sum(
            1
            for event in trace[start_index:end_index]
            if event.get("kind") == "attempt_start"
        )
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return None, None, "malformed"
    for result in results:
        result_status = result.get("status")
        if result_status is not None and result_status != status:
            return None, None, "malformed"
        result_attempts = result.get("attempts")
        if result_attempts is not None and result_attempts != attempts:
            return None, None, "malformed"
    return (
        start_data,
        {
            "status": status,
            "attempts": attempts,
            "reason": end_data.get("reason"),
        },
        None,
    )


def _count_repairs(trace: List[Dict[str, Any]]) -> int:
    """Repair fires = extra attempts beyond the first (attempt_start
    events are 1-indexed in the harness trace, so attempt >= 2 means a
    verify/refusal-gated retry) PLUS in-session step retries that never
    reached a new attempt (verify events with target_passed=false
    followed by more model work in the same attempt — counted as the
    re-plan turn, i.e. final_verify/verify failures that led to an
    additional attempt_start are NOT double-counted: the attempt_start
    already carries them)."""
    repairs = 0
    for ev in trace:
        if ev["kind"] == "attempt_start":
            attempt = ev.get("data", {}).get("attempt")
            if isinstance(attempt, int) and attempt >= 2:
                repairs += 1
    return repairs


def _failure_bucket(status: Optional[str], reason: Optional[str]) -> str:
    """Coarse failure taxonomy for the common-patterns question."""
    if status == "success":
        return "success"
    if status == "timeout":
        return "timeout (wallclock/hang)"
    if status == "error":
        r = (reason or "").lower()
        if "planner" in r or "model" in r or "litellm" in r or "api" in r:
            return "error: model/endpoint"
        if "budget" in r:
            return "error: budget cap"
        return "error: other"
    if status == "failed":
        return "failed (verifier refused after retries)"
    return "unknown/incomplete"


def summarize_task(
    task_dir: Path,
    rel_parts: tuple,
    logs_root: Path,
    diagnostics: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Summarize one complete fix-mode lifecycle without exposing raw secrets."""
    trace_path = task_dir / "trace.jsonl"
    if not trace_path.exists():
        return None
    trace = list(_iter_trace(trace_path))
    if not trace:
        if diagnostics is not None:
            diagnostics["incomplete"] = diagnostics.get("incomplete", 0) + 1
        return None
    start, outcome, exclusion = _validated_lifecycle(trace)
    if exclusion is not None or start is None or outcome is None:
        if diagnostics is not None:
            diagnostics[exclusion or "malformed"] = (
                diagnostics.get(exclusion or "malformed", 0) + 1
            )
        return None
    config = start.get("config") or {}
    if _is_scripted(rel_parts, config) or start.get("model_source") == "scripted":
        if diagnostics is not None:
            diagnostics["scripted"] = diagnostics.get("scripted", 0) + 1
        return None
    issue = start.get("issue_text", "")
    if not isinstance(issue, str):
        if diagnostics is not None:
            diagnostics["malformed"] = diagnostics.get("malformed", 0) + 1
        return None
    secrets = _collect_secret_values(config)
    repairs = _count_repairs(trace)
    retrieval = next(
        (event for event in trace if event.get("kind") == "retrieval"), None
    )
    retrieval_data = retrieval.get("data") if retrieval else None
    strategy = (
        retrieval_data.get("strategy") if isinstance(retrieval_data, dict) else None
    )
    if strategy is not None and not isinstance(strategy, str):
        strategy = None

    ledger = _load_ledger(task_dir, diagnostics)
    hints: Dict[str, int] = {}
    models: Dict[str, int] = {}
    cost = 0.0
    tokens = 0
    planner_hint = None
    routed_calls = 0
    for record in ledger:
        model = _safe_text(record["model"], secrets)
        models[model] = models.get(model, 0) + 1
        hint = record.get("difficulty_hint")
        if hint in {"easy", "medium", "hard"}:
            hints[hint] = hints.get(hint, 0) + 1
        cost += float(record["cost_usd"])
        tokens += int(record["tokens"])
        route = record.get("routed_via_hint")
        if route:
            routed_calls += 1
        if planner_hint is None:
            planner_hint = route or hint

    predicted_hint = None
    predicted_score = None
    if issue:
        try:
            from runtime.ensemble import predict_task_difficulty

            predicted_hint, info = predict_task_difficulty(issue)
            predicted_score = info.get("features", {}).get("score")
        except Exception:
            predicted_hint = None

    task_id = start.get("task_id") or task_dir.name
    reason = _safe_text(outcome.get("reason"), secrets)
    repo = start.get("repo_path")
    return {
        "task_id": _safe_text(task_id, secrets),
        "rel_dir": _safe_text("/".join(rel_parts), secrets),
        "repo": _safe_text(repo, secrets) if isinstance(repo, str) else None,
        "repo_key": _repo_key(repo),
        "issue_chars": len(issue),
        "status": outcome["status"],
        "attempts": outcome["attempts"],
        "attempts_raw": sum(
            1 for event in trace if event.get("kind") == "attempt_start"
        ),
        "repairs": repairs,
        "failure_bucket": _failure_bucket(outcome["status"], reason),
        "reason": reason,
        "retrieval_strategy": _safe_text(strategy, secrets) if strategy else None,
        "calls": len(ledger),
        "cost_usd": round(cost, 6),
        "tokens": tokens,
        "models": models,
        "difficulty_hints": hints,
        "planner_routed_via": planner_hint,
        "routed": routed_calls > 0,
        "predicted_hint": predicted_hint,
        "predicted_score": predicted_score,
    }


def scan_tasks(
    logs_root: Path, diagnostics: Optional[Dict[str, int]] = None
) -> List[Dict[str, Any]]:
    """Recursively scan complete real fix-mode traces in deterministic order."""
    records: List[Dict[str, Any]] = []
    counters = diagnostics if diagnostics is not None else {}
    root = logs_root.resolve()
    for dirpath, _dirnames, filenames in os_walk_pruned(root):
        if "trace.jsonl" not in filenames:
            continue
        counters["traces_seen"] = counters.get("traces_seen", 0) + 1
        task_dir = Path(dirpath)
        relative = task_dir.relative_to(root)
        try:
            record = summarize_task(task_dir, relative.parts, root, counters)
        except Exception:
            counters["malformed"] = counters.get("malformed", 0) + 1
            continue
        if record is not None:
            records.append(record)
            counters["accepted"] = counters.get("accepted", 0) + 1
    records.sort(key=lambda item: (str(item.get("rel_dir", "")), item["task_id"]))
    return records


def os_walk_pruned(root: Path):
    """Walk sorted directories while pruning generated and archived trees."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            directory
            for directory in dirnames
            if directory not in _SKIP_DIRS and not _is_archive_dir(directory)
        )
        yield dirpath, dirnames, filenames


# ---------------------------------------------------------------------------
# Task A aggregates: divergence, retrieval-vs-repairs, failure patterns
# ---------------------------------------------------------------------------

HINT_ORDER = {"easy": 0, "medium": 1, "hard": 2}

# Task-id prefixes that carry the bug slug as their LAST segment
# (abl-on-<slug>, ens-a-<slug>, ens-c1/c2/x-<slug>): the same BUG
# re-run across ablation windows (v2/v3/v4/ir2...) is ONE underlying
# observation, not several — collapse by bug for the per-bug view.
_ABL_PREFIXES = ("abl-on-", "ens-a-", "ens-c1-", "ens-c2-", "ens-x-")


def bug_key(task_id: str) -> str:
    """Return the stable bug slug after removing a known arm prefix."""
    for prefix in _ABL_PREFIXES:
        if task_id.startswith(prefix):
            return task_id[len(prefix) :]
    return task_id


def _observation_group(row: Dict[str, Any]) -> str:
    """Return a repo-scoped group key for one calibration observation."""
    bug = bug_key(str(row.get("task_id", "")))
    repo_key = row.get("repo_key")
    return f"{repo_key}:{bug}" if repo_key else bug


def _divergence(rec: Dict[str, Any]) -> Optional[str]:
    """Classify predictor-vs-outcome divergence for ONE task.

    predicted_hint is the task-level score from the issue text (equals
    the router's planner-call decision). realized signal = status +
    attempts + repairs:
      - hard-predicted AND solved on attempt 1 with zero repairs and
        all-cheap models -> FALSE ESCALATION (hard look, easy reality)
      - easy-predicted AND (failed OR needed 2+ attempts) -> MISSED
        ESCALATION (easy look, hard reality)
      - else -> aligned
    """
    if rec.get("predicted_hint") is None:
        return None
    ph = rec["predicted_hint"]
    status = rec.get("status")
    attempts = rec.get("attempts")
    repairs = rec.get("repairs")
    if ph == "hard":
        if status == "success" and (attempts or 0) <= 1 and (repairs or 0) == 0:
            return "false_escalation"
        return "hard_aligned_or_ambiguous"
    if ph in ("easy", "medium"):
        if status in ("failed", "timeout", "error"):
            return "missed_escalation"
        if (attempts or 0) >= 2 or (repairs or 0) >= 2:
            return "missed_escalation"
        return "aligned"
    return None


def aggregate(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The three Task-A aggregates over the per-task records."""
    # --- 1. predictor divergence ---------------------------------------
    # Only ROUTED tasks (prediction actually drove the tier choice)
    # carry divergence semantics; pinned/off-arm tasks are reported
    # separately as context, not scored against the predictor.
    div: Dict[str, Dict[str, Any]] = {}
    routed = [t for t in tasks if t.get("routed")]
    with_pred = [t for t in routed if t.get("predicted_hint")]
    pinned = [t for t in tasks if not t.get("routed")]
    for cls in (
        "aligned",
        "false_escalation",
        "missed_escalation",
        "hard_aligned_or_ambiguous",
    ):
        members = [t for t in with_pred if _divergence(t) == cls]
        div[cls] = {
            "n": len(members),
            "task_ids": [_safe_text(t["task_id"]) for t in members][:50],
            "mean_cost_usd": _mean([t["cost_usd"] for t in members]),
            "mean_attempts": _mean([t["attempts"] or 0 for t in members]),
        }
    # hard-predicted overall
    hard_pred = [t for t in with_pred if t["predicted_hint"] == "hard"]
    div["hard_predicted"] = {
        "n": len(hard_pred),
        "mean_cost_usd": _mean([t["cost_usd"] for t in hard_pred]),
        "mean_attempts": _mean([t["attempts"] or 0 for t in hard_pred]),
    }
    div["n_routed_tasks"] = len(routed)
    div["n_pinned_tasks_not_scored"] = len(pinned)

    # --- 2. retrieval strategy vs repairs/attempts ---------------------
    # Group by coarse strategy family: structural+grep vs grep-only vs
    # other/None. HONEST CAVEAT carried in the report: this is
    # OBSERVATIONAL, not causal — strategy correlates with repo shape
    # (structural needs an index; tiny fixture repos fall back to grep),
    # and error-died tasks skew the "other/none" row (early deaths ->
    # fewer attempts). Read as association, not a controlled comparison.
    strat: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        s = t.get("retrieval_strategy") or "none"
        if "structural" in s:
            key = "structural+grep"
        elif s.startswith("grep"):
            key = "grep-only"
        else:
            key = "other/none"
        bucket = strat.setdefault(
            key, {"n": 0, "attempts": [], "repairs": [], "costs": [], "statuses": {}}
        )
        bucket["n"] += 1
        bucket["attempts"].append(t["attempts"] or 0)
        bucket["repairs"].append(t["repairs"] or 0)
        bucket["costs"].append(t["cost_usd"])
        st = t.get("status") or "unknown"
        bucket["statuses"][st] = bucket["statuses"].get(st, 0) + 1
    for key, b in strat.items():
        strat[key] = {
            "n": b["n"],
            "mean_attempts": _mean(b["attempts"]),
            "mean_repairs": _mean(b["repairs"]),
            "mean_cost_usd": _mean(b["costs"]),
            "statuses": b["statuses"],
        }

    # --- 3. failure patterns -------------------------------------------
    fails: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        b = t.get("failure_bucket") or "unknown/incomplete"
        f = fails.setdefault(b, {"n": 0, "task_ids": [], "reasons": {}})
        f["n"] += 1
        if b != "success":
            f["task_ids"].append(_safe_text(t["task_id"]))
            r = _safe_text(t.get("reason"))[:120]
            if r:
                f["reasons"][r] = f["reasons"].get(r, 0) + 1
    for f in fails.values():
        f["task_ids"] = f["task_ids"][:50]

    return {
        "n_tasks": len(tasks),
        "predictor_divergence": div,
        "retrieval_vs_repairs": strat,
        "failure_patterns": fails,
    }


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


# ---------------------------------------------------------------------------
# Task B: calibration dataset build + hold-out evaluation
# ---------------------------------------------------------------------------


def calibration_rows(
    tasks: List[Dict[str, Any]], label_policy: str = "strict"
) -> List[Dict[str, Any]]:
    """Training rows for the offline recalibration.

    One row per REAL ADAPTIVE-ROUTING task with a prediction + outcome.
    OFF-arm tasks (model pinned) are excluded: their outcomes measure
    the expensive tier, not the predictor's routing decision — including
    them would let endpoint-latency deaths masquerade as difficulty
    signal.

    Label design (the honest option, "strict" policy — the default):
    the target is NOT the observed hint (self-confirming) and NOT any
    struggle (a cheap attempt-2 retry is a documented economic WASH vs
    an expensive planner call — the Improvement-Round-2 ensemble
    ablation measured 2x-cheap-candidates ~= 1x-expensive-call at these
    tiers, so "needed a retry" is NOT evidence the predictor should
    have escalated). The only case where earlier escalation could
    plausibly have changed the outcome is a verifier-refused FAILURE
    on the routed-cheap tier:
      hard  = status "failed" (verifier refused the fix after the full
              attempt budget burned on cheap)
      easy  = any success (the cheap tier finished it, first or second
              attempt — both cheaper than or equal to escalating)
    error/timeout tasks are EXCLUDED either way (endpoint deaths are
    evidence about the endpoint, not the bug — the v1 rate-limit-
    artifact lesson applied in reverse).

    The "struggle" policy (hard = any second attempt or failure) is
    kept for the Task-A DIVERGENCE description — predicting difficulty
    of the repair process, not routing economics — but is NOT the
    calibration target, for the wash reason above.
    """
    rows: List[Dict[str, Any]] = []
    for t in tasks:
        if not t.get("predicted_hint"):
            continue
        if not t.get("repo_key"):
            continue
        if t.get("status") is None:
            continue
        # adaptive-routing tasks only (see docstring): the planner call
        # was routed BY the prediction. OFF-arm/pinned tasks are marked
        # by an empty routed_via_hint ledger (pinned calls never route).
        if t.get("routed") is False:
            continue
        attempts = t.get("attempts") or 0
        repairs = t.get("repairs") or 0
        status = t["status"]
        if status == "failed":
            label = "hard"
        elif status == "success":
            if label_policy == "struggle":
                label = "easy" if (attempts <= 1 and repairs == 0) else "hard"
            else:
                label = "easy"
        else:
            # error/timeout: endpoint/machinery death — no difficulty
            # signal UNLESS the struggle policy is explicitly requested
            # and the run also shows genuine struggle first.
            if label_policy == "struggle" and (attempts >= 2 or repairs >= 1):
                label = "hard"
            else:
                continue
        group_id = _observation_group(t)
        rows.append(
            {
                "task_id": t["task_id"],
                "bug_id": bug_key(t["task_id"]),
                "repo_key": t["repo_key"],
                "group_id": group_id,
                "predicted_hint": t["predicted_hint"],
                "predicted_score": t.get("predicted_score"),
                "predicted_hint_num": HINT_ORDER.get(t["predicted_hint"]),
                "label": label,
                "status": t["status"],
                "attempts": attempts,
                "repairs": repairs,
            }
        )
    return rows


def evaluate(rows: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    """Predictor quality on a row set, using predicted_hint vs label
    (hard-labeled rows predicted easy/medium = missed escalations;
    easy-labeled rows predicted hard = false escalations). Reports BOTH
    the per-run and the per-bug (deduped) view — the same bug re-run
    across ablation windows is one underlying observation."""
    if not rows:
        return {"name": name, "n": 0}
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_group.setdefault(_observation_group(row), []).append(row)
    per_bug_rows = []
    for group, group_rows in sorted(by_group.items()):
        ordered = sorted(
            group_rows,
            key=lambda row: (
                str(row.get("task_id", "")),
                str(row.get("label", "")),
                str(row.get("predicted_hint", "")),
            ),
        )
        representative = ordered[0]
        per_bug_rows.append(
            {
                "task_id": group,
                "group_id": group,
                "predicted_hint": representative["predicted_hint"],
                "predicted_hint_num": representative["predicted_hint_num"],
                "predicted_score": representative.get("predicted_score"),
                "label": (
                    "hard" if any(row["label"] == "hard" for row in ordered) else "easy"
                ),
            }
        )

    def _score(rs):
        missed = [
            r
            for r in rs
            if r["label"] == "hard" and r["predicted_hint"] in ("easy", "medium")
        ]
        false_e = [
            r for r in rs if r["label"] == "easy" and r["predicted_hint"] == "hard"
        ]
        correct = [
            r
            for r in rs
            if (
                (r["label"] == "easy" and r["predicted_hint"] in ("easy", "medium"))
                or (r["label"] == "hard" and r["predicted_hint"] == "hard")
            )
        ]
        err = [
            abs(
                r["predicted_hint_num"]
                - HINT_ORDER["hard" if r["label"] == "hard" else "easy"]
            )
            for r in rs
            if r["predicted_hint_num"] is not None
        ]
        return {
            "n": len(rs),
            "accuracy_easy_or_hard": round(len(correct) / len(rs), 4),
            "missed_escalations": len(missed),
            "false_escalations": len(false_e),
            "missed_task_ids": [r["task_id"] for r in missed][:50],
            "false_task_ids": [r["task_id"] for r in false_e][:50],
            "mean_ordinal_error": _mean(err),
        }

    out = {"name": name}
    out.update(_score(rows))
    out["per_bug"] = _score(per_bug_rows)
    return out


def split_holdout(
    rows: List[Dict[str, Any]], frac: float = 0.25, seed: int = 7
) -> tuple:
    """Split whole repo-scoped bug groups into deterministic train/holdout sets."""
    if not math.isfinite(float(frac)) or not 0 < float(frac) < 1:
        raise ValueError("holdout fraction must be between 0 and 1")
    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_group.setdefault(_observation_group(row), []).append(row)
    if not by_group:
        return [], []
    if len(by_group) == 1:
        group_rows = next(iter(by_group.values()))
        return sorted(group_rows, key=lambda row: str(row.get("task_id", ""))), []

    ranked = sorted(
        by_group,
        key=lambda group: hashlib.sha256(
            f"{int(seed)}:{group}".encode("utf-8")
        ).hexdigest(),
    )
    holdout_count = max(1, min(len(ranked) - 1, round(len(ranked) * float(frac))))
    held_groups = set(ranked[:holdout_count])

    def ordered_for(groups: set[str]) -> List[Dict[str, Any]]:
        selected = [
            row
            for group, group_rows in by_group.items()
            if group in groups
            for row in group_rows
        ]
        return sorted(
            selected,
            key=lambda row: (
                _observation_group(row),
                str(row.get("task_id", "")),
                str(row.get("label", "")),
            ),
        )

    return ordered_for(set(by_group) - held_groups), ordered_for(held_groups)


def recalibrate(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Offline threshold recalibration of score_to_hint's bands.

    The v2 predictor's score bands (<=1 easy, <=3 medium, else hard)
    were calibrated by hand on 5 fixture bugs (Round 2). This job fits
    new thresholds by maximizing agreement with the REALIZED-difficulty
    labels: a grid search over (easy_max, hard_min) integer bands —
    the same 2-parameter family, no new features (recalibration, not
    re-architecture). Duplicate bug observations are collapsed by the
    CALLER (per-bug rows) so re-run windows don't overweight a bug.

    Assumes rows carry predicted_score (int) + label ("easy"|"hard").
    Returns the fitted bands + the train-split error decomposition. The
    bands are NOT auto-applied anywhere — applying them is a
    deliberate human-reviewed maintenance step (see write_difficulty_
    calibration / runtime.difficulty.apply_calibration_file).
    """
    train = [r for r in rows if r.get("predicted_score") is not None]
    if len(train) < 4:
        return {"status": "insufficient_data", "n": len(train)}

    def miss_rate(bands):
        """weighted error: false escalation cost + missed escalation
        cost (both 1 per task; weights configurable in future)."""
        fe, me = 0, 0
        for r in train:
            s = int(r["predicted_score"])
            hint = "easy" if s <= bands[0] else ("medium" if s < bands[1] else "hard")
            if r["label"] == "easy" and hint == "hard":
                fe += 1
            elif r["label"] == "hard" and hint in ("easy", "medium"):
                me += 1
        return fe, me

    best = None  # (fe+me, fe, me, bands)
    for easy_max in range(0, 5):
        for hard_min in range(easy_max + 1, 7):
            fe, me = miss_rate((easy_max, hard_min))
            cand = (fe + me, fe, me, (easy_max, hard_min))
            if best is None or cand < best:
                best = cand
    total_err, fe, me, bands = best
    return {
        "status": "ok",
        "n_train": len(train),
        "bands": {"easy_max": bands[0], "hard_min": bands[1]},
        "train_false_escalations": fe,
        "train_missed_escalations": me,
        "train_error_rate": round(total_err / len(train), 4),
    }


def apply_bands(score: int, bands: Dict[str, int]) -> str:
    """Hint under fitted bands (mirror of difficulty.score_to_hint)."""
    if score <= bands["easy_max"]:
        return "easy"
    if score < bands["hard_min"]:
        return "medium"
    return "hard"


def build_report(
    logs_root: Path,
    holdout_frac: float = 0.25,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Full pipeline: scan -> aggregate -> calibrate -> report dict.

    Assumes logs_root is the logs/ tree root. Writes report.json under
    logs/analyze-history/<ts>/ (or out_dir) and returns the dict. The
    before/after evaluation is per-BUG (deduped) on BOTH sides of the
    split so re-run windows can't overweight one bug.
    """
    if not math.isfinite(float(holdout_frac)) or not 0 < float(holdout_frac) < 1:
        raise ValueError("holdout fraction must be between 0 and 1")
    diagnostics: Dict[str, int] = {}
    tasks = scan_tasks(logs_root, diagnostics)
    agg = aggregate(tasks)
    rows = calibration_rows(tasks)  # strict policy (routing economics)
    train, held = split_holdout(rows, frac=holdout_frac)

    # Per-bug training rows (deduped): calibration fits these so a bug
    # re-run across ablation windows is ONE observation, not a weight.
    def _dedup(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_group: Dict[str, Dict[str, Any]] = {}
        for row in sorted(
            rows,
            key=lambda item: (
                _observation_group(item),
                str(item.get("task_id", "")),
                str(item.get("label", "")),
            ),
        ):
            group = _observation_group(row)
            existing = by_group.get(group)
            if existing is None:
                by_group[group] = dict(row)
                continue
            if row.get("label") == "hard" and existing.get("label") != "hard":
                by_group[group].update(row)
        for group, row in by_group.items():
            row["group_id"] = group
            row["task_id"] = group
        return sorted(by_group.values(), key=lambda row: row["group_id"])

    train_bugs = _dedup(train)
    held_bugs = _dedup(held)

    cal = recalibrate(train_bugs)
    before_held = evaluate(held_bugs, "before (v2 bands, held-out per bug)")
    after_held = evaluate(held_bugs, "after (recalibrated bands, held-out per bug)")
    before_train = evaluate(train_bugs, "before (v2 bands, train per bug)")
    after_train = evaluate(train_bugs, "after (recalibrated bands, train per bug)")
    if cal.get("status") == "ok":
        bands = cal["bands"]
        for r in held_bugs + train_bugs:
            if r.get("predicted_score") is not None:
                r["recalibrated_hint"] = apply_bands(int(r["predicted_score"]), bands)
        held_after = [
            {
                **r,
                "predicted_hint": r.get("recalibrated_hint") or r["predicted_hint"],
                "predicted_hint_num": HINT_ORDER.get(r.get("recalibrated_hint")),
            }
            for r in held_bugs
        ]
        train_after = [
            {
                **r,
                "predicted_hint": r.get("recalibrated_hint") or r["predicted_hint"],
                "predicted_hint_num": HINT_ORDER.get(r.get("recalibrated_hint")),
            }
            for r in train_bugs
        ]
        after_held = evaluate(
            held_after, "after (recalibrated bands, held-out per bug)"
        )
        after_train = evaluate(train_after, "after (recalibrated bands, train per bug)")

    # Honest recommendation: apply only when the HELD-OUT numbers (not
    # the train numbers the bands were fitted on) actually improve.
    rec = "insufficient_data"
    if cal.get("status") == "ok" and held_bugs:
        b = before_held["accuracy_easy_or_hard"] or 0.0
        a = after_held["accuracy_easy_or_hard"] or 0.0
        if a > b:
            rec = "apply"
        elif a == b:
            rec = "marginal — not worth applying (held-out delta zero)"
        else:
            rec = "reject (held-out got worse)"
    report = {
        "ts": time.strftime("%Y%m%d-%H%M%S"),
        "logs_root": _safe_text(logs_root),
        "n_real_tasks": len(tasks),
        "scan_diagnostics": dict(sorted(diagnostics.items())),
        "n_routed_tasks": agg["predictor_divergence"]["n_routed_tasks"],
        "aggregate": agg,
        "calibration": cal,
        "heldout_frac": holdout_frac,
        "n_train_rows": len(train),
        "n_holdout_rows": len(held),
        "n_train_bugs": len(train_bugs),
        "n_holdout_bugs": len(held_bugs),
        "before_heldout": before_held,
        "after_heldout": after_held,
        "before_train": before_train,
        "after_train": after_train,
        "recommendation": rec,
        "data_honesty_notes": [
            "Rows come only from REAL-model adaptive-routing tasks (scripted/fake-harness/eval runs and OFF-arm pinned tasks excluded — pinned outcomes measure the endpoint, not the predictor).",
            "Labels are realized difficulty (outcome-derived), not observed hints — avoids the self-confirming trap of scoring the predictor against itself.",
            "TWO difficulty semantics, deliberately: the divergence aggregate describes the REPAIR PROCESS (any second attempt = struggle); the calibration labels target ROUTING ECONOMICS (only a verifier-refused failure on the cheap tier means earlier escalation could plausibly have helped — a cheap attempt-2 retry is a documented economic wash vs an expensive planner call).",
            "Before/after is per-BUG (deduped) on both splits: the same bug re-run across ablation windows is one observation; grouped split keeps a bug entirely on one side.",
            "Held-out split is deterministic (bug-key hash) for reproducibility.",
            "n is small (single-machine accumulated free-tier runs); directional, not benchmark-grade — same standing honesty note as the ablations.",
            "The recalibrated bands are NOT auto-applied: applying them to runtime.difficulty is a deliberate human-reviewed step (write_difficulty_calibration).",
        ],
    }
    if out_dir is None:
        out_dir = logs_root / "analyze-history" / report["ts"]
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "report.json"
    atomic_write_json(p, report)
    report["_report_path"] = str(p)
    return report


def write_difficulty_calibration(
    bands: Dict[str, int], out_path: Path, source_report: str
) -> Path:
    """Write the recalibrated bands as the difficulty predictor's
    calibration file (the apply step of the maintenance loop).

    Assumes bands is {"easy_max": int, "hard_min": int} from
    recalibrate() and source_report is the report.json path the bands
    came from (provenance recorded in the file). The file is read
    OPT-IN by runtime.difficulty.load_calibration (see its docstring);
    removing the file reverts to the built-in v2 bands.
    """
    payload = {
        "kind": "difficulty-bands",
        "easy_max": int(bands["easy_max"]),
        "hard_min": int(bands["hard_min"]),
        "source_report": str(source_report),
        "written": time.strftime("%Y%m%d-%H%M%S"),
        "note": (
            "fitted by runtime.analyze_history's offline recalibration "
            "job; apply only after reviewing the held-out before/after "
            "in the source report (the job's recommendation field)"
        ),
    }
    atomic_write_json(out_path, payload)
    return out_path


def apply_recommendation(report: Dict[str, Any]) -> Optional[str]:
    """Apply the job's recommendation: write the calibration file when
    (and only when) the held-out evaluation actually improved.

    Assumes report is a build_report() dict. Returns the written path
    or None (no-apply cases: marginal/reject/insufficient data). This
    is the DELIBERATE apply step — a human runs the analysis, reviews
    the report, and the pipeline stays offline (never live/online).
    """
    if report.get("recommendation") != "apply":
        return None
    calibration = report.get("calibration") or {}
    if calibration.get("status") != "ok":
        return None
    before = report.get("before_heldout") or {}
    after = report.get("after_heldout") or {}
    if int(before.get("n", 0) or 0) <= 0 or int(after.get("n", 0) or 0) <= 0:
        return None
    before_accuracy = before.get("accuracy_easy_or_hard")
    after_accuracy = after.get("accuracy_easy_or_hard")
    if (
        not isinstance(before_accuracy, (int, float))
        or not isinstance(after_accuracy, (int, float))
        or float(after_accuracy) <= float(before_accuracy)
    ):
        return None
    from runtime.difficulty import calibration_path

    return str(
        write_difficulty_calibration(
            calibration["bands"],
            calibration_path(),
            report.get("_report_path", ""),
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="runtime.analyze_history")
    ap.add_argument("--logs-root", default="logs")
    ap.add_argument("--holdout-frac", type=float, default=0.25)
    ap.add_argument(
        "--out", default=None, help="output dir (default logs/analyze-history/<ts>)"
    )
    ap.add_argument("--json", action="store_true", help="print report JSON to stdout")
    args = ap.parse_args(argv)

    root = Path(args.logs_root)
    if not root.is_dir():
        print(f"error: logs root not found: {root}")
        return 2
    try:
        rep = build_report(
            root,
            holdout_frac=args.holdout_frac,
            out_dir=Path(args.out) if args.out else None,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"analyzed {rep['n_real_tasks']} real tasks")
        print(f"report: {rep.get('_report_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
