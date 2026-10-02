"""VEX-TERM-UX-08 round 2 — the skills receipt a default daily run must carry.

Round 1 recorded `skill_receipt_in_journal` as a passing check against the
DEFAULT daily path. Replaying that exact campaign on the live tree fails it:
a run injects a project skill into the model's first request and journals NO
`skills` and NO `skill_model_content` row.

The reason is structural, not a missing call. `harness/agent_loop.py` builds
`spec.metadata["skills_receipt"]` on two of its three entry points, and
`DailyCodingStrategy.run` emits the two journal rows from that metadata key.
The DEFAULT dispatch (`run_agent` -> `SessionController.run_turn`) builds its
own `RunSpec` with `metadata={"config": ...}` and no receipt, while the skills
themselves arrive through a completely different mechanism — the context
compiler's skills section. Content and receipt therefore had two producers and
only one of them was wired.

So this suite pins two things:

1. the receipt EXISTS on the default path (the regression), and
2. the receipt CANNOT claim delivery that did not happen (the honesty rule).

Offline and deterministic: every test builds its own repository and journal
under `tmp_path` and injects a scripted boundary through the documented
`harness.deps.set_call_model` seam. No Docker, no provider, no network, and
the developer's real `logs/` tree is never read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from harness import deps
from harness.knowledge import KnowledgeContext

SKILL_MARKER = "NEO_SKILL_MARKER_qa_7f21"


def _write_skill(repo: Path, name: str, description: str, body: str) -> None:
    skill = repo / ".neo" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def _scripted(*replies: str):
    """A Boundary-2 double that also answers the router's usage reader."""
    remaining = list(replies)

    def _call(messages: List[Dict[str, str]], **kwargs: Any) -> str:
        return (
            remaining.pop(0)
            if remaining
            else json.dumps({"tool": "finish", "answer": "done"})
        )

    _call.get_last_usage = lambda: {
        "model": "scripted",
        "provider": "scripted",
        "tokens": 1,
        "cost_usd": 0.0,
    }
    return _call


def _journal_rows(log_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for trace in log_root.rglob("trace.jsonl"):
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _kinds(rows: List[Dict[str, Any]]) -> List[str]:
    return [str(row.get("event") or row.get("kind") or "") for row in rows]


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("payload", "data"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _default_run(repo: Path, request: str, config: Dict[str, Any] | None = None):
    """Run the product's DEFAULT agent dispatch, unconfigured by strategy."""
    from harness import agent_loop

    log_root = repo.parent / f"{repo.name}-logs"
    log_root.mkdir(parents=True, exist_ok=True)
    merged: Dict[str, Any] = {"model": "scripted", "provider": "scripted"}
    merged.update(config or {})
    deps.set_call_model(_scripted())
    try:
        result = agent_loop.run_agent(request, str(repo), merged, log_root=log_root)
    finally:
        deps.reset_overrides()
    return result, _journal_rows(log_root)


class TestTheDefaultPathCarriesTheReceipt:
    """The regression: content and receipt had two producers; one was wired."""

    def test_a_default_daily_run_journals_both_skills_rows(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        _result, rows = _default_run(
            repo, "validate terminal QA evidence and record the journal receipt"
        )

        kinds = _kinds(rows)
        assert "skills" in kinds, kinds
        assert "skill_model_content" in kinds, kinds

    def test_the_receipt_names_the_skill_that_reached_the_model(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        _result, rows = _default_run(
            repo, "validate terminal QA evidence and record the journal receipt"
        )
        receipt = next(
            _payload(row)
            for row in rows
            if (row.get("event") or row.get("kind")) == "skills"
        )

        assert "qa" in receipt.get("matched", []), receipt
        assert receipt.get("model_content") is True, receipt

    def test_the_receipt_is_derived_from_the_bundle_that_was_injected(
        self, tmp_path
    ) -> None:
        """Not a second scan.

        A receipt produced by re-scanning could name a skill the run did NOT
        inject (a different cache key, a changed file, a narrower budget). The
        provenance key makes the claim checkable rather than plausible.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        _result, rows = _default_run(
            repo, "validate terminal QA evidence and record the journal receipt"
        )
        receipt = next(
            _payload(row)
            for row in rows
            if (row.get("event") or row.get("kind")) == "skills"
        )

        assert receipt.get("provenance") == "compiled_context_bundle", receipt

    def test_the_skill_body_really_is_in_the_first_model_request(
        self, tmp_path
    ) -> None:
        """The receipt is not the point; the CONTENT is. Prove both."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        seen: Dict[str, bool] = {}

        def _boundary(messages: List[Dict[str, str]], **kwargs: Any) -> str:
            seen["marker"] = any(
                SKILL_MARKER in str(message.get("content") or "")
                for message in messages
            )
            return json.dumps({"tool": "finish", "answer": "done"})

        _boundary.get_last_usage = lambda: {
            "model": "scripted",
            "provider": "scripted",
            "tokens": 1,
            "cost_usd": 0.0,
        }
        log_root = tmp_path / "logs"
        log_root.mkdir()
        from harness import agent_loop

        deps.set_call_model(_boundary)
        try:
            agent_loop.run_agent(
                "validate terminal QA evidence and record the journal receipt",
                str(repo),
                {"model": "scripted", "provider": "scripted"},
                log_root=log_root,
            )
        finally:
            deps.reset_overrides()

        assert seen.get("marker") is True, "the skill body never reached the model"

    def test_the_receipt_fires_once_per_run_not_once_per_turn(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        _result, rows = _default_run(
            repo, "validate terminal QA evidence and record the journal receipt"
        )
        kinds = _kinds(rows)

        assert kinds.count("skills") <= 1, kinds
        assert kinds.count("skill_model_content") <= 1, kinds


class TestTheReceiptCannotLie:
    """A receipt that claims a delivery which did not happen is the defect."""

    def test_no_matching_skill_reports_no_delivery(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(repo, "unrelated", "Use for kubernetes deployment charts.", "nope")

        knowledge = KnowledgeContext(str(repo), config={"skills_enabled": True})
        knowledge.compile(issue_text="validate terminal QA evidence")
        receipt = knowledge.skill_receipt()

        assert receipt["matched"] == [], receipt
        assert receipt["model_content"] is False, receipt
        assert receipt["included"] is False, receipt

    def test_the_none_matched_placeholder_is_never_a_delivery(self, tmp_path) -> None:
        """`(none matched)` is the compiler's "found nothing" marker.

        The compiler emits a `skills` SECTION even when nothing matched, so a
        receipt that only checked for the section's presence would claim a
        delivered skill for every run in every repository. This is the exact
        shape of the bug, so it is pinned against the real compiler output
        rather than against a hand-built bundle.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        knowledge = KnowledgeContext(str(repo), config={"skills_enabled": True})
        knowledge.compile(issue_text="anything at all")
        receipt = knowledge.skill_receipt()

        assert receipt, "the run did consult skills, so a receipt is owed"
        assert receipt["model_content"] is False, receipt
        assert receipt["skipped"] == "no skill matched", receipt
        assert receipt["rendered"] == [], receipt

    def test_the_placeholder_constant_is_the_one_authority(self) -> None:
        """Two literals for "nothing matched" would drift, and the drift
        would read as a delivered skill."""
        from harness import skills

        assert skills.NONE_MATCHED == "(none matched)"
        assert skills.render_skills_block([]) == skills.NONE_MATCHED

    def test_disabled_skills_produce_no_receipt_at_all(self, tmp_path) -> None:
        """A caller gating on truthiness must not publish an empty receipt."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        knowledge = KnowledgeContext(str(repo), config={"skills_enabled": False})
        knowledge.compile(issue_text="validate terminal QA evidence")

        assert knowledge.skill_receipt() == {}

    def test_a_receipt_is_absent_before_anything_is_compiled(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

        knowledge = KnowledgeContext(str(repo), config={"skills_enabled": True})

        assert knowledge.skill_receipt() == {}

    def test_a_broken_receiver_cannot_take_the_run_down(self, tmp_path) -> None:
        """A receipt is evidence. It must never change a run's outcome."""

        class _Exploding:
            def skill_receipt(self):
                raise RuntimeError("receipt exploded")

        from harness.agent_kernel.strategy import DailyCodingStrategy

        strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
        strategy._skill_receipt_emitted = False
        emitted: List[str] = []
        strategy._event = lambda kind, data: emitted.append(kind)  # type: ignore[method-assign]

        # No exception escapes: the guard's whole job is to be unfailable.
        DailyCodingStrategy._emit_skill_receipt(strategy, _Exploding())
        assert emitted == []

    def test_a_receiver_without_the_method_is_skipped(self, tmp_path) -> None:
        from harness.agent_kernel.strategy import DailyCodingStrategy

        strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
        strategy._skill_receipt_emitted = False
        emitted: List[str] = []
        strategy._event = lambda kind, data: emitted.append(kind)  # type: ignore[method-assign]

        DailyCodingStrategy._emit_skill_receipt(strategy, object())
        assert emitted == []


class TestThePinnedPathsStillWork:
    """The two paths that already emitted a receipt must not double up."""

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param({"agent_kernel_enabled": True}, id="kernel-adapter"),
            pytest.param({"agent_strategy": "legacy_agent"}, id="legacy-agent"),
        ],
    )
    def test_each_path_journals_exactly_one_receipt(self, tmp_path, config) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        _write_skill(
            repo,
            "qa",
            "Use when validating terminal QA evidence.",
            SKILL_MARKER,
        )

        _result, rows = _default_run(
            repo,
            "validate terminal QA evidence and record the journal receipt",
            config,
        )
        kinds = _kinds(rows)

        assert kinds.count("skills") == 1, kinds
        assert kinds.count("skill_model_content") == 1, kinds

    def test_a_caller_supplied_receipt_stays_authoritative(self, tmp_path) -> None:
        """The kernel adapter's own receipt is not replaced by the derived one.

        It carries Ceiling-12 declaration data the bundle cannot reconstruct,
        so the compiled-bundle path is a FALLBACK, not an override.
        """
        from harness.agent_kernel.strategy import DailyCodingStrategy

        strategy = DailyCodingStrategy.__new__(DailyCodingStrategy)
        strategy._skill_receipt_emitted = False
        emitted: List[str] = []
        strategy._event = lambda kind, data: emitted.append(kind)  # type: ignore[method-assign]

        class _Knowledge:
            def skill_receipt(self):
                return {"provenance": "compiled_context_bundle"}

        # `_compile_knowledge` only calls the emitter; the metadata branch in
        # `run()` is what publishes a caller's receipt, and it returns first
        # when the key is present. Assert the guard cannot fire twice.
        DailyCodingStrategy._emit_skill_receipt(strategy, _Knowledge())
        DailyCodingStrategy._emit_skill_receipt(strategy, _Knowledge())

        assert emitted == ["skills", "skill_model_content"], emitted


class TestTheVerifierGateIsUntouched:
    """This round changed a receipt. It must not have touched the mint."""

    def test_the_completion_mint_does_not_mention_skills(self) -> None:
        from pathlib import Path as _Path

        for relative in (
            "harness/knowledge.py",
            "harness/agent_kernel/strategy.py",
        ):
            source = (_Path(__file__).resolve().parents[1] / relative).read_text(
                encoding="utf-8", errors="replace"
            )
            assert "target_test_passed" not in source, relative
            assert "regression_passed" not in source, relative
