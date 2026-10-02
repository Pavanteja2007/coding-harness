"""Ceiling Prompt 17 - the shadow gate's own honesty rules.

The point of a gate is that its verdicts cannot be talked into being better
than the evidence. Every test here therefore tries to make a lane, a task, or
a verdict look healthier than it is, and asserts the gate refuses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import shadow_gate as sg


class TestManifestIntegrity:
    """A task the matrix cannot honestly measure must not be claimable."""

    def test_the_manifest_has_five_repositories_and_ten_tasks(self) -> None:
        assert len(sg.REPOS) >= 5
        assert len(sg.SHADOW_TASKS) >= 10
        assert len({task.repo for task in sg.SHADOW_TASKS}) >= 5

    def test_every_task_names_a_pinned_repository_commit(self) -> None:
        for task in sg.SHADOW_TASKS:
            spec = sg.REPOS[task.repo]
            assert len(spec.commit) == 40, f"{task.slug} has an unpinned commit"
            assert spec.url.startswith("https://"), f"{task.slug} url"

    def test_every_task_has_a_real_oracle_and_a_real_probe(self) -> None:
        for task in sg.SHADOW_TASKS:
            assert task.target_test, f"{task.slug} has no target test"
            assert task.suite_tests, f"{task.slug} has no suite"
            assert task.behavior_probe, f"{task.slug} has no behavior probe"
            assert task.original != task.defective
            assert task.issue.strip(), f"{task.slug} has no issue text"

    def test_every_task_defect_occurs_exactly_once_in_its_source(self) -> None:
        # Ambiguity here would make "apply the defect" silently wrong: a task
        # whose text occurs twice is not a task, it is a coin flip.
        cache = sg.clone_cache_dir()
        for task in sg.SHADOW_TASKS:
            clone = cache / task.repo
            if not (clone / task.source_file).is_file():
                pytest.skip(f"clone for {task.repo} is not present")
            text = (clone / task.source_file).read_text(encoding="utf-8")
            assert text.count(task.original) == 1, (
                f"{task.slug}: original text occurs "
                f"{text.count(task.original)} times in {task.source_file}"
            )
            assert task.defective not in text, f"{task.slug} defect already present"

    def test_every_behavior_probe_is_implemented(self) -> None:
        for task in sg.SHADOW_TASKS:
            body = sg.probe_body(task.behavior_probe)
            assert "out = " in body, f"{task.slug} probe never sets a verdict"
        with pytest.raises(ValueError):
            sg.probe_body("no-such-probe")

    def test_every_scenario_has_assertions(self) -> None:
        for scenario in sg.DAILY_SCENARIOS:
            assert scenario["slug"] in sg.DAILY_ASSERTIONS, scenario["slug"]
            assert scenario["requirement"].strip()
        assert len(sg.DAILY_SCENARIOS) >= 10


class TestDefectApplication:
    """The defect must be applied and reverted by what is actually on disk."""

    def test_apply_refuses_an_ambiguous_original(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "m.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        task = sg.ShadowTask(
            slug="t",
            repo="parse",
            source_file="pkg/m.py",
            original="x = 1",
            defective="x = 2",
            issue="i",
            target_test="t.py",
            suite_tests=("t.py",),
            behavior_probe="percent",
            defect_class="d",
        )
        result = sg.apply_defect(task, repo)
        assert result["applied"] is False
        assert "2 times" in result["error"]

    def test_revert_restores_the_upstream_bytes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "m.py"
        target.write_text("before\n", encoding="utf-8")
        task = sg.ShadowTask(
            slug="t",
            repo="parse",
            source_file="m.py",
            original="before",
            defective="after",
            issue="i",
            target_test="t.py",
            suite_tests=("t.py",),
            behavior_probe="percent",
            defect_class="d",
        )
        assert sg.apply_defect(task, repo)["applied"] is True
        assert target.read_text(encoding="utf-8") == "after\n"
        assert sg.revert_defect(task, repo) is True
        assert target.read_text(encoding="utf-8") == "before\n"

    def test_revert_is_a_no_op_on_clean_uptstream_text(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "m.py").write_text("before\n", encoding="utf-8")
        task = sg.ShadowTask(
            slug="t",
            repo="parse",
            source_file="m.py",
            original="before",
            defective="after",
            issue="i",
            target_test="t.py",
            suite_tests=("t.py",),
            behavior_probe="percent",
            defect_class="d",
        )
        assert sg.revert_defect(task, repo) is False
        assert (repo / "m.py").read_text(encoding="utf-8") == "before\n"


class TestVerdictHonesty:
    """A skipped lane is a skipped lane. A missing metric is not a pass."""

    @staticmethod
    def _shadow(kernel_rate: float, legacy_rate: float, **overrides) -> dict:
        def arm(rate: float) -> dict:
            return {
                "measured": 10,
                "blocked": 0,
                "correctness": int(10 * rate),
                "correctness_rate": rate,
                "verified_success": int(10 * rate),
            }

        report = {
            "repo_count": 5,
            "task_count": 10,
            "workspace_safety": {"verdict": "pass", "mutations": 0},
            "summary": {
                "default/kernel": arm(kernel_rate),
                "default/legacy": arm(legacy_rate),
            },
        }
        report.update(overrides)
        return report

    def test_a_skipped_lane_is_never_a_pass(self) -> None:
        lanes = [
            {"lane": "full_test_suite", "status": "skipped", "reason": "not selected"}
        ]
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0), {"verdict": "CLEAN"}, lanes, []
        )
        assert decision["verdict"] == "shadow_only"
        assert any("skipped" in reason for reason in decision["soft_reasons"])
        # The lane is REPORTED as skipped - named, not hidden, and not a pass.
        lanes = decision["derived_from"]["evidence_lanes"]
        assert lanes["full_test_suite"] == "skipped"

    def test_a_failing_lane_is_a_hard_block(self) -> None:
        lanes = [{"lane": "prompt_regression_check", "status": "fail", "exit_code": 1}]
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0), {"verdict": "CLEAN"}, lanes, []
        )
        assert decision["verdict"] == "blocked"
        assert any("failing evidence lanes" in r for r in decision["hard_reasons"])

    def test_a_less_correct_kernel_is_a_hard_block(self) -> None:
        decision = sg.cutover_decision(
            self._shadow(0.0, 1.0), {"verdict": "CLEAN"}, [], []
        )
        assert decision["verdict"] == "blocked"
        assert any("LESS correct" in reason for reason in decision["hard_reasons"])

    def test_an_unmeasurable_rate_is_a_hard_block_not_a_pass(self) -> None:
        shadow = self._shadow(1.0, 1.0)
        shadow["summary"]["default/kernel"] = {"measured": 0, "correctness_rate": None}
        shadow["summary"]["default/legacy"]["measured"] = 0
        decision = sg.cutover_decision(shadow, {"verdict": "CLEAN"}, [], [])
        assert decision["verdict"] == "blocked"
        assert any("not measurable" in reason for reason in decision["hard_reasons"])

    def test_unequal_measured_counts_are_a_hard_block(self) -> None:
        shadow = self._shadow(1.0, 1.0)
        shadow["summary"]["default/kernel"]["measured"] = 3
        decision = sg.cutover_decision(shadow, {"verdict": "CLEAN"}, [], [])
        assert decision["verdict"] == "blocked"
        assert any("not comparable" in reason for reason in decision["hard_reasons"])

    def test_a_dirty_reference_tree_is_a_hard_block(self) -> None:
        shadow = self._shadow(
            1.0, 1.0, workspace_safety={"verdict": "FAIL", "mutations": 1}
        )
        decision = sg.cutover_decision(shadow, {"verdict": "CLEAN"}, [], [])
        assert decision["verdict"] == "blocked"
        assert any("mutated" in reason for reason in decision["hard_reasons"])

    def test_too_few_repositories_or_tasks_is_never_a_cutover(self) -> None:
        shadow = self._shadow(1.0, 1.0, repo_count=3, task_count=4)
        decision = sg.cutover_decision(shadow, {"verdict": "CLEAN"}, [], [])
        assert decision["verdict"] == "shadow_only"
        assert any("repositories" in r for r in decision["soft_reasons"])
        assert any("real tasks" in r for r in decision["soft_reasons"])

    def test_a_dirty_daily_matrix_is_a_hard_block(self) -> None:
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0),
            {"verdict": "NOT_CLEAN", "failed": 1, "errored": 0},
            [],
            [],
        )
        assert decision["verdict"] == "blocked"
        assert any("daily-driver" in reason for reason in decision["hard_reasons"])

    def test_a_not_run_daily_matrix_is_never_a_pass(self) -> None:
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0), {"verdict": "NOT_RUN"}, [], []
        )
        assert decision["verdict"] == "blocked"

    def test_an_open_blocker_always_blocks(self) -> None:
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0),
            {"verdict": "CLEAN"},
            [],
            [{"id": "SG-99", "owner": "x", "file": "y", "next_action": "z"}],
        )
        assert decision["verdict"] == "blocked"
        assert decision["blockers"][0]["id"] == "SG-99"

    def test_a_perfect_matrix_with_nothing_skipped_is_a_cutover(self) -> None:
        decision = sg.cutover_decision(
            self._shadow(1.0, 1.0),
            {"verdict": "CLEAN"},
            [{"lane": "full_test_suite", "status": "pass"}],
            [],
        )
        assert decision["verdict"] == "cutover"
        assert decision["no_publish"]["committed"] is False
        assert decision["no_publish"]["published"] is False


class TestBlockerRecords:
    """Every blocker must be actionable, which is four fields and a repro."""

    def test_every_known_blocker_is_fully_owned(self) -> None:
        assert sg.KNOWN_BLOCKERS
        for blocker in sg.KNOWN_BLOCKERS:
            for field in (
                "id",
                "severity",
                "owner",
                "file",
                "title",
                "observed",
                "reproduction",
                "next_action",
            ):
                assert str(blocker.get(field, "")).strip(), (
                    f"{blocker.get('id')} missing {field}"
                )

    def test_blocker_ids_are_unique(self) -> None:
        ids = [blocker["id"] for blocker in sg.KNOWN_BLOCKERS]
        assert len(ids) == len(set(ids))

    def test_blockers_name_files_or_say_why_not(self) -> None:
        for blocker in sg.KNOWN_BLOCKERS:
            assert (
                ".py" in blocker["file"]
                or "no source file" in blocker["file"]
                or ".md" in blocker["file"]
            ), blocker["id"]


class TestWorkspaceSafety:
    """The tree digest is the safety claim, so the digest must be real."""

    def test_the_digest_changes_on_a_content_change(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        before = sg.tree_digest(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert sg.tree_digest(tmp_path) != before

    def test_the_digest_ignores_git_and_cache_directories(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        before = sg.tree_digest(tmp_path)
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"\x00junk")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
        assert sg.tree_digest(tmp_path) == before

    def test_a_rename_is_a_change(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        before = sg.tree_digest(tmp_path)
        (tmp_path / "a.py").rename(tmp_path / "b.py")
        assert sg.tree_digest(tmp_path) != before


class TestSuiteCommand:
    """A malformed suite command silently turns a real oracle into a block."""

    def test_a_k_expression_is_quoted(self) -> None:
        task = next(t for t in sg.SHADOW_TASKS if t.repo == "jpath")
        command = sg._sandbox_suite(task)
        assert '-k "' in command, command
        assert "not Symlink and not symlink" in command

    def test_pythonpath_is_carried_for_src_layouts(self) -> None:
        task = next(t for t in sg.SHADOW_TASKS if t.repo == "packaging")
        assert sg._sandbox_suite(task).startswith("env PYTHONPATH=src ")

    def test_every_deselected_node_carries_its_reason_in_the_manifest(self) -> None:
        jpath = sg.REPOS["jpath"]
        # Every deselect is a platform/plugin artifact, and the comment above
        # the tuple is the reason. A bare ignore with no reason is the thing
        # this gate exists to prevent.
        assert "tests/test_path.py::ruff" in jpath.deselect
        source = Path(sg.__file__).read_text(encoding="utf-8")
        assert "not a behavioural test" in source


class TestConfigProfile:
    """The two arms must differ in exactly the thing the report claims."""

    def test_the_two_arms_differ_only_in_strategy(self) -> None:
        task = sg.SHADOW_TASKS[0]
        legacy = sg._run_config(task, "legacy", "default")
        kernel = sg._run_config(task, "kernel", "default")
        differing = {
            k for k in set(legacy) | set(kernel) if legacy.get(k) != kernel.get(k)
        }
        assert differing == {"agent_strategy"}
        assert legacy["agent_strategy"] == "legacy_agent"
        assert kernel["agent_strategy"] == "daily"

    def test_the_comparison_profile_only_narrows_protected_paths(self) -> None:
        task = sg.SHADOW_TASKS[0]
        base = sg._run_config(task, "kernel", "default")
        relaxed = sg._run_config(task, "kernel", "comparison")
        differing = {
            k for k in set(base) | set(relaxed) if base.get(k) != relaxed.get(k)
        }
        assert differing == {"protected_paths"}
        assert "test_*.py" not in relaxed["protected_paths"]

    def test_both_arms_receive_the_declared_verifier(self) -> None:
        task = sg.SHADOW_TASKS[0]
        for arm in sg.ARMS:
            config = sg._run_config(task, arm, "default")
            assert config["verification_policy"]["target_test"] == task.target_test
            assert config["test_command"] == sg._sandbox_suite(task)

    def test_the_off_arms_are_not_pinned_by_the_probe(self) -> None:
        # Pinning agent_approval="never" would turn every read into an approval
        # prompt and the gate would measure the probe, not the product.
        task = sg.SHADOW_TASKS[0]
        config = sg._run_config(task, "kernel", "default")
        assert "agent_approval" not in config
        assert "knowledge_enabled" not in config


class TestDefaultPlacement:
    """The default run root must not sit inside a repository."""

    def test_the_default_out_root_is_outside_the_repository(self) -> None:
        root = sg.default_out_root().resolve()
        repo = Path(sg.__file__).resolve().parents[1]
        assert repo not in root.parents and root != repo

    def test_an_explicit_override_is_honoured(self, monkeypatch) -> None:
        monkeypatch.setenv("NEO_SHADOW_OUT_ROOT", "/tmp/neo-shadow-override")
        assert sg.default_out_root() == Path("/tmp/neo-shadow-override")

    def test_merge_records_what_it_replaced(self) -> None:
        base = {
            "generated_at": "t0",
            "runs": [
                {
                    "slug": "a",
                    "profile": "default",
                    "arm": "kernel",
                    "status": "blocked",
                    "block_kind": "",
                    "workspace_mutated": False,
                }
            ],
            "summary": {},
        }
        fresh = {
            "generated_at": "t1",
            "runs": [
                {
                    "slug": "a",
                    "profile": "default",
                    "arm": "kernel",
                    "status": "failed",
                    "block_kind": "arm_policy_refusal",
                    "workspace_mutated": False,
                }
            ],
        }
        merged = sg.merge_shadow_reports(base, fresh)
        assert merged["runs"][0]["status"] == "failed"
        assert merged["merged_from"]["replaced_records"] == 1
        assert merged["summary"]["default/kernel"]["policy_refusals"] == 1


class TestInvariants:
    """The five ceiling invariants, checked against the shipped code."""

    def test_completed_unverified_never_renders_as_success(self) -> None:
        from cli import runview

        terminal = runview.effective_terminal_status("completed_unverified", None)
        assert terminal != "completed_verified"
        assert terminal == "completed_unverified"

    def test_the_verifier_condition_is_still_in_the_source(self) -> None:
        import inspect

        from harness import core

        assert "target_test_passed and" in inspect.getsource(core)

    def test_the_tui_consumes_the_run_journal(self) -> None:
        import inspect

        from cli import tui

        source = inspect.getsource(tui)
        assert "runview" in source or "projection" in source
