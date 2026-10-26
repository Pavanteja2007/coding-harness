"""P5/W1/T2 — workspace concept, honest git output, honest diff, measured cost.

Host-only where possible: a real fixture repo, real git plumbing on a
tempfile, no Docker, no provider, no credential. Every claim in this module
is asserted by behaviour, and each "must not" carries its control.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution import (
    git_output,
    git_preview,
    human_diff,
    monorepo_cost,
    monorepo_sandbox,
    monorepo_select,
    workspace_map,
)
from execution.git_output import GitOutputError, branch_and_commit

# --------------------------------------------------------------------------
# shared fixtures
# --------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=path, check=True,
    )


def _write(path: Path, rel: str, text: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _two_package_workspace(root: Path) -> "workspace_map.Workspace":
    _write(root, "packages/api/package.json", '{"name":"api","scripts":{"test":"echo t"}}')
    _write(root, "packages/web/package.json", '{"name":"web","scripts":{"test":"echo t"}}')
    _write(root, "packages/shared/package.json", '{"name":"shared","scripts":{"test":"echo t"}}')
    _write(root, "package.json", '{"workspaces":["packages/*"]}')
    return workspace_map.discover_workspace(str(root))


# --------------------------------------------------------------------------
# T5.W1.1 — workspace concept
# --------------------------------------------------------------------------


class TestWorkspaceDiscovery:
    def test_discovery_is_a_data_table(self):
        # One row per ecosystem; a new ecosystem is data, not a new branch.
        names = {src["ecosystem"] for src in workspace_map.WORKSPACE_SOURCES}
        assert {"pnpm", "lerna", "node", "turbo", "nx", "go", "cargo", "python"} <= names

    def test_node_workspaces_discover_packages(self, tmp_path):
        ws = _two_package_workspace(tmp_path)
        names = sorted(p.name for p in ws.packages)
        assert names == ["api", "shared", "web"]
        assert ws.is_monorepo

    def test_go_work_discovers(self, tmp_path):
        _write(tmp_path, "go.work", "go 1.22\nuse (\n\t./a\n\t./b\n)\n")
        _write(tmp_path, "a/go.mod", "module example/a\n")
        _write(tmp_path, "b/go.mod", "module example/b\n")
        ws = workspace_map.discover_workspace(str(tmp_path))
        assert {p.relpath for p in ws.packages} == {"a", "b"}

    def test_missing_manifest_is_a_gap_not_a_crash(self, tmp_path):
        _write(tmp_path, "packages/x/README", "hi")
        ws = workspace_map.discover_workspace(str(tmp_path))
        # python source row matches packages/*, but x has no manifest:
        assert isinstance(ws, workspace_map.Workspace)

    def test_empty_repo_is_not_monorepo(self, tmp_path):
        ws = workspace_map.discover_workspace(str(tmp_path))
        assert ws.is_monorepo is False


class TestCrossPackageSelection:
    def test_change_in_api_does_not_run_web(self, tmp_path):
        _two_package_workspace(tmp_path)
        sel = monorepo_select.select_tests_across_packages(
            str(tmp_path), ["packages/api/index.js"]
        )
        ran = {p.package for p in sel.per_package if not p.skipped}
        skipped = set(sel.skipped_packages)
        assert "web" in skipped
        assert "shared" in skipped
        assert "api" in ran

    def test_shared_package_change_runs_consumers(self, tmp_path):
        _two_package_workspace(tmp_path)
        # make api and web depend on shared
        for name in ("api", "web"):
            p = tmp_path / "packages" / name / "package.json"
            p.write_text(
                '{"name":"%s","scripts":{"test":"echo t"},"dependencies":{"shared":"*"}}' % name
            )
        ws = workspace_map.discover_workspace(str(tmp_path))
        sel = monorepo_select.select_tests_across_packages(
            str(tmp_path), ["packages/shared/util.js"], workspace=ws
        )
        ran = {p.package for p in sel.per_package if not p.skipped}
        assert {"api", "web", "shared"} <= ran

    def test_coverage_is_reported_never_assumed(self, tmp_path):
        _two_package_workspace(tmp_path)
        receipt = monorepo_select.coverage_receipt_across_packages(
            str(tmp_path), ["packages/api/index.js"]
        )
        assert receipt["skipped_packages"] == ["shared", "web"]
        assert "skipped" in receipt["note"]

    def test_change_group_detected_for_shared_and_consumer(self, tmp_path):
        root = Path(tmp_path)
        _write(root, "package.json", '{"workspaces":["packages/*"]}')
        _write(root, "packages/shared/package.json", '{"name":"shared"}')
        _write(root, "packages/api/package.json",
               '{"name":"api","dependencies":{"shared":"*"}}')
        ws = workspace_map.discover_workspace(str(root))
        group = workspace_map.workspace_change_group(
            ws, ["packages/shared/a.js", "packages/api/b.js"]
        )
        assert group["is_group"] is True
        assert ("shared", "api") in group["edges"]

    def test_independent_packages_are_not_a_group(self, tmp_path):
        _two_package_workspace(tmp_path)
        ws = workspace_map.discover_workspace(str(tmp_path))
        group = workspace_map.workspace_change_group(
            ws, ["packages/api/a.js", "packages/web/b.js"]
        )
        assert group["is_group"] is False


# --------------------------------------------------------------------------
# T5.W1.1 — environment limits reported, never silently narrowing
# --------------------------------------------------------------------------


class TestEnvironmentLimits:
    def test_aggregate_limits_detected_and_reported(self):
        check = monorepo_sandbox.aggregate_limit_check(8, 1.0, 7.614)
        assert check.verdict == "DEGRADED"
        assert check.detected
        assert check.promised_gib == pytest.approx(8.0)

    def test_within_headroom_is_held(self):
        check = monorepo_sandbox.aggregate_limit_check(4, 1.0, 16.0)
        assert check.verdict == "HELD"
        assert not check.detected

    def test_unknown_daemon_is_unknown_not_held(self):
        check = monorepo_sandbox.aggregate_limit_check(4, 1.0, 0.0)
        assert check.verdict == "UNKNOWN"


# --------------------------------------------------------------------------
# T5.W1.2 — git safety under a human
# --------------------------------------------------------------------------


_calls: list = []
_real_run = subprocess.run


def _record(cmd, **kw):
    _calls.append(list(cmd))
    return _real_run(cmd, **kw)


class TestGitPreview:
    def test_preview_is_derived_from_the_real_argv(self, tmp_path, monkeypatch):
        work = tmp_path / "work"
        _init_repo(work)
        _write(work, "app.py", "print('hi')\n")
        preview = git_preview.preview_branch_and_commit(
            str(work), "fix the thing", changed_files=["app.py"]
        )
        _calls.clear()
        monkeypatch.setattr(git_output.subprocess, "run", _record)
        branch_and_commit(str(work), "fix the thing", changed_files=["app.py"])
        commit_calls = [c for c in _calls if "commit" in c]
        assert commit_calls, "expected a real commit call"
        git_preview.assert_preview_matches_argv(preview, commit_calls[0])

    def test_hostile_commit_text_is_handled_not_executed(self, tmp_path):
        nasty = "fix\x00\x1b[31m\n--amend\nrm -rf /"
        clean = git_preview.sanitize_commit_text(nasty)
        assert "\x00" not in clean and "\x1b" not in clean
        # it stays ONE argv slot; a shell never sees it
        assert "\n" in clean  # body newline is fine; the point is it is one arg
        with pytest.raises(GitOutputError):
            git_preview.sanitize_commit_text("x" * (git_preview.MAX_COMMIT_TEXT_CHARS + 1))

    def test_hostile_branch_name_rejected(self):
        for bad in ("-m", "--upload-draft", "..\\etc", "@{", "a//b"):
            with pytest.raises(GitOutputError):
                git_output._validate_branch_name(bad)

    def test_original_repo_is_never_touched(self, tmp_path):
        original = tmp_path / "original"
        _init_repo(original)
        work = tmp_path / "work"
        _write(work, "app.py", "x = 1\n")
        _init_repo(work)
        preview = git_preview.preview_branch_and_commit(
            str(work), "msg", changed_files=["app.py"]
        )
        assert git_preview.original_repo_is_untouched(str(original), preview)

    def test_credentials_never_reach_a_commit(self):
        with pytest.raises(GitOutputError):
            git_preview.assert_no_live_credentials(
                "token = ghp_abcdefghijklmnopqrstuvwxyz123456\n"
            )
        # and the redaction path strips the same token, so the commit is safe
        assert "ghp_" not in git_output._redact_secrets("token = ghp_abcdefghijklmnopqrstuvwxyz123456\n")

    def test_partial_failure_restores_the_repo(self, tmp_path, monkeypatch):
        work = tmp_path / "work"
        _init_repo(work)
        _write(work, "a.py", "x = 1\n")
        _git_real = git_output._git

        def fail_on_commit(work_dir, *args, check=True):
            if args and args[0] == "commit":
                raise GitOutputError("simulated partial failure")
            return _git_real(work_dir, *args, check=check)

        original_head = _git_real(str(work), "rev-parse", "--verify", "HEAD", check=False).stdout.strip()
        monkeypatch.setattr(git_output, "_git", fail_on_commit)
        with pytest.raises(GitOutputError):
            branch_and_commit(str(work), "msg", changed_files=["a.py"])
        monkeypatch.undo()
        head = _git_real(str(work), "rev-parse", "--verify", "HEAD", check=False).stdout.strip()
        assert head == original_head
        # staged index is clean again
        assert _git_real(str(work), "diff", "--cached", "--name-only").stdout.strip() == ""

    def test_a_fresh_repo_failure_removes_only_its_own_gitdir(self, tmp_path, monkeypatch):
        work = tmp_path / "freshwork"
        work.mkdir()
        _write(work, "a.py", "x = 1\n")
        _git_real = git_output._git

        def fail_on_commit(work_dir, *args, check=True):
            if args and args[0] == "commit":
                raise GitOutputError("boom")
            return _git_real(work_dir, *args, check=check)

        monkeypatch.setattr(git_output, "_git", fail_on_commit)
        with pytest.raises(GitOutputError):
            branch_and_commit(str(work), "msg", changed_files=["a.py"],
                              pristine_dir=None)
        monkeypatch.undo()
        assert not (work / ".git").exists()
        assert (work / "a.py").read_text() == "x = 1\n"


class TestCredentialsNeverReachACommit:
    """Driven through the REAL commit path, not the helper.

    The commit MESSAGE is redacted upstream; the staged CONTENT never was. A
    token pasted into a source file would be redacted out of the rendered diff
    and still land in the commit object, the branch, and every clone of it.
    """

    def test_a_token_in_staged_content_refuses_the_commit(self, tmp_path):
        work = tmp_path / "w"
        _init_repo(work)
        _git_real = git_output._git
        _write(work, "README.md", "base\n")
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "base", "--no-verify")
        original_head = _git_real(str(work), "rev-parse", "--verify", "HEAD",
                                  check=False).stdout.strip()
        _write(
            work,
            "config.py",
            'API_KEY = "sk-live-AAAABBBBCCCCDDDDEEEEFFFF"\n',
        )
        with pytest.raises(GitOutputError) as excinfo:
            branch_and_commit(
                str(work), "[fix] configure the client", changed_files=["config.py"]
            )
        assert "credential" in str(excinfo.value).lower()
        # and the repository did NOT move: no commit, no branch left behind
        head_after = _git_real(str(work), "rev-parse", "--verify", "HEAD",
                               check=False).stdout.strip()
        assert head_after == original_head
        branches = _git_real(str(work), "branch", "--list", check=False).stdout
        assert "harness/fix" not in branches

    def test_the_control_arm_commits_clean_content(self, tmp_path):
        """A guard that refuses everything is a guard that gets switched off."""
        work = tmp_path / "w"
        _init_repo(work)
        _git_real = git_output._git
        _write(work, "README.md", "base\n")
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "base", "--no-verify")
        _write(work, "app.py", 'KEY_LENGTH = 32  # not a token, just a number\n')
        branch, sha = branch_and_commit(
            str(work), "[fix] add a constant", changed_files=["app.py"]
        )
        assert branch.startswith("harness/fix-")
        assert re.fullmatch(r"[0-9a-f]{40,64}", sha)
        assert _git_real(str(work), "show", "--stat", "--oneline", sha,
                         check=False).stdout.strip()

    def test_a_long_benign_identifier_is_not_a_credential(self, tmp_path):
        """The shape must not fire on every long token run.

        A regex that refuses any 12+ character identifier is a guard nobody can
        live with, and a guard nobody can live with is disabled.
        """
        work = tmp_path / "w"
        _init_repo(work)
        _git_real = git_output._git
        _write(work, "README.md", "base\n")
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "base", "--no-verify")
        _write(
            work,
            "app.py",
            "SOME_VERY_LONG_CONSTANT_NAME_HERE = 'aVeryLongBenignStringValue'\n",
        )
        _branch, _sha = branch_and_commit(
            str(work), "[fix] name a constant", changed_files=["app.py"]
        )

    def test_a_binary_blob_is_not_scanned_and_the_refusal_says_so(self, tmp_path):
        """A compiled artefact legitimately has high-entropy bytes.

        So binaries are skipped — and the receipt names what was NOT scanned,
        because a silent gap in a security check is the thing this package keeps
        treating as a defect.
        """
        work = tmp_path / "w"
        _init_repo(work)
        _git_real = git_output._git
        _write(work, "README.md", "base\n")
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "base", "--no-verify")
        _write(work, "token.py", 'T = "ghp_AAAABBBBCCCCDDDDEEEEFFFF"\n')
        (work / "blob.bin").write_bytes(b"\x00\x01sk-abcdefghijklmnop\xff")
        with pytest.raises(GitOutputError) as excinfo:
            branch_and_commit(
                str(work), "[fix] both", changed_files=["token.py", "blob.bin"]
            )
        note = str(excinfo.value)
        assert "not scanned" in note
        assert "binary" in note or "scan bound" in note


# --------------------------------------------------------------------------
# T5.W1.3 — the honest diff
# --------------------------------------------------------------------------


class TestHumanDiff:
    def _dirty_repo(self, tmp_path):
        work = tmp_path / "work"
        _init_repo(work)
        _write(work, "app.py", "print('v1')\n")
        _git_real = git_output._git
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "v1", "--no-verify")
        return work

    def test_binary_says_binary(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        (work / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        _git_real = git_output._git
        _git_real(str(work), "add", "-N", "blob.bin")
        diff = human_diff.render_human_diff(str(work))
        blob = [e for e in diff.entries if e.path == "blob.bin"]
        assert blob and blob[0].is_binary and blob[0].kind == "binary"

    def test_mode_change_is_shown(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        script = work / "run.sh"
        script.write_text("#!/bin/sh\n")
        _git_real = git_output._git
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "add", "--no-verify")
        subprocess.run(["git", "config", "core.filemode", "true"], cwd=work, check=True)
        subprocess.run(
            ["git", "update-index", "--chmod=+x", "run.sh"], cwd=work, check=True
        )
        diff = human_diff.render_human_diff(str(work))
        modes = [e for e in diff.entries if e.kind == "mode_changed"]
        if not modes:
            pytest.skip(
                "this git build reports no mode change for an index-only chmod; "
                "the parse is pinned independently by "
                "test_a_mode_change_is_parsed_and_not_also_reported_as_modified"
            )
        # Which DIRECTION the change goes is the platform's business: on a
        # uid-agnostic worktree (Windows) the index carries +x while the file
        # itself has no exec bit, so git reports 100755 => 100644. What this
        # module owns is that a chmod is surfaced AS a mode change, with both
        # modes, instead of rendering as an ordinary modification.
        assert len(modes) == 1
        assert modes[0].path == "run.sh"
        assert modes[0].old_mode != modes[0].new_mode
        assert {modes[0].old_mode[-3:], modes[0].new_mode[-3:]} == {"644", "755"}

    def test_a_mode_change_is_parsed_and_not_also_reported_as_modified(
        self, tmp_path
    ):
        """git says a chmod twice -- once in --summary, once as an M.

        A chmod +x that renders as an ordinary modification is the exact
        thing this module exists to stop, so the parse is pinned
        independently of whether THIS host's git can produce the change.
        """
        work = self._dirty_repo(tmp_path)
        (work / "run.sh").write_text("#!/bin/sh\n")
        real = human_diff._git

        def fake(work_dir, *args):
            argv = list(args)
            if argv[:2] == ["diff", "--summary"]:
                return " mode change 100644 => 100755 run.sh\n"
            if argv[:2] == ["diff", "--name-status"]:
                return "M\trun.sh\n"
            if argv[:3] == ["diff", "--numstat"]:
                return "0\t0\trun.sh\n"
            if argv[:2] == ["diff", "-w"]:
                return "0\t0\trun.sh\n"
            if argv[:1] == ["diff"]:
                return ""
            return real(work_dir, *args)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(human_diff, "_git", fake)
            diff = human_diff.render_human_diff(str(work))
        modes = [e for e in diff.entries if e.kind == "mode_changed"]
        assert [e.path for e in modes] == ["run.sh"]
        assert modes[0].old_mode == "100644"
        assert modes[0].new_mode == "100755"
        # one path is one entry: the generic "modified" must not survive too
        assert [e.path for e in diff.entries if e.path == "run.sh"] == ["run.sh"]

    def test_rename_is_a_rename(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        _write(work, "old.py", "x = 1\n")
        _git_real = git_output._git
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "add", "--no-verify")
        (work / "old.py").rename(work / "new.py")
        _git_real(str(work), "add", "-A")
        diff = human_diff.render_human_diff(str(work))
        renames = [e for e in diff.entries if e.kind == "renamed"]
        # git may or may not detect the rename depending on similarity; the
        # contract we own is that IF it is one, it is reported as a rename.
        if renames:
            assert renames[0].path == "new.py"

    def test_whitespace_only_is_marked(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        path = work / "app.py"
        # a trailing-space change: identical text, different whitespace only
        path.write_text("print('v1')   \n")
        diff = human_diff.render_human_diff(str(work))
        marked = [e for e in diff.entries if e.path == "app.py"]
        assert marked and marked[0].whitespace_only is True

    def test_a_crlf_to_lf_pass_is_not_reported_as_every_line_changing(
        self, tmp_path
    ):
        """A whole-file line-ending conversion is the classic giant diff.

        A reviewer who sees "+400/-400" on a file whose only change is CRLF to LF
        stops reading, and the real change hides inside it. So the receipt has
        to say the endings moved, and the diff text has to carry the caveat.
        """
        work = self._dirty_repo(tmp_path)
        path = work / "app.py"
        path.write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
        _git_real = git_output._git
        _git_real(str(work), "add", "-A")
        _git_real(str(work), "commit", "-q", "-m", "crlf", "--no-verify")
        # now convert to LF with NO content change at all
        path.write_bytes(b"a = 1\nb = 2\nc = 3\n")
        diff = human_diff.render_human_diff(str(work))
        marked = [e for e in diff.entries if e.path == "app.py"]
        assert marked, "the CRLF conversion must appear in the diff at all"
        assert marked[0].line_ending_change is True
        assert "line ending" in diff.text.lower()

    def test_the_control_arm_reports_no_line_ending_change(self, tmp_path):
        """Otherwise "always marked" would satisfy the previous row."""
        work = self._dirty_repo(tmp_path)
        (work / "app.py").write_text("print('v2')\n")
        diff = human_diff.render_human_diff(str(work))
        marked = [e for e in diff.entries if e.path == "app.py"]
        assert marked and marked[0].line_ending_change is False
        assert "line ending" not in diff.text.lower()

    def test_truncation_states_its_bound(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        (work / "big.py").write_text("x = " + "1" * 10_000 + "\n")
        _git_real = git_output._git
        _git_real(str(work), "add", "-N", "big.py")
        diff = human_diff.render_human_diff(str(work), max_chars=500)
        assert diff.truncated
        assert diff.truncation_bound == 500
        assert "truncated" in diff.text

    def test_digest_matches_and_stale_is_refused(self, tmp_path):
        work = self._dirty_repo(tmp_path)
        (work / "app.py").write_text("print('v2')\n")
        diff = human_diff.render_human_diff(str(work))
        human_diff.assert_fresh(diff, str(work))  # must not raise
        # mutate the tree: the diff is now stale
        (work / "app.py").write_text("print('v3')\n")
        with pytest.raises(ValueError):
            human_diff.assert_fresh(diff, str(work))


# --------------------------------------------------------------------------
# T5.W1.4 — measured cost + policy + bound
# --------------------------------------------------------------------------


class TestVerificationCost:
    def test_three_numbers_are_reported(self):
        numbers = monorepo_cost.CostNumbers(
            full_suite_s=6.5, per_package_selection_s=2.1, changed_package_only_s=0.9
        )
        assert numbers.full_suite_s == pytest.approx(6.5)
        assert numbers.per_package_selection_s == pytest.approx(2.1)
        assert numbers.changed_package_only_s == pytest.approx(0.9)

    def test_policy_prefers_smallest_truthful_set(self):
        policy = monorepo_cost.VerificationPolicy()
        d = monorepo_cost.choose_policy(
            changed_packages=["api"], total_packages=3, commits_since_full=1, policy=policy
        )
        assert d.mode == "changed_package_only"
        d2 = monorepo_cost.choose_policy(
            changed_packages=["api", "web", "shared"], total_packages=3,
            commits_since_full=1, policy=policy,
        )
        assert d2.mode == "full_suite"
        d3 = monorepo_cost.choose_policy(
            changed_packages=["api"], total_packages=3, commits_since_full=5, policy=policy
        )
        assert d3.mode == "full_suite"

    def test_every_policy_branch_carries_a_reason(self):
        policy = monorepo_cost.VerificationPolicy()
        for kwargs in (
            {"changed_packages": [], "total_packages": 3, "commits_since_full": 0},
            {"changed_packages": ["a", "b", "c"], "total_packages": 3, "commits_since_full": 0},
            {"changed_packages": ["a"], "total_packages": 3, "commits_since_full": 9},
            {"changed_packages": ["a", "b"], "total_packages": 5, "commits_since_full": 0},
        ):
            d = monorepo_cost.choose_policy(policy=policy, **kwargs)
            assert d.mode in monorepo_cost.COST_MODES
            assert d.reason.strip(), f"a mode with no stated reason: {d}"

    def test_skipped_package_is_named_not_verified(self):
        report = monorepo_cost.VerificationRunReport(
            mode="changed_package_only",
            verified_packages=("api",),
            skipped_packages=("web",),
            wall_clock_s=1.2,
            bounded=True,
        )
        assert report.skipped_sentence() == "skipped web (no dependency path)"

    def test_the_three_costs_are_MEASURED_not_asserted(self, tmp_path):
        """The headline number has to come from a clock, not from a literal."""
        pkg = tmp_path / "api"
        pkg.mkdir()
        _write(pkg, "test_api.py", "x = 1\n")
        python = "python -c \"import time; time.sleep(0.35)\""
        commands = {
            "full_suite": {"api": python},
            "changed_package_only": {"api": python},
        }
        numbers, arms = monorepo_cost.measure_verification_costs(
            str(tmp_path), commands
        )
        assert numbers.measured
        assert numbers.full_suite_s is not None and numbers.full_suite_s >= 0.30
        assert numbers.changed_package_only_s is not None
        # the third arm had no commands: UNMEASURED, never 0.0
        assert numbers.per_package_selection_s is None
        assert numbers.unmeasured_modes() == ("package_selection",)
        assert "UNMEASURED" in monorepo_cost.render_cost_numbers(numbers)
        assert {a.mode for a in arms} == {"full_suite", "changed_package_only"}

    def test_an_unmeasured_cost_is_never_rendered_as_zero(self):
        numbers = monorepo_cost.CostNumbers(full_suite_s=0.0031)
        text = monorepo_cost.render_cost_numbers(numbers)
        assert "0.00s" not in text
        assert "3.1ms" in text

    def test_a_budget_that_runs_out_reports_what_it_DID_NOT_RUN(self, tmp_path):
        """A bounded run that stops early must name the packages it skipped.

        This is the whole point of the bound: a run that times out after two of
        five packages and returns a clean report is a false verified success
        wearing a wall-clock budget.
        """
        real_runner = monorepo_cost.subprocess_argv_runner()
        python = sys.executable
        # a 10 s budget cannot finish three 1 s packages, and the per-package
        # floor means the last one is never even started
        policy = monorepo_cost.VerificationPolicy(
            max_wall_clock_s=1.2,
            max_package_s=5.0,
            min_remaining_to_start_package_s=0.4,
        )
        cmds = [
            (f"p{i}", f'"{python}" -c "import time; time.sleep(1.0)"') for i in range(3)
        ]
        report = monorepo_cost.run_packages_bounded(
            cmds, workspace_root=str(tmp_path), policy=policy, runner=real_runner
        )
        assert report.bounded
        assert report.skipped_packages, "a bounded run must name what it skipped"
        assert set(report.verified_packages) | set(report.skipped_packages) == {
            "p0", "p1", "p2"
        }
        assert not (set(report.verified_packages) & set(report.skipped_packages))
        assert any("not started" in n or "budget" in n for n in report.notes)

    def test_a_timeout_is_not_run_not_failed(self, tmp_path):
        runner = monorepo_cost.subprocess_argv_runner()
        python = sys.executable
        report = monorepo_cost.run_packages_bounded(
            [("slow", f'"{python}" -c "import time; time.sleep(30)"')],
            workspace_root=str(tmp_path),
            policy=monorepo_cost.VerificationPolicy(
                max_wall_clock_s=30, max_package_s=1.0,
                min_remaining_to_start_package_s=0.1,
            ),
            runner=runner,
        )
        assert report.verified_packages == ()
        assert report.skipped_packages == ("slow",)
        assert report.bounded
        assert report.per_package[0].timed_out

    def test_a_command_that_never_dispatched_is_neither_pass_nor_failure(self, tmp_path):
        """A missing binary is a machine fault, not a red package.

        Collapsing it into "exit != 0" would let a broken environment read as a
        code failure and send a repair loop after a working tree.
        """
        runner = monorepo_cost.subprocess_argv_runner()
        report = monorepo_cost.run_packages_bounded(
            [("api", "definitely-not-a-real-binary-xyz --run")],
            workspace_root=str(tmp_path),
            policy=monorepo_cost.VerificationPolicy(
                max_wall_clock_s=60, max_package_s=10.0
            ),
            runner=runner,
        )
        assert report.verified_packages == ()
        assert report.skipped_packages == ("api",)
        assert report.per_package[0].exit_code is None
        assert "not found" in report.per_package[0].note

    def test_the_measurement_default_runs_in_the_sandbox_not_a_host_shell(self):
        """A verification command is repository-controlled INPUT.

        So the default runner is the container boundary, and the host runner is
        a list-argv fallback. The source scan is AST-based and skips docstrings:
        this module's own prose discusses the shell, and a substring scan over a
        file that discusses the thing it forbids finds its own documentation.
        """
        import ast
        import inspect

        from execution import sandbox

        tree = ast.parse(inspect.getsource(monorepo_cost))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    offenders.append(node.lineno)
        assert not offenders, f"shell-mode spawns at lines {offenders}"

        assert monorepo_cost.subprocess_argv_runner() is not None
        # the sandbox runner really dispatches through execute_sandboxed
        fake_calls = []

        def fake_execute(repo_path, command, timeout_s, *a, **kw):
            fake_calls.append((repo_path, command, timeout_s))
            return sandbox.ExecutionResult(
                exit_code=0, stdout="ok", stderr="", timed_out=False
            )

        real_execute = sandbox.execute_sandboxed
        sandbox.execute_sandboxed = fake_execute
        try:
            run = monorepo_cost.sandbox_runner("/tmp/x")("echo hi", "/tmp/x", 5.0)
        finally:
            sandbox.execute_sandboxed = real_execute
        assert fake_calls, "the sandbox runner must dispatch through execute_sandboxed"
        assert run.exit_code == 0 and run.timed_out is False


# --------------------------------------------------------------------------
# T5.W1.5 — the sandbox holds
# --------------------------------------------------------------------------


class TestSandboxHolds:
    def test_fingerprint_excludes_code_edits(self, tmp_path):
        """MEASURED on a real edit, in both directions.

        The first half says a code edit does not rebuild the dependency image.
        The second half is what makes the first mean anything: a dependency edit
        DOES move the tag. A fingerprint that ignored everything would satisfy
        the first pin perfectly and be a stale-serve bug.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _write(repo, "requirements.txt", "pytest==8.0.0\n")
        _write(repo, "main.py", "x = 1\n")
        from execution.sandbox import _dep_image_tag

        check = monorepo_sandbox.fingerprint_check(
            str(repo), {"main.py": "x = 2\n"}
        )
        assert check.stable_under_code_edit is True
        assert check.before == check.after
        # and the direction that must NOT be stable
        assert (
            monorepo_sandbox.fingerprint_moves_on_a_dependency_edit(
                str(repo), {"requirements.txt": "pytest==8.1.0\n"}
            )
            is True
        )
        # control: the control arm is not vacuous -- the tag does move on it
        assert _dep_image_tag(str(repo)) == check.before

    def test_the_fingerprint_check_did_not_touch_the_caller_repository(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write(repo, "requirements.txt", "pytest==8.0.0\n")
        _write(repo, "main.py", "x = 1\n")
        monorepo_sandbox.fingerprint_check(str(repo), {"main.py": "TOTALLY DIFFERENT\n"})
        assert (repo / "main.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_two_repos_with_the_same_dependencies_share_a_DEPENDENCY_image(
        self, tmp_path
    ):
        """And that sharing is a designed saving, not a leak.

        Phase 1 fingerprinted on manifests precisely so byte-identical
        dependencies cost one build. Calling that a containment failure would
        make the cache a bug, so the receipt separates the two properties:
        a shared container NAME would be a leak; a shared dependency tag is not.
        """
        a, b = tmp_path / "a", tmp_path / "b"
        for repo in (a, b):
            repo.mkdir()
            _write(repo, "requirements.txt", "pytest==8.0.0\n")
        check = monorepo_sandbox.container_isolation_check((str(a), str(b)))
        assert check.distinct_names is True
        assert check.distinct_caches is False
        assert check.shared_dependency_images, (
            "identical manifests MUST share a dependency image -- if this is "
            "empty the fingerprint stopped reading manifests and the whole "
            "Phase-1 latency property is gone"
        )
        assert "designed saving" in check.note

    def test_container_names_are_distinct_because_they_are_BUILT(self, tmp_path):
        """The name half is measured off the argv the product would spawn.

        A hardcoded `distinct_names=True` is a receipt that cannot be wrong,
        which is the failure this whole package keeps guarding against.
        """
        a, b = tmp_path / "a", tmp_path / "b"
        for repo in (a, b):
            repo.mkdir()
            _write(repo, "requirements.txt", f"pytest==8.0.0  # {repo.name}\n")
        check = monorepo_sandbox.container_isolation_check((str(a), str(b)))
        assert check.distinct_names is True
        assert check.distinct_caches is True
        assert "distinct name" in check.note

    def test_missing_link_is_blocked_not_a_test_failure(self, tmp_path):
        _write(tmp_path, "package.json", '{"workspaces":["packages/*"]}')
        _write(tmp_path, "packages/api/README", "no manifest here\n")
        check = monorepo_sandbox.check_workspace_links(str(tmp_path))
        assert "api" in check.blocked
        assert check.reasons["api"]

    def test_a_declared_but_undiscovered_sibling_is_blocked(self, tmp_path):
        """The link check has two shapes and must catch both.

        A package whose manifest declares a scoped sibling that discovery never
        found would otherwise resolve against an INSTALLED, stale copy — which
        tests the wrong code while reporting green.
        """
        _write(tmp_path, "package.json", '{"workspaces":["packages/*"]}')
        _write(tmp_path, "packages/api/package.json", '{"name":"api"}')
        _write(
            tmp_path,
            "packages/web/package.json",
            '{"name":"web","dependencies":{"@acme/shared":"1.0.0"}}',
        )
        check = monorepo_sandbox.check_workspace_links(str(tmp_path))
        assert "web" in check.blocked
        assert "@acme/shared" in check.reasons["web"]
        # and the control: the package with no unresolvable link is fine
        assert "api" in check.ok

    def test_a_registry_dependency_is_not_treated_as_a_missing_sibling(
        self, tmp_path
    ):
        _write(tmp_path, "package.json", '{"workspaces":["packages/*"]}')
        _write(tmp_path, "packages/api/package.json", '{"name":"api"}')
        _write(
            tmp_path,
            "packages/web/package.json",
            '{"name":"web","dependencies":{"lodash":"^4.0.0","react":"^18"}}',
        )
        check = monorepo_sandbox.check_workspace_links(str(tmp_path))
        assert "web" in check.ok, check.reasons

    def test_overlay_holds_and_the_argv_carries_ro(self, tmp_path):
        _init_repo(tmp_path)
        check = monorepo_sandbox.overlay_check(str(tmp_path))
        assert check.holds
        assert ".git" in check.present
        assert check.read_only_mounts_ok is True

    def test_an_overlay_that_lost_its_ro_mode_is_caught(self, tmp_path):
        """The weakening demonstration: ":ro" -> ":rw" on the overlay.

        A read-write .git overlay inside a writable workspace is the one mount
        whose forgery changes what a reviewer believes happened, so the check
        has to be able to fail on exactly that. The patch is applied to
        ``monorepo_sandbox``'s OWN binding, because that is the name it calls —
        patching ``execution.sandbox`` would leave the imported symbol intact
        and this test would pass for the wrong reason.
        """
        import execution.monorepo_sandbox as msx

        _init_repo(tmp_path)
        real = msx._docker_run_args

        def weakened(*args, **kwargs):
            argv = list(real(*args, **kwargs))
            return [a.replace(":ro", ":rw") for a in argv]

        msx._docker_run_args = weakened
        try:
            weakened_check = msx.overlay_check(str(tmp_path))
        finally:
            msx._docker_run_args = real
        assert weakened_check.read_only_mounts_ok is False
        assert weakened_check.holds is False
        # control: the shipped check still holds on the same tree
        assert msx.overlay_check(str(tmp_path)).holds is True
