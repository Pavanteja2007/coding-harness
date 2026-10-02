"""Difficulty predictor + approval gate unit tests (offline)."""

import threading
import time

import pytest

from runtime import approval as ap
from runtime.difficulty import heuristic_features, predict_difficulty, score_to_hint


class TestHeuristicFeatures:
    def test_short_text_is_easy(self):
        assert score_to_hint(heuristic_features("fix typo")["score"]) == "easy"

    def test_loaded_text_is_hard(self):
        text = (
            "Crash Traceback ValueError " * 3 + "src/mod/parser.py utils/handlers.py "
            "race condition deadlock flaky intermittent timing "
            "sometimes not sure maybe "
        )
        feats = heuristic_features(text)
        assert feats["score"] >= 6
        assert score_to_hint(feats["score"]) == "hard"

    def test_medium_band(self):
        text = (
            "ImportError with a stack trace in module/utils.py, plus a code block ```"
        )
        feats = heuristic_features(text)
        assert 3 <= feats["score"] <= 5
        assert score_to_hint(feats["score"]) == "medium"

    def test_features_are_deterministic(self):
        t = "some reproducible text with src/file.py and race condition"
        assert heuristic_features(t) == heuristic_features(t)


class TestPredictDifficulty:
    def test_heuristic_estimator(self):
        hint, info = predict_difficulty("fix typo", estimator="heuristic")
        assert hint == "easy"
        assert info["estimator"] == "heuristic"
        assert "features" in info

    def test_keyword_matching_uses_word_boundaries(self):
        hint, _ = predict_difficulty(
            "A traceback should mention a change and regression",
            estimator="heuristic",
        )
        assert hint == "easy"

    def test_post_cut_prompt_sections_do_not_change_prediction(self):
        base = (
            "## Issue\nfix typo\n\n## Retrieved context\n"
            "src/example.py contains ordinary source code"
        )
        decorated = base + (
            "\n## Relevant past decisions\nFAILED AssertionError exit=1\n"
            "## Applicable skills\nTraceback SyntaxError\n"
            "## FETCH results\ndeadlock race intermittent\n"
            "## Coordinated-change fan-out\nflaky crash timing"
        )
        base_hint, base_info = predict_difficulty(
            "",
            messages=[
                {"role": "system", "content": "large planner template"},
                {"role": "user", "content": base},
            ],
        )
        decorated_hint, decorated_info = predict_difficulty(
            "",
            messages=[
                {"role": "system", "content": "large planner template"},
                {"role": "user", "content": decorated},
            ],
        )
        assert decorated_hint == base_hint == "easy"
        assert decorated_info["features"] == base_info["features"]

    def test_real_later_feedback_still_escalates(self):
        hint, info = predict_difficulty(
            "",
            messages=[
                {"role": "user", "content": "fix typo"},
                {"role": "assistant", "content": "try an edit"},
                {
                    "role": "user",
                    "content": "## Feedback\nFAILED tests/test_x.py AssertionError exit=1",
                },
            ],
        )
        assert hint == "medium"
        assert info["features"]["struggle_fails"] > 0

    def test_llm_estimator_parses_rating(self, monkeypatch):
        """The llm estimator maps a 1-5 reply to a hint (2->easy, 4->medium)."""
        import runtime.difficulty as diff

        replies = ["2", "4", "5"]
        calls = {"n": 0}

        def fake_call_model(messages, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return replies[i]

        monkeypatch.setattr(diff, "call_model", None, raising=False)
        # difficulty imports call_model lazily from .model_router — patch there
        import runtime.model_router as mr

        monkeypatch.setattr(mr, "call_model", fake_call_model)

        hint2, _ = predict_difficulty("text", estimator="llm")
        hint4, _ = predict_difficulty("text", estimator="llm")
        hint5, _ = predict_difficulty("text", estimator="llm")
        assert (hint2, hint4, hint5) == ("easy", "medium", "hard")

    def test_llm_estimator_receives_only_routing_text(self, monkeypatch):
        import runtime.model_router as mr

        captured = {}

        def fake_call_model(messages, **_kwargs):
            captured["messages"] = messages
            return "1"

        monkeypatch.setattr(mr, "call_model", fake_call_model)
        planner = [
            {"role": "system", "content": "PLANNER SYSTEM"},
            {
                "role": "user",
                "content": (
                    "## Issue\nfix typo\n\n## Retrieved context\nlarge context\n"
                    "## Applicable skills\nskill body"
                ),
            },
        ]
        predict_difficulty("", estimator="llm", messages=planner)
        rendered = str(captured["messages"])
        assert "fix typo" in rendered
        assert "PLANNER SYSTEM" not in rendered
        assert "## Retrieved context" not in rendered
        assert "skill body" not in rendered


class TestApprovalGate:
    def test_approve_after_short_wait(self, tmp_path):
        gate = str(tmp_path / "gate")
        t = threading.Timer(0.3, lambda: ap.decide(gate, approve=True))
        t.start()
        verdict = ap.request_approval(
            gate, "task-1", "diff...", "issue...", timeout_s=10, _clock=time.sleep
        )
        t.join()
        assert verdict == "approve"
        assert ap.pending_request(gate)["task_id"] == "task-1"

    def test_reject_raises(self, tmp_path):
        gate = str(tmp_path / "gate")
        t = threading.Timer(0.3, lambda: ap.decide(gate, approve=False))
        t.start()
        with pytest.raises(ap.ApprovalRejected):
            ap.request_approval(
                gate, "task-1", "diff...", "issue...", timeout_s=10, _clock=time.sleep
            )
        t.join()

    def test_timeout_raises_approval_timeout(self, tmp_path):
        gate = str(tmp_path / "gate")
        with pytest.raises(ap.ApprovalTimeout):
            ap.request_approval(
                gate, "task-1", "diff...", "issue...", timeout_s=0.2, _clock=time.sleep
            )

    def test_existing_request_survives_restart(self, tmp_path):
        import json

        gate = tmp_path / "gate"
        gate.mkdir()
        original = ap._request_payload(
            "task-1", "/repo", "diff...", "issue...", "review"
        )
        (gate / "request.json").write_text(json.dumps(original))
        timer = threading.Timer(0.2, lambda: ap.decide(str(gate), approve=True))
        timer.start()
        ap.request_approval(
            str(gate),
            "task-1",
            "diff...",
            "issue...",
            timeout_s=10,
            _clock=time.sleep,
            repo_path="/repo",
        )
        timer.join()
        current = json.loads((gate / "request.json").read_text())
        assert current["request_id"] == original["request_id"]
        assert current["fingerprint"] == original["fingerprint"]

    def test_stale_request_and_decision_are_rotated(self, tmp_path):
        import json

        gate = tmp_path / "gate"
        gate.mkdir()
        original = ap._request_payload(
            "task-a", "/repo-a", "diff-a", "issue-a", "review"
        )
        (gate / "request.json").write_text(json.dumps(original))
        ap.decide(str(gate), approve=True)

        with pytest.raises(ap.ApprovalTimeout):
            ap.request_approval(
                str(gate),
                "task-b",
                "diff-b",
                "issue-b",
                timeout_s=0,
                _clock=lambda _seconds: None,
                repo_path="/repo-b",
            )

        current = ap.pending_request(str(gate))
        assert current["task_id"] == "task-b"
        assert current["repo_path"] == "/repo-b"
        assert current["request_id"] != original["request_id"]
        assert not (gate / "decision.json").exists()


class TestLLMEstimatorFallback:
    def test_llm_estimator_falls_back_on_failure(self, monkeypatch):
        import sys
        import types

        from runtime.model_router import set_call_context

        def offline_failure(**_kwargs):
            raise RuntimeError("offline provider disabled")

        monkeypatch.setitem(
            sys.modules,
            "litellm",
            types.SimpleNamespace(completion=offline_failure),
        )
        set_call_context(None)
        hint, info = predict_difficulty(
            "fix typo",
            estimator="llm",
            llm_cfg={"model": "offline-model"},
        )
        assert hint == "easy"
        assert info["estimator"] == "llm"
        assert info["llm_fallback_reason"].startswith("RuntimeError:")
