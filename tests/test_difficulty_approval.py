"""Difficulty predictor + approval gate unit tests (offline)."""
import time
import threading

import pytest

from runtime import approval as ap
from runtime.difficulty import heuristic_features, predict_difficulty, score_to_hint


class TestHeuristicFeatures:
    def test_short_text_is_easy(self):
        assert score_to_hint(heuristic_features("fix typo")["score"]) == "easy"

    def test_loaded_text_is_hard(self):
        text = ("Crash Traceback ValueError " * 3
                + "src/mod/parser.py utils/handlers.py "
                  "race condition deadlock flaky intermittent timing "
                  "sometimes not sure maybe ")
        feats = heuristic_features(text)
        assert feats["score"] >= 6
        assert score_to_hint(feats["score"]) == "hard"

    def test_medium_band(self):
        text = "ImportError with a stack trace in module/utils.py, plus a code block ```"
        feats = heuristic_features(text)
        assert 3 <= feats["score"] <= 5
        assert score_to_hint(feats["score"]) == "medium"

    def test_features_are_deterministic(self):
        t = "some reproducible text with src/file.py and race condition"
        assert heuristic_features(t) == heuristic_features(t)


class TestPredictDifficulty:
    def test_heuristic_estimator(self):
        hint, info = predict_difficulty("fix typo", estimator="heuristic")
        assert hint in ("easy", "medium", "hard")
        assert info["estimator"] == "heuristic"
        assert "features" in info

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


class TestApprovalGate:
    def test_approve_after_short_wait(self, tmp_path):
        gate = str(tmp_path / "gate")
        t = threading.Timer(0.3, lambda: ap.decide(gate, approve=True))
        t.start()
        verdict = ap.request_approval(gate, "task-1", "diff...", "issue...",
                                      timeout_s=10, _clock=time.sleep)
        t.join()
        assert verdict == "approve"
        assert ap.pending_request(gate)["task_id"] == "task-1"

    def test_reject_raises(self, tmp_path):
        gate = str(tmp_path / "gate")
        t = threading.Timer(0.3, lambda: ap.decide(gate, approve=False))
        t.start()
        with pytest.raises(ap.ApprovalRejected):
            ap.request_approval(gate, "task-1", "diff...", "issue...",
                                timeout_s=10, _clock=time.sleep)
        t.join()

    def test_timeout_raises_approval_timeout(self, tmp_path):
        gate = str(tmp_path / "gate")
        with pytest.raises(ap.ApprovalTimeout):
            ap.request_approval(gate, "task-1", "diff...", "issue...",
                                timeout_s=0.2, _clock=time.sleep)

    def test_existing_request_survives_restart(self, tmp_path):
        """Re-entering the gate keeps the original request (crash-resume)."""
        import json
        gate = tmp_path / "gate"
        gate.mkdir()
        (gate / "request.json").write_text(json.dumps({"task_id": "task-1", "ts": "old"}))
        t = threading.Timer(0.2, lambda: ap.decide(str(gate), approve=True))
        t.start()
        ap.request_approval(str(gate), "task-1", "diff...", "issue...",
                            timeout_s=10, _clock=time.sleep)
        t.join()
        # the original request file was NOT rewritten
        assert json.loads((gate / "request.json").read_text())["ts"] == "old"


class TestLLMEstimatorFallback:
    def test_llm_estimator_falls_back_on_failure(self):
        # No mock installed, no keys -> llm estimator must fall back to
        # heuristic, never raise.
        hint, info = predict_difficulty("fix typo", estimator="llm")
        assert hint in ("easy", "medium", "hard")
        assert info["estimator"] == "llm"
        assert "llm_fallback_reason" in info
