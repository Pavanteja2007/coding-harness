"""Ensemble routing tests (Improvement Round 2, runtime/ensemble.py).

Offline: fake harness through REAL scheduler worker subprocesses — the
two-phase flow (parallel cheap candidates -> escalation only on double
miss), the easy/medium pass-through, ledger aggregation, and stats
shapes are all exercised for real with zero network. Endpoint live-mode
proof is the ablation run itself (runtime/AGENTS.md Improvement Round 2).
"""

import json

import pytest

from runtime.ensemble import (
    ensemble_stats,
    predict_task_difficulty,
    run_ensemble,
)

CHEAP = {
    "provider": "openai",
    "model": "cheap-m",
    "api_key": "k1",
    "api_base": "http://x/v1",
}
EXPENSIVE = {
    "provider": "openai",
    "model": "expensive-m",
    "api_key": "k2",
    "api_base": "http://y/v1",
}

# Issue texts pinned to the v2 predictor's calibration: the easy one is
# a plain one-liner (scores 0-1); the hard one carries a stack trace +
# complexity keywords (scores 4+, same class as the ablation set's
# scary probes).
EASY_ISSUE = "max3() returns 2 for the input 1, 2, 3 instead of 3."
HARD_ISSUE = (
    "Workers crash intermittently under concurrent load. Log excerpt:\n"
    "```\nTraceback (most recent call last):\n"
    '  File "retrylib/backoff.py", line 4, in retry_delay\n'
    "ValueError: retry storm\n```\n"
    "Possibly a race between the scheduler and the timer; the timing of "
    "retries looks constant. Not sure if it's flaky-clock related."
)


def _bug(slug, issue, **extra):
    return {"slug": slug, "fixture": str(slug and "unused"), "issue": issue, **extra}


def _common(tmp_path):
    return {
        "use_fake_harness": True,
        "fake_steps": ["plan", "edit", "verify"],
        "fake_step_delay_s": 0.0,
        "max_wallclock_s": 120.0,
        "crash_retries": 0,
        "resume": False,
        "hang_heartbeat_stale_s": 60.0,
        "log_root": str(tmp_path / "tasklogs"),
    }


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """Fake-harness ensemble run config + a per-run logs root."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestPredictTaskDifficulty:
    def test_easy_issue_predicts_easy(self):
        hint, info = predict_task_difficulty(EASY_ISSUE)
        assert hint in ("easy", "medium")
        # the extracted issue includes the "## Issue\n" header prefix
        # (stripped only at "## Retrieved context"), same as the real
        # planner message the router ingress scores
        assert info["features"]["issue_chars"] > len(EASY_ISSUE)

    def test_hard_issue_predicts_hard(self):
        hint, _ = predict_task_difficulty(HARD_ISSUE)
        assert hint == "hard"

    def test_prediction_matches_router_planner_ingress(self):
        """The task-level hint must equal what the per-call router
        ingress decides on the planner call for the same issue — that
        equality is what makes the ensemble a strict extension of the
        same mechanism (same predictor, same calibration)."""
        from runtime.difficulty import predict_difficulty

        planner_msg = [
            {
                "role": "user",
                "content": (f"## Issue\n{HARD_ISSUE}\n\n## Retrieved context\nfoo"),
            }
        ]
        ingress_hint, _ = predict_difficulty(HARD_ISSUE, messages=planner_msg)
        assert predict_task_difficulty(HARD_ISSUE)[0] == ingress_hint


class TestRunEnsemble:
    def test_easy_task_single_adaptive_no_candidates(self, clean_env):
        """easy/medium: exactly one sub-task, ON-arm config (adaptive
        routing on, tiers wired) — the ensemble arm must be identical to
        ON for non-hard tasks."""
        bugs = [_bug("easybug", EASY_ISSUE)]
        per_bug, meta = run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-easy",
        )
        agg = per_bug["easybug"]
        assert agg["strategy"] == "single-adaptive"
        assert agg["hint"] in ("easy", "medium")
        assert len(agg["sub_tasks"]) == 1
        assert agg["sub_tasks"][0]["task_id"] == "ens-a-easybug"
        assert agg["status"] == "success"  # fake harness defaults to success
        assert meta["n_escalations"] == 0
        assert meta["phase2_run_id"] is None

    def test_hard_task_two_candidates_parallel_then_win(self, clean_env):
        """hard: TWO cheap-pinned candidate sub-tasks run; when a
        candidate verifies, NO escalation happens and the bug reports a
        candidate win."""
        bugs = [_bug("hardbug", HARD_ISSUE)]
        per_bug, meta = run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-hard",
        )
        agg = per_bug["hardbug"]
        assert agg["strategy"] == "ensemble-2cheap"
        assert agg["hint"] == "hard"
        assert [s["task_id"] for s in agg["sub_tasks"]] == [
            "ens-c1-hardbug",
            "ens-c2-hardbug",
        ]
        assert agg["status"] == "success"
        assert agg["candidate_win"] is True
        assert agg["escalated"] is False
        assert meta["n_escalations"] == 0
        assert meta["phase2_run_id"] is None

    def test_hard_task_escalates_on_double_miss(self, clean_env):
        """hard + BOTH candidates fail -> ONE expensive escalation run;
        its success is the bug's final status; the strategy is marked
        escalated."""
        bugs = [
            _bug("hardbug", HARD_ISSUE, ens_candidate_overrides={"fake_success": False})
        ]
        per_bug, meta = run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-esc",
        )
        agg = per_bug["hardbug"]
        assert agg["strategy"] == "ensemble-2cheap+escalated"
        assert [s["task_id"] for s in agg["sub_tasks"]] == [
            "ens-c1-hardbug",
            "ens-c2-hardbug",
            "ens-x-hardbug",
        ]
        assert agg["candidate_win"] is False
        assert agg["escalated"] is True
        assert agg["status"] == "success"  # escalation run succeeds
        assert meta["n_escalations"] == 1
        assert meta["phase2_run_id"] == "ens-test-esc-p2"
        # statuses recorded per sub-task: both candidates failed
        assert [s["status"] for s in agg["sub_tasks"][:2]] == ["failed", "failed"]
        assert agg["sub_tasks"][2]["status"] == "success"

    def test_hard_task_one_candidate_wins_skips_escalation(
        self, clean_env, monkeypatch
    ):
        """The ANY semantics: c1 succeeds, c2 fails -> NO escalation even
        though one candidate missed. Per-role config overrides aren't
        supported (documented: overrides apply to both candidates), so
        phase 1 is stubbed at scheduler_run with per-sub-task results —
        phase 1 has real end-to-end coverage in the tests above; the
        decision under test here is phase 2's."""
        from runtime import ensemble as ens_mod
        from shared.types import TaskResult

        def _mk(tid, status):
            return TaskResult(
                task_id=tid,
                status=status,
                attempts=1,
                diff=None,
                verification=None,
                cost_usd=0.0,
                model_calls=[],
                log_path="",
            )

        def fake_run(tasks, **_kw):
            out = {}
            for t in tasks:
                status = "success" if t.task_id == "ens-c1-hardbug" else "failed"
                out[t.task_id] = _mk(t.task_id, status)
            return out

        monkeypatch.setattr(ens_mod, "scheduler_run", fake_run)
        bugs = [_bug("hardbug", HARD_ISSUE)]
        per_bug, meta = run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-any",
        )
        agg = per_bug["hardbug"]
        assert agg["status"] == "success"
        assert agg["candidate_win"] is True
        assert agg["escalated"] is False
        assert agg["strategy"] == "ensemble-2cheap"
        assert meta["n_escalations"] == 0
        assert meta["phase2_run_id"] is None

    def test_mixed_set_strategies_and_stats(self, clean_env):
        """A mixed easy+hard set produces the right per-strategy split
        and arm-level stats: predicted-hard count, escalation count,
        candidate wins, success rate, model mix all measured."""
        bugs = [
            _bug("easybug", EASY_ISSUE),
            _bug("hardwin", HARD_ISSUE),
            _bug(
                "hardesc", HARD_ISSUE, ens_candidate_overrides={"fake_success": False}
            ),
        ]
        per_bug, meta = run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=3,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-mixed",
        )
        stats = ensemble_stats(per_bug)
        assert stats["tasks_predicted_hard"] == 2
        assert stats["ensemble_candidate_wins"] == 1
        assert stats["ensemble_escalated_tasks"] == 1
        assert stats["success_rate"] == 1.0
        assert stats["statuses"]["success"] == 3
        assert meta["n_escalations"] == 1
        assert meta["phase2_run_id"] == "ens-test-mixed-p2"
        # per-task re-keyed to the other arms' reporting shape
        assert "abl-ensemble-hardwin" in stats["per_task"]
        assert (
            stats["per_task"]["abl-ensemble-easybug"]["strategy"] == "single-adaptive"
        )
        # escalation metric: hardesc's ledger shows a cheap->expensive
        # move ONLY if the fake harness wrote model calls; with no
        # fake_model_calls the ledgers are empty and only the strategy
        # fields carry the escalation story (verified per sub-task above)
        hardesc = stats["per_task"]["abl-ensemble-hardesc"]
        assert hardesc["sub_tasks"][2]["task_id"] == "ens-x-hardesc"

    def test_candidate_configs_pinned_to_cheap(self, clean_env):
        """The spawned candidate task.json must pin the CHEAP tier and
        adaptive_routing=False (a candidate is a full run on one model);
        the escalation task must pin EXPENSIVE the same way. Read the
        task.json files the scheduler wrote for the workers."""
        bugs = [
            _bug("hardbug", HARD_ISSUE, ens_candidate_overrides={"fake_success": False})
        ]
        run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-cfg",
        )
        run_dir = clean_env / "tasklogs" / "ens-test-cfg-p1"
        for role in ("c1", "c2"):
            d = run_dir / f"ens-{role}-hardbug" / "attempt_0"
            cfg = json.loads((d / "task.json").read_text(encoding="utf-8"))
            assert cfg["config"]["model"] == "cheap-m"
            assert cfg["config"]["api_key"] == "k1"
            assert cfg["config"]["adaptive_routing"] is False
        d2 = clean_env / "tasklogs" / "ens-test-cfg-p2" / "ens-x-hardbug" / "attempt_0"
        cfg2 = json.loads((d2 / "task.json").read_text(encoding="utf-8"))
        assert cfg2["config"]["model"] == "expensive-m"
        assert cfg2["config"]["api_key"] == "k2"
        assert cfg2["config"]["adaptive_routing"] is False

    def test_adaptive_subtask_config_matches_on_arm(self, clean_env):
        """The easy/medium pass-through must carry the ON arm's exact
        routing config: adaptive_routing True + tier table with all
        three hints wired (cheap/cheap/expensive)."""
        bugs = [_bug("easybug", EASY_ISSUE)]
        run_ensemble(
            bugs,
            _common(clean_env),
            CHEAP,
            EXPENSIVE,
            concurrency=2,
            logs_root=str(clean_env / "tasklogs"),
            run_id="ens-test-arcfg",
        )
        d = clean_env / "tasklogs" / "ens-test-arcfg-p1" / "ens-a-easybug" / "attempt_0"
        cfg = json.loads((d / "task.json").read_text(encoding="utf-8"))
        c = cfg["config"]
        assert c["adaptive_routing"] is True
        assert c["model_tiers"]["easy"]["model"] == "cheap-m"
        assert c["model_tiers"]["medium"]["model"] == "cheap-m"
        assert c["model_tiers"]["hard"]["model"] == "expensive-m"


class TestEnsembleStats:
    def test_stats_shape_matches_ablation_collect(self):
        """ensemble_stats must produce the same top-level keys the on/off
        arms' collect() produces, so summary.json consumers stay
        uniform."""
        per_bug = {
            "b1": {
                "hint": "easy",
                "status": "success",
                "strategy": "single-adaptive",
                "sub_tasks": [],
                "calls": 3,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "ledger_cost_usd": 0.01,
                "models": {"cheap-m": 3},
                "difficulty_hints": {"easy": 3},
                "escalations_cheap_to_expensive": 0,
                "escalated": False,
                "candidate_win": False,
                "prediction_features": {},
                "attempts": 1,
                "result_cost_usd": 0.01,
            },
        }
        stats = ensemble_stats(per_bug)
        for key in (
            "per_task",
            "success_rate",
            "statuses",
            "total_calls",
            "total_tokens",
            "total_cost_usd",
            "model_mix",
            "total_escalations",
            "tasks_with_escalation",
        ):
            assert key in stats, key
        assert stats["success_rate"] == 1.0
        assert stats["total_calls"] == 3
        assert stats["tasks_predicted_hard"] == 0
