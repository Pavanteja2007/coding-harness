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

Run:  python -m runtime.analyze_history  (or: vex analyze-history)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
_ARCHIVE_SUFFIXES = (".old-", ".base")


def _is_archive_dir(name: str) -> bool:
    """True for the harness's archived/staged task-dir siblings."""
    return any(name.startswith(s) or (s in name) for s in _ARCHIVE_SUFFIXES)


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


def _load_ledger(task_dir: Path) -> List[Dict[str, Any]]:
    """A task's per-call routing ledger ({task_id}.runtime/ sibling)."""
    runtime_dir = task_dir.with_name(task_dir.name + ".runtime")
    p = runtime_dir / "model_ledger.jsonl"
    try:
        return [
            json.loads(l)
            for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    except (OSError, ValueError):
        return []


def _task_outcome(trace: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Final status + attempts from the last result/task_end events.
    Non-fix modes (question/research/build) carry mode= on task_end;
    the returned dict propagates it so callers can exclude them."""
    status = None
    attempts = None
    reason = None
    mode = None
    for ev in trace:
        if ev["kind"] == "result":
            d = ev.get("data", {})
            status = d.get("status")
            attempts = d.get("attempts")
        elif ev["kind"] == "task_end":
            d = ev.get("data", {})
            status = d.get("status") or status
            attempts = d.get("attempts", attempts)
            reason = d.get("reason")
            mode = d.get("mode") or mode
    return {"status": status, "attempts": attempts, "reason": reason, "mode": mode}


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
    task_dir: Path, rel_parts: tuple, logs_root: Path
) -> Optional[Dict[str, Any]]:
    """One task's aggregate record, or None when there is no real signal.

    Assumes task_dir holds a trace.jsonl (harness tasks always write
    one; a dir with only state.json is an interrupted run and still has
    a partial trace if the harness started). Returns the record with
    issue text, config, outcome, attempts, repairs, retrieval strategy,
    ledger rollup, and re-derived difficulty prediction (router ingress
    shape, same as runtime.ensemble.predict_task_difficulty).
    Non-fix modes (question/research/build — the modes round) carry no
    fix-loop difficulty semantics: excluded from the fix-routing
    dataset (their traces end in a mode-specific task_end).
    """
    trace_p = task_dir / "trace.jsonl"
    if not trace_p.exists():
        return None
    trace = list(_iter_trace(trace_p))
    if not trace:
        return None
    start = next((e for e in trace if e["kind"] == "task_start"), None)
    if start is None:
        return None
    d = start.get("data", {})
    cfg = d.get("config") or {}
    # Non-fix modes (the modes round): question/research carry their
    # mode on a dedicated `mode` event / task_end field; build sets
    # config.mode. None of them have fix-loop difficulty semantics.
    if cfg.get("mode") in ("question", "research", "build"):
        return None
    if any(e["kind"] == "mode" for e in trace):
        return None
    if (_task_end := _task_outcome(trace)) and _task_end.get("mode"):
        # task_end for non-fix modes carries mode= (fix never does)
        return None
    if _is_scripted(rel_parts, cfg):
        return None
    issue = str(d.get("issue_text", ""))
    outcome = _task_outcome(trace)
    repairs = _count_repairs(trace)
    ret = next((e for e in trace if e["kind"] == "retrieval"), None)
    strategy = ret.get("data", {}).get("strategy") if ret else None

    ledger = _load_ledger(task_dir)
    hints: Dict[str, int] = {}
    models: Dict[str, int] = {}
    cost = 0.0
    tokens = 0
    planner_hint = None
    routed_calls = 0
    for rec in ledger:
        models[rec["model"]] = models.get(rec["model"], 0) + 1
        h = rec.get("difficulty_hint")
        if h:
            hints[h] = hints.get(h, 0) + 1
        cost += float(rec.get("cost_usd", 0.0))
        tokens += int(rec.get("tokens", 0) or 0)
        if rec.get("routed_via_hint"):
            routed_calls += 1
        # first call ~ the planner call (the router's per-call ingress
        # prediction on the planner message = the task-level hint, the
        # ensemble's documented equality)
        if planner_hint is None:
            planner_hint = rec.get("routed_via_hint") or rec.get("difficulty_hint")
    # Was this task actually ROUTED by predictions (ON-arm semantics)?
    # Pinned/off-arm ledgers carry routed_via_hint=None on every call.
    routed = routed_calls > 0

    # Re-derive the task-level difficulty prediction with the SAME
    # predictor + message shape the router ingress uses (documented
    # equality with the planner-call decision).
    predicted_hint = None
    predicted_score = None
    if issue:
        try:
            from runtime.ensemble import predict_task_difficulty

            predicted_hint, info = predict_task_difficulty(issue)
            predicted_score = info.get("features", {}).get("score")
        except Exception:
            predicted_hint = None

    task_id = d.get("task_id") or task_dir.name
    return {
        "task_id": task_id,
        "rel_dir": "/".join(rel_parts),
        "repo": d.get("repo_path"),
        "issue_chars": len(issue),
        "issue": issue[:200],
        "status": outcome["status"],
        "attempts": outcome["attempts"],
        "attempts_raw": (len([e for e in trace if e["kind"] == "attempt_start"])),
        "repairs": repairs,
        "failure_bucket": _failure_bucket(outcome["status"], outcome["reason"]),
        "reason": outcome["reason"],
        "retrieval_strategy": strategy,
        "calls": len(ledger),
        "cost_usd": round(cost, 6),
        "tokens": tokens,
        "models": models,
        "difficulty_hints": hints,
        "planner_routed_via": planner_hint,
        "routed": routed,
        "predicted_hint": predicted_hint,
        "predicted_score": predicted_score,
    }


def scan_tasks(logs_root: Path) -> List[Dict[str, Any]]:
    """Walk the logs tree; every dir holding a REAL task trace becomes a
    record. Skips scripted/fake runs per _is_scripted, the shared
    skip-dirs (pristine/work copies etc.), and ARCHIVED task dirs
    ({task_id}.old-*/{task_id}.base siblings). An archived trace is a
    stale PREFIX of its live sibling's history: the live dir's final
    result/attempt fields are cumulative across restarts (the resume
    contract) and the .runtime ledger appends across relaunches, so
    dropping archives loses nothing and avoids double-counting tasks
    (measured: 77 duplicated task_ids on the real tree before this
    filter). The same task_id in DIFFERENT ablation runs is a legitimate
    separate observation (different run windows) and is kept."""
    out: List[Dict[str, Any]] = []
    root = logs_root.resolve()
    for dirpath, _dirnames, filenames in os_walk_pruned(root):
        if "trace.jsonl" not in filenames:
            continue
        task_dir = Path(dirpath)
        if _is_archive_dir(task_dir.name):
            continue
        rel = task_dir.relative_to(root)
        parts = rel.parts
        if any(p in _SKIP_DIRS for p in parts):
            continue
        rec = summarize_task(task_dir, parts, root)
        if rec is not None:
            out.append(rec)
    return out


def os_walk_pruned(root: Path):
    """os.walk with the skip-dirs pruned at every level (same discipline
    as dashboard/collect.py — pristine/work trees would multiply scan
    time by the size of every repo ever fixed)."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
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
    """Stable per-BUG key: strip the arm/run prefix so re-runs of the
    same bug across ablation windows collapse to one observation."""
    for p in _ABL_PREFIXES:
        if task_id.startswith(p):
            return task_id[len(p) :]
    return task_id


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
            "task_ids": [t["task_id"] for t in members][:50],
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
            f["task_ids"].append(t["task_id"])
            r = (t.get("reason") or "")[:120]
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
        rows.append(
            {
                "task_id": t["task_id"],
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
    by_bug: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        by_bug.setdefault(bug_key(r["task_id"]), []).append(r)
    per_bug_rows = []
    for bug, rs in by_bug.items():
        # one row per bug: hard if ANY run was hard (a bug that EVER
        # needed retries is a hard bug), prediction = the first run's
        # (deterministic predictor — identical issue text)
        per_bug_rows.append(
            {
                "task_id": bug,
                "predicted_hint": rs[0]["predicted_hint"],
                "predicted_hint_num": rs[0]["predicted_hint_num"],
                "predicted_score": rs[0].get("predicted_score"),
                "label": "hard" if any(x["label"] == "hard" for x in rs) else "easy",
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
    """Deterministic GROUPED split (by bug — the same bug's re-runs
    across ablation windows must not straddle train/holdout or the
    train data leaks into the evaluation). Sorted by bug key, hashed
    round-robin; same data -> same split, every time (the committed
    report must be reproducible from disk)."""
    by_bug: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_bug.setdefault(bug_key(r["task_id"]), []).append(r)
    ordered = sorted(by_bug.items())
    held: List[Dict[str, Any]] = []
    train: List[Dict[str, Any]] = []
    denom = max(round(1 / frac), 1)
    for i, (bug, rs) in enumerate(ordered):
        h = sum(ord(c) for c in bug)
        if (h + i) % denom == 0:
            held.extend(rs)
        else:
            train.extend(rs)
    # guarantee both sides non-empty when data allows
    if not held and train:
        held = [train[-1]]
        train = train[:-1]
    if not train and held:
        train = [held[0]]
        held = held[1:]
    return train, held


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
    tasks = scan_tasks(logs_root)
    agg = aggregate(tasks)
    rows = calibration_rows(tasks)  # strict policy (routing economics)
    train, held = split_holdout(rows, frac=holdout_frac)

    # Per-bug training rows (deduped): calibration fits these so a bug
    # re-run across ablation windows is ONE observation, not a weight.
    def _dedup(rs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        by_bug: Dict[str, Dict[str, Any]] = {}
        for r in rs:
            k = bug_key(r["task_id"])
            if k not in by_bug or r["label"] == "hard":
                base = dict(by_bug.get(k, r))
                base.update(r)
                base["task_id"] = k
                by_bug[k] = base
        return sorted(by_bug.values(), key=lambda r: r["task_id"])

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
        "logs_root": str(logs_root),
        "n_real_tasks": len(tasks),
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
    p.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
    cal = report.get("calibration") or {}
    if cal.get("status") != "ok":
        return None
    return str(
        write_difficulty_calibration(
            cal["bands"],
            Path("runtime") / "difficulty_calibration.json",
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
    rep = build_report(
        root,
        holdout_frac=args.holdout_frac,
        out_dir=Path(args.out) if args.out else None,
    )
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"analyzed {rep['n_real_tasks']} real tasks")
        print(f"report: {rep.get('_report_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
