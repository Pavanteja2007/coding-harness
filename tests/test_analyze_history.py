"""Cross-task history analysis + difficulty recalibration tests (offline).

Covers runtime/analyze_history.py (the Task A aggregates, the Task B
calibration dataset/split/fitter, the apply gate) and the difficulty
predictor's calibration-file override. Fixtures are SYNTHETIC log trees
— no real logs, no Docker, no network; every case is deterministic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runtime.analyze_history import (
    aggregate,
    apply_bands,
    apply_recommendation,
    bug_key,
    build_report,
    calibration_rows,
    evaluate,
    recalibrate,
    scan_tasks,
    split_holdout,
    summarize_task,
    write_difficulty_calibration,
)


def _write_trace(
    dirpath: Path,
    *,
    task_id: str,
    issue: str,
    mode: str | None = None,
    config: dict | None = None,
    attempts: int = 1,
    final_status: str = "success",
    strategy: str | None = "structural+grep (2 symbol(s) matched)",
    planned: bool = True,
    reason: str | None = None,
    start_mode: str | None = None,
    include_mode_event: bool = True,
    end_mode: str | None = None,
    repo_path: str = "x/repo",
    terminal: bool = True,
    result_status: str | None = None,
):
    """Write a synthetic harness trace.jsonl with the documented shapes."""
    cfg = {
        "max_retries": 2,
        "budget_cap_usd": 2.0,
        "use_mock_provider": False,
        "adaptive_routing": True,
    }
    cfg.update(config or {})
    events = [
        {
            "ts": time.time(),
            "kind": "task_start",
            "data": {
                "task_id": task_id,
                "repo_path": repo_path,
                "mode": start_mode,
                "issue_text": issue,
                "resumed": False,
                "config": cfg,
            },
        },
        {
            "ts": time.time(),
            "kind": "baseline_verify",
            "data": {
                "target_test": None,
                "target_passed_on_pristine": False,
                "flaky": False,
                "raw": "exit=1",
            },
        },
    ]
    if strategy is not None:
        events.append(
            {
                "ts": time.time(),
                "kind": "retrieval",
                "data": {
                    "strategy": strategy,
                    "terms": ["x"],
                    "files": ["a.py"],
                },
            }
        )
    if mode and include_mode_event:
        events.append(
            {
                "ts": time.time(),
                "kind": "mode",
                "data": {
                    "mode": mode,
                    "question": issue,
                },
            }
        )
    if planned:
        events.append(
            {
                "ts": time.time(),
                "kind": "plan",
                "data": {
                    "plan": [
                        {
                            "id": 1,
                            "description": "d",
                            "checkpoint": "c",
                            "files_hint": "",
                            "change_group": None,
                        }
                    ],
                    "resumed": False,
                },
            }
        )
    for i in range(1, attempts + 1):
        events.append(
            {"ts": time.time(), "kind": "attempt_start", "data": {"attempt": i}}
        )
    if terminal:
        end_data = {"status": final_status, "attempts": attempts}
        if reason:
            end_data["reason"] = reason
        terminal_mode = end_mode if end_mode is not None else mode
        if terminal_mode:
            end_data["mode"] = terminal_mode
        events.append({"ts": time.time(), "kind": "task_end", "data": end_data})
        events.append(
            {
                "ts": time.time(),
                "kind": "result",
                "data": {
                    "status": result_status or final_status,
                    "attempts": attempts,
                    "note": "",
                    "cost_usd": 0.01,
                },
            }
        )
    dirpath.mkdir(parents=True, exist_ok=True)
    with (dirpath / "trace.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _write_ledger(dirpath: Path, calls: list[dict]):
    """Write a synthetic model_ledger.jsonl ({id}.runtime sibling)."""
    rt = dirpath.with_name(dirpath.name + ".runtime")
    rt.mkdir(parents=True, exist_ok=True)
    with (rt / "model_ledger.jsonl").open("w", encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps(c) + "\n")


def _call(model="cheap-m", hint="easy", routed="easy", cost=0.001, tokens=100):
    return {
        "ts": "2026-09-14T00:00:00+00:00",
        "model": model,
        "provider": "openai",
        "prompt_tokens": tokens,
        "completion_tokens": tokens // 2,
        "tokens": tokens + tokens // 2,
        "cost_usd": cost,
        "elapsed_s": 1.0,
        "routed_via_hint": routed,
        "difficulty_hint": hint,
    }


class TestScanFilters:
    """The scripted/archive/mode exclusion filters (data honesty)."""

    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "logs"
        root.mkdir()
        return root

    def test_scripted_evals_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "evals" / "run1" / "baseline" / "bug1",
            task_id="bug1",
            issue="fix the mean bug",
        )
        assert scan_tasks(root) == []

    def test_mock_provider_config_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "run-a" / "task1",
            task_id="task1",
            issue="fix the bug",
            config={"use_mock_provider": True},
        )
        assert scan_tasks(root) == []

    def test_fake_harness_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "run-b" / "task2",
            task_id="task2",
            issue="fix the bug",
            config={"use_fake_harness": True},
        )
        assert scan_tasks(root) == []

    def test_archive_dirs_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(root / "run-c" / "task3", task_id="task3", issue="fix the bug")
        _write_trace(
            root / "run-c" / "task3.old-20260914-000000",
            task_id="task3",
            issue="fix the bug",
        )
        _write_trace(
            root / "run-c" / "staged.base", task_id="staged", issue="fix the bug"
        )
        _write_trace(
            root / "run-c" / "fix.old-api",
            task_id="fix-old-api",
            issue="ordinary task name containing archive-like text",
        )
        _write_trace(
            root / "run-c" / "fix.baseball-regression",
            task_id="fix-baseball",
            issue="ordinary task name containing base text",
        )
        recs = scan_tasks(root)
        assert [r["task_id"] for r in recs] == [
            "fix-baseball",
            "fix-old-api",
            "task3",
        ]

    def test_qa_mode_excluded_via_mode_event(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "modes-qa-1",
            task_id="modes-qa-1",
            issue="what does mean() do?",
            mode="question",
        )
        assert scan_tasks(root) == []

    def test_agent_start_mode_without_terminal_mode_is_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "agent-one",
            task_id="agent-one",
            issue="edit the parser",
            start_mode="agent",
            include_mode_event=False,
            end_mode=None,
        )
        assert scan_tasks(root) == []

    def test_scan_start_mode_without_terminal_mode_is_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "scan-one",
            task_id="scan-one",
            issue="inspect coverage",
            start_mode="scan",
            include_mode_event=False,
            end_mode=None,
        )
        assert scan_tasks(root) == []

    def test_conflicting_mode_signals_are_excluded(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "mode-conflict",
            task_id="mode-conflict",
            issue="fix parser",
            start_mode="fix",
            end_mode="agent",
        )
        assert scan_tasks(root) == []

    def test_partial_trace_is_not_history(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "partial",
            task_id="partial",
            issue="fix parser",
            terminal=False,
        )
        diagnostics = {}
        assert scan_tasks(root, diagnostics) == []
        assert diagnostics["incomplete"] == 1

    def test_result_conflict_is_not_history(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "conflict",
            task_id="conflict",
            issue="fix parser",
            final_status="success",
            result_status="failed",
        )
        assert scan_tasks(root) == []

    def test_build_mode_excluded_via_config(self, tmp_path):
        root = self._root(tmp_path)
        _write_trace(
            root / "modes-build-1",
            task_id="modes-build-1",
            issue="add mode()",
            config={"mode": "build"},
        )
        assert scan_tasks(root) == []

    def test_real_task_included_with_ledger(self, tmp_path):
        root = self._root(tmp_path)
        d = root / "run-d" / "abl-on-bug1"
        _write_trace(d, task_id="abl-on-bug1", issue="wrap() drops lines")
        _write_ledger(d, [_call(), _call(hint="medium", routed="medium")])
        recs = scan_tasks(root)
        assert len(recs) == 1
        r = recs[0]
        assert r["task_id"] == "abl-on-bug1"
        assert r["status"] == "success"
        assert r["attempts"] == 1
        assert r["repairs"] == 0
        assert r["calls"] == 2
        assert r["routed"] is True
        assert r["predicted_hint"] == "easy"  # one-liner issue -> v2 easy
        assert r["retrieval_strategy"].startswith("structural+grep")

    def test_pinned_off_arm_marked_not_routed(self, tmp_path):
        root = self._root(tmp_path)
        d = root / "run-e" / "abl-off-bug1"
        _write_trace(
            d,
            task_id="abl-off-bug1",
            issue="wrap() drops lines",
            config={"adaptive_routing": False},
        )
        _write_ledger(d, [_call(model="expensive-m", hint=None, routed=None)])
        recs = scan_tasks(root)
        assert recs[0]["routed"] is False

    def test_malformed_trace_does_not_suppress_valid_task(self, tmp_path):
        root = self._root(tmp_path)
        malformed = root / "run-bad" / "bad"
        malformed.mkdir(parents=True)
        (malformed / "trace.jsonl").write_text(
            json.dumps({"kind": "task_start", "data": None}) + "\n",
            encoding="utf-8",
        )
        _write_trace(
            root / "run-good" / "good",
            task_id="abl-on-good",
            issue="wrap() drops lines",
        )
        diagnostics = {}
        records = scan_tasks(root, diagnostics)
        assert [record["task_id"] for record in records] == ["abl-on-good"]
        assert diagnostics["malformed"] == 1

    def test_corrupt_ledger_line_preserves_valid_rows(self, tmp_path):
        root = self._root(tmp_path)
        task_dir = root / "run-ledger" / "abl-on-ledger"
        _write_trace(task_dir, task_id="abl-on-ledger", issue="fix typo")
        _write_ledger(task_dir, [_call(), _call()])
        ledger = task_dir.with_name(task_dir.name + ".runtime") / "model_ledger.jsonl"
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write("{partial\n")
            handle.write(json.dumps(["not", "a", "record"]) + "\n")
        diagnostics = {}
        record = scan_tasks(root, diagnostics)[0]
        assert record["calls"] == 2
        assert diagnostics["ledger_invalid"] == 2

    def test_credentials_never_enter_report(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        sentinel = "sk-history-secret-123456"
        task_dir = root / "run-secret" / "abl-on-secret"
        _write_trace(
            task_dir,
            task_id="abl-on-secret",
            issue=f"authentication failed with {sentinel}",
            config={
                "model_tiers": {
                    "hard": {"model": "custom", "api_key": sentinel},
                },
                "difficulty_llm": {"model": "classifier", "api_key": sentinel},
            },
            reason=f"provider rejected credential {sentinel}",
        )
        _write_ledger(task_dir, [_call()])
        scanned = scan_tasks(root)
        report = build_report(root, out_dir=tmp_path / "report")
        rendered = json.dumps(report)
        artifact = Path(report["_report_path"]).read_text(encoding="utf-8")
        assert sentinel not in rendered
        assert sentinel not in artifact
        assert sentinel not in json.dumps(scanned)
        assert "issue" not in scanned[0]


class TestDivergenceAndAggregates:
    def _task(self, **kw):
        base = {
            "task_id": "abl-on-x",
            "rel_dir": "ablations/r/x",
            "repo": None,
            "issue_chars": 50,
            "issue": "x",
            "status": "success",
            "attempts": 1,
            "repairs": 0,
            "failure_bucket": "success",
            "reason": None,
            "retrieval_strategy": None,
            "calls": 3,
            "cost_usd": 0.01,
            "tokens": 100,
            "models": {"cheap-m": 3},
            "difficulty_hints": {"easy": 3},
            "planner_routed_via": "easy",
            "routed": True,
            "predicted_hint": "easy",
            "predicted_score": 0,
        }
        base.update(kw)
        return base

    def test_aligned(self):
        agg = aggregate([self._task()])
        assert agg["predictor_divergence"]["aligned"]["n"] == 1

    def test_false_escalation_hard_pred_easy_reality(self):
        t = self._task(
            predicted_hint="hard",
            predicted_score=9,
            status="success",
            attempts=1,
            repairs=0,
        )
        agg = aggregate([t])
        assert agg["predictor_divergence"]["false_escalation"]["n"] == 1

    def test_missed_escalation_easy_pred_hard_reality(self):
        t = self._task(
            predicted_hint="easy",
            status="failed",
            attempts=2,
            failure_bucket="failed (verifier refused after retries)",
        )
        agg = aggregate([t])
        assert agg["predictor_divergence"]["missed_escalation"]["n"] == 1

    def test_pinned_tasks_not_scored(self):
        agg = aggregate([self._task(routed=False)])
        assert agg["predictor_divergence"]["n_pinned_tasks_not_scored"] == 1
        assert agg["predictor_divergence"]["n_routed_tasks"] == 0

    def test_retrieval_groups(self):
        t1 = self._task(retrieval_strategy="structural+grep (2 symbols)", repairs=0)
        t2 = self._task(task_id="abl-on-y", retrieval_strategy="grep", repairs=2)
        t3 = self._task(task_id="abl-on-z", retrieval_strategy=None, repairs=1)
        agg = aggregate([t1, t2, t3])
        r = agg["retrieval_vs_repairs"]
        assert r["structural+grep"]["n"] == 1
        assert r["structural+grep"]["mean_repairs"] == 0.0
        assert r["grep-only"]["n"] == 1
        assert r["grep-only"]["mean_repairs"] == 2.0
        assert r["other/none"]["n"] == 1

    def test_failure_patterns(self):
        t1 = self._task()
        t2 = self._task(
            task_id="abl-on-f",
            status="timeout",
            attempts=1,
            failure_bucket="timeout (wallclock/hang)",
            reason="wallclock cap",
        )
        agg = aggregate([t1, t2])
        fp = agg["failure_patterns"]
        assert fp["success"]["n"] == 1
        assert fp["timeout (wallclock/hang)"]["n"] == 1
        assert fp["timeout (wallclock/hang)"]["reasons"] == {"wallclock cap": 1}


class TestCalibrationRows:
    def _tasks(self, tmp_path, specs):
        root = tmp_path / "logs"
        root.mkdir(exist_ok=True)
        tasks = []
        for i, (tid, status, attempts) in enumerate(specs):
            d = root / f"run{i}" / tid
            issue = "wrap() drops the final line"  # predicts easy
            _write_trace(
                d, task_id=tid, issue=issue, attempts=attempts, final_status=status
            )
            _write_ledger(d, [_call()])
            rec = summarize_task(d, (f"run{i}", tid), root)
            assert rec is not None
            tasks.append(rec)
        return tasks

    def test_strict_labels_failed_is_hard_success_is_easy(self, tmp_path):
        tasks = self._tasks(
            tmp_path,
            [
                ("abl-on-a", "success", 1),
                ("abl-on-b", "success", 2),  # retry succeeded -> EASY (wash rule)
                ("abl-on-c", "failed", 2),  # verifier refused -> HARD
            ],
        )
        rows = calibration_rows(tasks)
        by = {r["task_id"]: r for r in rows}
        assert by["abl-on-a"]["label"] == "easy"
        assert by["abl-on-b"]["label"] == "easy"  # strict policy
        assert by["abl-on-c"]["label"] == "hard"

    def test_endpoint_errors_excluded_strict(self, tmp_path):
        tasks = self._tasks(
            tmp_path,
            [
                ("abl-on-d", "error", 1),
                ("abl-on-e", "timeout", 1),
            ],
        )
        rows = calibration_rows(tasks)
        assert rows == []  # endpoint death is not difficulty signal

    def test_struggle_policy_keeps_second_attempt(self, tmp_path):
        tasks = self._tasks(
            tmp_path,
            [
                ("abl-on-f", "success", 2),
                ("abl-on-g", "error", 2),
            ],
        )
        rows = calibration_rows(tasks, label_policy="struggle")
        assert all(r["label"] == "hard" for r in rows)
        assert len(rows) == 2

    def test_pinned_not_routed_excluded(self, tmp_path):
        tasks = self._tasks(tmp_path, [("abl-on-h", "success", 1)])
        tasks[0]["routed"] = False
        assert calibration_rows(tasks) == []

    def test_same_task_id_in_different_repos_is_separate(self, tmp_path):
        root = tmp_path / "logs"
        records = []
        for index, repo in enumerate(("repo-a", "repo-b")):
            task_dir = root / f"run-{index}" / "abl-on-shared"
            _write_trace(
                task_dir,
                task_id="abl-on-shared",
                issue="fix shared bug",
                repo_path=repo,
            )
            _write_ledger(task_dir, [_call()])
            records.append(summarize_task(task_dir, task_dir.parts, root))
        rows = calibration_rows(records)
        assert len(rows) == 2
        assert rows[0]["group_id"] != rows[1]["group_id"]

    def test_missing_repo_is_excluded_from_calibration(self, tmp_path):
        root = tmp_path / "logs"
        task_dir = root / "run" / "abl-on-no-repo"
        _write_trace(
            task_dir,
            task_id="abl-on-no-repo",
            issue="fix typo",
            repo_path="",
        )
        _write_ledger(task_dir, [_call()])
        record = summarize_task(task_dir, task_dir.parts, root)
        assert record is not None
        assert calibration_rows([record]) == []


class TestSplitAndEvaluate:
    def _row(self, tid, hint, score, label):
        return {
            "task_id": tid,
            "predicted_hint": hint,
            "predicted_hint_num": {"easy": 0, "medium": 1, "hard": 2}[hint],
            "predicted_score": score,
            "label": label,
        }

    def test_grouped_split_no_bug_straddles(self):
        rows = []
        for i in range(10):
            task_id = f"abl-on-bug{i}"
            for _run in range(3):
                rows.append(self._row(task_id, "easy", 0, "easy"))
        train, held = split_holdout(rows, frac=0.25)
        train_bugs = {bug_key(r["task_id"]) for r in train}
        held_bugs = {bug_key(r["task_id"]) for r in held}
        assert train_bugs & held_bugs == set()
        assert train and held

    def test_deterministic(self):
        rows = [self._row(f"abl-on-b{i}", "easy", i % 3, "easy") for i in range(20)]
        t1, h1 = split_holdout(list(rows))
        t2, h2 = split_holdout(list(reversed(rows)))
        assert [r["task_id"] for r in t1] == [r["task_id"] for r in t2]
        assert [r["task_id"] for r in h1] == [r["task_id"] for r in h2]

    def test_seed_changes_group_assignment(self):
        rows = [self._row(f"abl-on-b{i}", "easy", 0, "easy") for i in range(20)]
        _train1, held1 = split_holdout(rows, seed=1)
        _train2, held2 = split_holdout(rows, seed=99)
        assert {r["task_id"] for r in held1} != {r["task_id"] for r in held2}

    def test_invalid_fraction_rejected(self):
        rows = [self._row("abl-on-a", "easy", 0, "easy")]
        for fraction in (0, -0.1, 1, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="between 0 and 1"):
                split_holdout(rows, frac=fraction)

    def test_evaluate_per_bug_dedup(self):
        # bug-X observed in 3 run windows (1 hard, 2 easy) + bug-Y easy
        rows = [
            self._row("abl-on-x", "easy", 0, "easy"),
            self._row("abl-on-x", "easy", 0, "hard"),  # any-hard -> hard bug
            self._row("abl-on-x", "easy", 0, "easy"),
            self._row("abl-on-y", "easy", 0, "easy"),
        ]
        e = evaluate(rows, "t")
        assert e["n"] == 4
        assert e["per_bug"]["n"] == 2
        # bug-x easy-predicted hard-labeled -> missed escalation
        assert e["per_bug"]["missed_escalations"] == 1
        assert e["missed_escalations"] == 1  # per-run view
        assert e["accuracy_easy_or_hard"] == 0.75

    def test_evaluate_false_escalation(self):
        rows = [self._row("abl-on-z", "hard", 9, "easy")]
        e = evaluate(rows, "t")
        assert e["false_escalations"] == 1
        assert e["accuracy_easy_or_hard"] == 0.0


class TestRecalibrate:
    def _r(self, tid, score, label):
        return {
            "task_id": tid,
            "predicted_score": score,
            "label": label,
            "predicted_hint": None,
            "predicted_hint_num": None,
        }

    def test_perfect_separation_found(self):
        rows = [self._r(f"b{i}", 0, "easy") for i in range(4)] + [
            self._r(f"h{i}", 9, "hard") for i in range(4)
        ]
        cal = recalibrate(rows)
        assert cal["status"] == "ok"
        assert cal["train_error_rate"] == 0.0
        b = cal["bands"]
        # every easy score 0, every hard score 9: bands must separate
        assert apply_bands(0, b) == "easy"
        assert apply_bands(9, b) == "hard"

    def test_insufficient_data(self):
        assert recalibrate([self._r("a", 0, "easy")])["status"] == ("insufficient_data")

    def test_bands_are_v2_family(self):
        # fitted bands stay in the 2-threshold integer family
        rows = [self._r(f"t{i}", i % 6, "easy" if i % 2 else "hard") for i in range(12)]
        cal = recalibrate(rows)
        assert 0 <= cal["bands"]["easy_max"] < cal["bands"]["hard_min"] <= 7


class TestApplyGate:
    def test_marginal_recommendation_writes_nothing(self, tmp_path, monkeypatch):
        rep = {
            "recommendation": "marginal - not worth applying (held-out delta zero)",
            "calibration": {"status": "ok", "bands": {"easy_max": 0, "hard_min": 5}},
            "_report_path": "x/report.json",
        }
        monkeypatch.chdir(tmp_path)
        assert apply_recommendation(rep) is None
        assert not (tmp_path / "runtime" / "difficulty_calibration.json").exists()

    def test_apply_writes_authoritative_file(self, tmp_path, monkeypatch):
        import runtime.difficulty as difficulty

        calibration_file = tmp_path / "package" / "difficulty_calibration.json"
        monkeypatch.setattr(difficulty, "_CALIBRATION_FILE", calibration_file)
        monkeypatch.setattr(difficulty, "_cal_cache", None)
        monkeypatch.setattr(difficulty, "_cal_cache_mtime", None)
        monkeypatch.chdir(tmp_path)
        report = {
            "recommendation": "apply",
            "calibration": {
                "status": "ok",
                "bands": {"easy_max": 1, "hard_min": 5},
            },
            "before_heldout": {"n": 4, "accuracy_easy_or_hard": 0.5},
            "after_heldout": {"n": 4, "accuracy_easy_or_hard": 0.75},
            "_report_path": "x/report.json",
        }
        path = apply_recommendation(report)
        assert path == str(calibration_file)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["easy_max"] == 1 and data["hard_min"] == 5
        assert data["source_report"] == "x/report.json"
        assert difficulty.active_bands() == (1, 5)

    def test_apply_rejects_forged_recommendation_without_improvement(self, tmp_path):
        report = {
            "recommendation": "apply",
            "calibration": {
                "status": "ok",
                "bands": {"easy_max": 1, "hard_min": 5},
            },
        }
        assert apply_recommendation(report) is None
        assert not (tmp_path / "difficulty_calibration.json").exists()

    def test_bad_recommendation_shape_is_none(self):
        assert apply_recommendation({"recommendation": "reject"}) is None
        assert apply_recommendation({}) is None


class TestDifficultyCalibrationOverride:
    """runtime.difficulty's opt-in calibration file."""

    def test_file_overrides_bands(self, tmp_path, monkeypatch):
        import runtime.difficulty as d

        f = tmp_path / "difficulty_calibration.json"
        monkeypatch.setattr(d, "_CALIBRATION_FILE", f)
        monkeypatch.setattr(d, "_cal_cache", None)
        monkeypatch.setattr(d, "_cal_cache_mtime", None)
        f.write_text(json.dumps({"easy_max": 0, "hard_min": 5}), encoding="utf-8")
        assert d.active_bands() == (0, 5)
        assert d.score_to_hint(0) == "easy"
        assert d.score_to_hint(3) == "medium"  # 3 was medium pre, still medium
        assert d.score_to_hint(4) == "medium"  # 4 was HARD pre -> now medium
        assert d.score_to_hint(9) == "hard"

    def test_malformed_file_falls_back_to_builtin(self, tmp_path, monkeypatch):
        import runtime.difficulty as d

        f = tmp_path / "difficulty_calibration.json"
        monkeypatch.setattr(d, "_CALIBRATION_FILE", f)
        monkeypatch.setattr(d, "_cal_cache", None)
        monkeypatch.setattr(d, "_cal_cache_mtime", None)
        f.write_text("{not json", encoding="utf-8")
        assert d.active_bands() == (1, 4)
        assert d.score_to_hint(3) == "medium"
        assert d.score_to_hint(4) == "hard"

    def test_out_of_range_bands_rejected(self, tmp_path, monkeypatch):
        import runtime.difficulty as d

        f = tmp_path / "difficulty_calibration.json"
        monkeypatch.setattr(d, "_CALIBRATION_FILE", f)
        monkeypatch.setattr(d, "_cal_cache", None)
        monkeypatch.setattr(d, "_cal_cache_mtime", None)
        f.write_text(json.dumps({"easy_max": 8, "hard_min": 3}), encoding="utf-8")
        assert d.active_bands() == (1, 4)  # easy_max >= hard_min invalid

    def test_write_difficulty_calibration_roundtrip(self, tmp_path):
        p = write_difficulty_calibration(
            {"easy_max": 0, "hard_min": 5},
            tmp_path / "runtime" / "difficulty_calibration.json",
            "src/report.json",
        )
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        assert data["kind"] == "difficulty-bands"
        assert data["source_report"] == "src/report.json"


class TestBuildReportEndToEnd:
    def test_full_pipeline_on_synthetic_tree(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        # 2 easy bugs (success, routed) + 1 hard bug (failed, routed)
        #   + 1 pinned/off task + 1 scripted eval task (excluded)
        for i, (tid, status, att, issue) in enumerate(
            [
                ("abl-on-easy1", "success", 1, "wrap() drops the final line"),
                ("abl-on-easy2", "success", 1, "mean() divides by len-1"),
                (
                    "abl-on-hard1",
                    "failed",
                    2,
                    "ImportError with a stack trace in module/utils.py, plus a "
                    "code block ``` race deadlock",
                ),
            ]
        ):
            d = root / f"ablations/run{i}" / "tasklogs" / tid
            _write_trace(d, task_id=tid, issue=issue, attempts=att, final_status=status)
            _write_ledger(d, [_call(), _call()])
        d = root / "ablations/run0" / "tasklogs" / "abl-off-x"
        _write_trace(
            d,
            task_id="abl-off-x",
            issue="wrap() drops",
            config={"adaptive_routing": False},
        )
        _write_ledger(d, [_call(model="expensive-m", hint=None, routed=None)])
        _write_trace(
            root / "evals" / "r" / "baseline" / "bug1",
            task_id="bug1",
            issue="fix the mean bug",
        )

        rep = build_report(root, holdout_frac=0.5)
        assert rep["n_real_tasks"] == 4
        assert rep["n_routed_tasks"] == 3
        agg = rep["aggregate"]
        assert agg["n_tasks"] == 4
        # report written
        assert Path(rep["_report_path"]).exists()
        data = json.loads(Path(rep["_report_path"]).read_text(encoding="utf-8"))
        assert data["n_real_tasks"] == 4
        assert rep["calibration"]["status"] == "insufficient_data"
        assert rep["n_train_rows"] + rep["n_holdout_rows"] == 3

    def test_report_reproducible(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        for i in range(8):
            tid = f"abl-on-b{i}"
            d = root / f"run{i}" / tid
            _write_trace(
                d,
                task_id=tid,
                issue=f"bug number {i} in func{i}()",
                attempts=1,
                final_status="success",
            )
            _write_ledger(d, [_call()])
        r1 = build_report(root, out_dir=tmp_path / "a")
        r2 = build_report(root, out_dir=tmp_path / "b")
        assert r1["n_train_bugs"] == r2["n_train_bugs"]
        assert r1["n_holdout_bugs"] == r2["n_holdout_bugs"]
        assert [t["task_id"] for t in r1["before_heldout"]["missed_task_ids"]] == [
            t["task_id"] for t in r2["before_heldout"]["missed_task_ids"]
        ]


class TestCliCommand:
    def test_cli_analyze_history_runs(self, tmp_path, capsys, monkeypatch):
        """neo analyze-history end-to-end on a synthetic tree (no Docker,
        no network): exit 0, report written, honest fields present."""
        from cli.main import main as cli_main

        root = tmp_path / "logs"
        root.mkdir()
        for i in range(4):
            tid = f"abl-on-b{i}"
            d = root / f"run{i}" / tid
            _write_trace(
                d,
                task_id=tid,
                issue=f"func{i}() returns wrong value sometimes",
                attempts=1,
                final_status="success",
            )
            _write_ledger(d, [_call()])
        rc = cli_main(["analyze-history", "--log-root", str(root)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "analyzed" in out
        assert "report:" in out
        assert "predictor divergence" in out

    def test_cli_json_mode(self, tmp_path, capsys, monkeypatch):
        from cli.main import main as cli_main

        root = tmp_path / "logs"
        root.mkdir()
        d = root / "run" / "abl-on-b1"
        _write_trace(
            d,
            task_id="abl-on-b1",
            issue="func() returns wrong value",
            attempts=1,
            final_status="success",
        )
        _write_ledger(d, [_call()])
        rc = cli_main(["analyze-history", "--log-root", str(root), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["n_real_tasks"] == 1

    def test_cli_missing_log_root(self, tmp_path, capsys):
        from cli.main import main as cli_main

        rc = cli_main(["analyze-history", "--log-root", str(tmp_path / "nope")])
        assert rc == 2

    def test_cli_apply_noop_when_marginal(self, tmp_path, capsys, monkeypatch):
        """--apply with a non-apply recommendation writes nothing and
        says so (the deliberate-gate contract)."""
        from cli.main import main as cli_main

        root = tmp_path / "logs"
        root.mkdir()
        d = root / "run" / "abl-on-b1"
        _write_trace(
            d,
            task_id="abl-on-b1",
            issue="func() returns wrong value",
            attempts=1,
            final_status="success",
        )
        _write_ledger(d, [_call()])
        monkeypatch.chdir(tmp_path)
        rc = cli_main(["analyze-history", "--log-root", str(root), "--apply"])
        assert rc == 0
        out = capsys.readouterr().out
        # either the marginal no-apply note or insufficient data; both
        # must NOT have written the calibration file
        assert not (tmp_path / "runtime" / "difficulty_calibration.json").exists()
        assert "--apply" in out or "insufficient" in out
