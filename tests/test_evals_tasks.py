"""Eval task-set invariants (the eval harness's own guardrails).

The eval harness is only trustworthy if its task SET is: every task
genuinely fails pre-fix, the scripted replies obey the step-session
contract (ONE bash command per reply — a two-command reply gets its
second command silently dropped by _extract_command, which is exactly
the false-negative class the 20260910-223216 run caught in
eval_lint_undefined), and every slug is unique so arms compare like
with like.

These tests are host-side and Docker-less: they check the task DICTS,
not the loop (the arms + --check cover that).
"""

import shutil
from pathlib import Path

import pytest

from evals import tasks as eval_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_tasks(tmp_path_factory):
    """The full task set, built into a scratch tree once for the module."""
    root = tmp_path_factory.mktemp("eval-tasks")
    yield eval_tasks.all_tasks(root)
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# structure invariants
# ---------------------------------------------------------------------------


def test_all_tasks_count_and_unique_slugs(built_tasks):
    slugs = [t["slug"] for t in built_tasks]
    assert len(slugs) == len(set(slugs)), "duplicate eval task slugs"
    # 5 fixtures + 4 synthesized + repair/docs/lint/fetch/skills scenarios
    assert len(slugs) == 14
    for prefix in ("bug0", "eval_"):
        assert any(s.startswith(prefix) for s in slugs)


def test_every_task_has_required_keys(built_tasks):
    for t in built_tasks:
        for key in ("slug", "repo", "issue", "target", "script"):
            assert t.get(key), f"{t.get('slug')}: missing {key}"
        assert Path(t["repo"]).is_dir(), f"{t['slug']}: repo not built"
        plan = t["script"]["plan"]
        assert isinstance(plan, list) and plan, f"{t['slug']}: empty plan"


def test_scenario_tasks_declare_retry_expectations(built_tasks):
    by_slug = {t["slug"]: t for t in built_tasks}
    for slug in ("eval_repair_retry", "eval_lint_undefined"):
        assert by_slug[slug].get("expects_retry") is True, (
            f"{slug}: must declare expects_retry (its whole point is the "
            f"repair loop carrying feedback to a second attempt)"
        )


# ---------------------------------------------------------------------------
# THE bug class the interrupted session caught: script replies must be
# ONE command each (the step contract). A multi-line reply is beheaded
# by _extract_command — the dropped second sed silently failed the task.
# ---------------------------------------------------------------------------

_CONTROL_LINES = {"SUBMIT", "ABORT"}


def _assert_single_command(replies, where):
    for reply in replies:
        assert isinstance(reply, str), f"{where}: reply not a string"
        stripped = reply.strip()
        if stripped in _CONTROL_LINES:
            continue
        if stripped.startswith(("DOCS ", "RECALL ", "FETCH ")):
            continue
        lines = [ln for ln in stripped.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"{where}: scripted reply spans {len(lines)} lines — the step "
            f"contract is ONE command per turn; _extract_command drops the "
            f"rest: {stripped[:120]!r}"
        )
        # a fenced block is one legitimate multi-line command shape
        if stripped.startswith("```"):
            continue


def test_scripted_replies_are_single_commands(built_tasks):
    for t in built_tasks:
        scripts = t["script"]["scripts"]
        where = f"{t['slug']}"
        for sid, attempts in scripts.items():
            # accept flat (single attempt) and per-attempt list shapes
            if attempts and isinstance(attempts[0], str):
                _assert_single_command(attempts, f"{where} step {sid}")
            else:
                for i, attempt in enumerate(attempts):
                    _assert_single_command(
                        attempt, f"{where} step {sid} attempt {i + 1}"
                    )


# ---------------------------------------------------------------------------
# the arms the runner ships must map onto real config keys
# ---------------------------------------------------------------------------


def test_runner_arms_use_real_config_keys():
    from evals.run import _ROUND_KEYS, ARMS
    from harness.config import DEFAULTS

    for key in _ROUND_KEYS:
        assert key in DEFAULTS, f"eval arm key {key} not in harness DEFAULTS"
    for arm, overrides in ARMS.items():
        for key in overrides:
            assert key in DEFAULTS, f"arm {arm} overrides unknown config key {key}"


def test_arm_set_covers_round_features():
    from evals.run import _ROUND_KEYS, ARMS

    # pre_round must toggle every round feature off at once
    assert all(ARMS["pre_round"].get(k) is False for k in _ROUND_KEYS)
    # each single-feature arm toggles exactly its own keys
    assert ARMS["no_memory"] == {"plan_with_memory": False}
    assert ARMS["no_docs"] == {"docs_lookup_enabled": False}
    assert ARMS["no_agent_tests"] == {"agent_tests": False}
    assert ARMS["no_webfetch"] == {"web_fetch_enabled": False}
    assert ARMS["no_skills"] == {"skills_enabled": False}


# ---------------------------------------------------------------------------
# repos built fresh: the runner must never inherit a polluted repo
# (the 20260910-222007 false pass: a prior run's fix sat in the shared
# synthesized repo, so the task "passed" with zero attempts)
# ---------------------------------------------------------------------------


def test_synthesized_repos_are_buggy_pre_fix(built_tasks):
    """Fails pre-fix on the BUILT repo — checked with the task's own
    target where possible (host pytest, no Docker)."""
    import subprocess
    import sys

    for t in built_tasks:
        if t["slug"] in (
            "bug01_wrap",
            "bug02_mean",
            "bug03_stack",
            "bug04_nameerror",
            "bug05_cart",
        ):
            continue  # committed fixtures; --check covers them
        cp = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                t["target"],
            ],
            cwd=str(t["repo"]),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert cp.returncode != 0, (
            f"{t['slug']}: target already green pre-fix — repo polluted "
            f"by a prior run (the false-pass class)"
        )


def test_rebuild_is_deterministic(built_tasks):
    """Building the same set twice yields byte-identical module sources —
    the runner's determinism claim (same repos every run)."""
    import tempfile

    by_slug = {t["slug"]: t for t in built_tasks}
    with tempfile.TemporaryDirectory() as td:
        fresh = eval_tasks.all_tasks(Path(td))
        for t in fresh:
            old_repo = Path(by_slug[t["slug"]]["repo"])
            for mod in old_repo.rglob("*.py"):
                rel = mod.relative_to(old_repo)
                twin = Path(t["repo"]) / rel
                assert twin.is_file(), f"{t['slug']}: rebuilt repo missing {rel}"
                assert twin.read_text(encoding="utf-8") == mod.read_text(
                    encoding="utf-8"
                ), f"{t['slug']}: {rel} differs across builds"
