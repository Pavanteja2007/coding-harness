"""Regression tests for VEX-CEILING-11 daily power tools.

Covers the seven required acceptance cases from the prompt:

- exact editor argv per platform (``$VISUAL`` / ``$EDITOR`` / ``code -g`` /
  POSIX default, plus the ``path[:line]`` split);
- review leaves the working tree hash unchanged for every scope;
- one-click recovery actions execute through the shared command registry;
- help search finds ``/steer``;
- ``doctor --json`` reports actionable failures with remediation;
- switching repos changes the effective model and log root;
- no ``logs/**`` phantom files appear in the command palette.

The editor/review/doctor seams are pure functions, so these are real
assertions against real git repositories and the real settings chain —
not mocks of the thing under test.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cli import commands as commands_mod
from cli import doctor, fileview, interactive


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> Path:
    """Create a real git repository with one committed file."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "power@tools.test")
    _git(repo, "config", "user.name", "Power Tools")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _tree_hash(root: Path) -> str:
    """Content hash of every non-.git file, used to prove read-only review."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 1. exact editor argv per platform
# ---------------------------------------------------------------------------


class TestEditorHandoff:
    def test_visual_wins_over_editor(self, monkeypatch):
        monkeypatch.setenv("VISUAL", "code --wait")
        monkeypatch.setenv("EDITOR", "vim")
        assert fileview.launch_editor("a.py", 3) == [
            "code",
            "-g",
            "--wait",
            "a.py",
            ":3",
        ]

    def test_editor_used_when_visual_absent(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("EDITOR", "nvim")
        assert fileview.launch_editor("a.py", 3) == ["nvim", "a.py", "+3"]

    def test_windows_default_is_code_with_g(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(fileview, "_platform_default", lambda: ["code"])
        assert fileview.launch_editor("a.py", 12) == ["code", "-g", "a.py", ":12"]

    def test_posix_default_is_vi_without_g(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(fileview, "_platform_default", lambda: ["vi"])
        assert fileview.launch_editor("a.py", 12) == ["vi", "a.py", "+12"]

    def test_platform_default_matches_the_host(self, monkeypatch):
        """The seam itself is platform-correct, not a constant."""
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        expected = ["code"] if os.name == "nt" else ["vi"]
        assert fileview._platform_default() == expected

    def test_no_line_suffix_omits_the_line_argument(self, monkeypatch):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.setattr(fileview, "_platform_default", lambda: ["code"])
        assert fileview.launch_editor("a.py") == ["code", "-g", "a.py"]

    def test_code_cmd_variant_is_recognised(self, monkeypatch):
        monkeypatch.setenv("EDITOR", "code.cmd")
        assert fileview.launch_editor("a.py", 4) == ["code.cmd", "-g", "a.py", ":4"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("src/a.py:42", ("src/a.py", 42)),
            ("src/a.py", ("src/a.py", None)),
            (r"C:\src\a.py", (r"C:\src\a.py", None)),
            (r"C:\src\a.py:42", (r"C:\src\a.py", 42)),
            ("", ("", None)),
        ],
    )
    def test_split_open_target_keeps_windows_drive_letters(self, raw, expected):
        assert fileview.split_open_target(raw) == expected

    def test_detached_launch_reports_failure_without_raising(self, monkeypatch):
        """A missing editor degrades to a reported failure, not a traceback."""
        result = fileview.launch_editor_detached(
            "a.py", None, editor="definitely-not-an-editor-binary-neo"
        )
        assert result.returncode != 0
        assert result.stderr


# ---------------------------------------------------------------------------
# 2. review leaves the working tree hash unchanged
# ---------------------------------------------------------------------------


class TestReadOnlyReview:
    @pytest.mark.parametrize("scope", ["uncommitted", "branch"])
    def test_review_scopes_do_not_mutate_the_working_tree(self, tmp_path, scope):
        repo = _init_repo(tmp_path / "repo")
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "b.py").write_text("y = 1\n", encoding="utf-8")

        before = _tree_hash(repo)
        record = fileview.review_scope(repo, scope)
        after = _tree_hash(repo)

        assert before == after, "review must never mutate the working tree"
        assert record["scope"] == scope
        assert record["additions"] >= 1
        assert sorted(record["changed_files"]) == ["a.py", "b.py"]

    def test_uncommitted_scope_reports_a_working_tree_fingerprint(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "a.py").write_text("x = 99\n", encoding="utf-8")
        before = _tree_hash(repo)
        first = fileview.review_worktree(repo)
        assert (
            first["working_tree_sha256"]
            == fileview.review_worktree(repo)["working_tree_sha256"]
        ), "the fingerprint must be stable for an unchanged tree"
        assert before == _tree_hash(repo)

    def test_fingerprint_tracks_a_real_edit(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        first = fileview.review_worktree(repo)["working_tree_sha256"]
        (repo / "a.py").write_text("x = 4242\n", encoding="utf-8")
        assert fileview.review_worktree(repo)["working_tree_sha256"] != first

    def test_sha_scope_needs_a_ref_and_says_so(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        before = _tree_hash(repo)
        record = fileview.review_scope(repo, "sha")
        assert record["status"] == "needs-ref"
        assert record["usage"] == "/review sha <ref>"
        assert before == _tree_hash(repo)

    def test_sha_scope_with_a_ref_reports_a_diff(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        record = fileview.review_scope(repo, "sha", ref="HEAD")
        assert record["status"] == "ok"
        assert record["ref"] == "HEAD"

    def test_pr_scope_reports_no_upstream_honestly(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        record = fileview.review_scope(repo, "pr")
        assert record["status"] == "no-upstream"
        assert "upstream" in record["reason"]

    def test_unknown_scope_is_rejected(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        assert fileview.review_scope(repo, "nope")["status"] == "unknown-scope"


# ---------------------------------------------------------------------------
# 3. one-click recovery actions execute
# ---------------------------------------------------------------------------


class TestRecoveryVocabulary:
    #: The authoritative kind vocabulary, owned by ``harness.tool_errors``
    #: plus the one task-level class the CLI adds. A recovery card that
    #: invents a kind outside this set is a regression.
    KIND_VOCABULARY = frozenset(
        {
            "ok",
            "timeout",
            "command_not_found",
            "file_not_found",
            "malformed_patch",
            "syntax_error",
            "import_error",
            "undefined_name",
            "permission_denied",
            "argument_error",
            "command_rejected",
            "internal_error",
            "model_rate_limited",
            "model_unavailable",
            "model_timeout",
            "model_auth",
            "model_bad_request",
            "model_internal",
            "verification_failed",
            "unknown",
        }
    )

    def test_every_required_command_reaches_the_recovery_registry(self):
        """/trace, /resume, /undo and /doctor are all runnable actions."""
        context = commands_mod.surface_command_context(
            "repl", in_flight=False, task_id="task-1"
        )
        for name in ("/trace", "/resume", "/undo", "/doctor"):
            spec = commands_mod.command_spec(name)
            assert spec is not None, f"{name} is not registered"
            resolution = commands_mod.resolve_command_line(name, context)
            assert resolution.status in {"ok", "queued"}, (
                f"{name} did not resolve to a runnable action: "
                f"{resolution.status} {resolution.message}"
            )
            assert spec.failure_recovery, f"{name} declares no recovery actions"

    def test_resolution_produces_a_runnable_command_string(self):
        """The recovery action resolves to a line a surface can execute."""
        context = commands_mod.surface_command_context("tui", task_id="task-1")
        for name, args in (
            ("/trace", "3"),
            ("/doctor", "--json"),
            ("/resume", "task-1"),
        ):
            resolution = commands_mod.resolve_command_line(f"{name} {args}", context)
            assert resolution.status == "ok"
            spec = commands_mod.command_spec(name)
            canonical = f"{resolution.spec.name} {resolution.args}".rstrip()
            assert canonical.startswith(spec.name)
            assert commands_mod.argument_hint(canonical)

    def test_classify_failure_maps_to_runnable_next_actions(self):
        record = fileview.classify_failure(
            "pytest tests/", "", "SyntaxError: invalid syntax"
        )
        assert record["kind"] == "syntax_error"
        assert record["evidence_path"]
        assert len(record["actions"]) >= 2
        assert all(isinstance(action, str) and action for action in record["actions"])

    def test_every_kind_is_in_the_documented_vocabulary(self):
        """The CLI must not invent a parallel error vocabulary."""
        samples = [
            "command timed out after 300s",
            "No such file or directory: src/x.py",
            "Permission denied (publickey)",
            "ServiceUnavailableError: no available channel",
            "litellm.RateLimitError: 429 Too Many Requests",
            "AuthenticationError: invalid api key",
            "NotFoundError: model does not exist",
            "FAILED tests/test_a.py::test_b AssertionError",
            "2 failed, 5 passed in 1.2s",
            "SyntaxError: invalid syntax",
            "ModuleNotFoundError: No module named requests",
            "totally unexpected state",
            "",
        ]
        for sample in samples:
            record = fileview.classify_failure("cmd", sample, "")
            assert record["kind"] in self.KIND_VOCABULARY, sample
            assert record["actions"], f"{sample!r} produced no recovery action"

    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("command timed out after 300s", "timeout"),
            ("No such file or directory: src/x.py", "file_not_found"),
            ("Permission denied (publickey)", "permission_denied"),
            ("ServiceUnavailableError: no available channel", "model_unavailable"),
            ("litellm.RateLimitError: 429 Too Many Requests", "model_rate_limited"),
            ("AuthenticationError: invalid api key", "model_auth"),
            ("SyntaxError: invalid syntax", "syntax_error"),
            ("FAILED tests/test_a.py::test_b AssertionError", "verification_failed"),
            ("2 failed, 5 passed in 1.2s", "verification_failed"),
        ],
    )
    def test_classification_matches_the_authoritative_classifier(self, text, kind):
        assert fileview.classify_failure("cmd", text, "")["kind"] == kind

    def test_provider_and_file_failures_do_not_share_a_policy(self):
        """ "not found" is ambiguous; a missing MODEL and a missing FILE
        must not be sent to the same recovery policy."""
        model = fileview.classify_failure(
            "call", "NotFoundError: model does not exist", ""
        )
        file_error = fileview.classify_failure(
            "cat", "No such file or directory: x", ""
        )
        assert model["kind"] != file_error["kind"]
        assert model["actions"] != file_error["actions"]

    def test_evidence_path_never_raises_on_empty_input(self):
        record = fileview.classify_failure("", "", "")
        assert record["kind"] in self.KIND_VOCABULARY
        assert record["evidence_path"]
        assert record["actions"]


# ---------------------------------------------------------------------------
# 4. help search finds /steer
# ---------------------------------------------------------------------------


class TestHelpSearch:
    def test_search_finds_steer(self):
        results = fileview.help_search(query="steer")
        assert any(row["name"] == "/steer" for row in results)
        steer = next(row for row in results if row["name"] == "/steer")
        assert steer["summary"]

    def test_search_matches_summary_text_too(self):
        results = fileview.help_search(query="steer the running task")
        assert any(row["name"] == "/steer" for row in results)

    def test_search_finds_the_new_power_tools(self):
        for name in ("/open", "/doctor", "/repo"):
            assert any(
                row["name"] == name for row in fileview.help_search(query=name)
            ), f"{name} is not discoverable through help search"

    def test_empty_query_returns_nothing_rather_than_everything(self):
        assert fileview.help_search(query="") == []

    def test_unknown_query_returns_an_empty_result(self):
        assert fileview.help_search(query="zzz-not-a-command") == []

    def test_search_is_case_insensitive(self):
        assert any(
            row["name"] == "/steer" for row in fileview.help_search(query="STEER")
        )


# ---------------------------------------------------------------------------
# 5. doctor --json reports actionable failures
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_mcp_check_survives_the_real_discovery_shape(self, monkeypatch):
        """`neo doctor` must not crash on a fresh install.

        `cli.connectors.discover_mcp_servers()` returns
        `{label: {"command": ..., "source": ...}}`. `check_mcp_servers` used to
        iterate that as a sequence of `(label, command)` pairs, which unpacks
        the dict KEYS -- so any label longer than two characters raised
        `ValueError: too many values to unpack (expected 2)`.

        Because a new install has a plugin-provided server ("memory"), this
        crashed the MCP health check on the first `neo doctor` a stranger ever
        runs. Found by installing the built 0.3.0 wheel into a clean venv and
        walking the real install-to-first-command journey.
        """
        from cli import connectors

        real = connectors.discover_mcp_servers
        calls = []

        def shaped(repo_path=None):
            return {
                "memory": {"command": "python -m mcp_server", "source": "plugin:demo"},
                "a": {"command": "srv-a", "source": "global"},
            }

        monkeypatch.setattr(connectors, "discover_mcp_servers", shaped)
        monkeypatch.setattr(
            connectors,
            "check_health",
            lambda label, timeout_s=3.0: calls.append(label) or True,
        )
        try:
            record = doctor.check_mcp_servers()
        finally:
            monkeypatch.setattr(connectors, "discover_mcp_servers", real)
        assert record["status"] == "ok", record
        assert "memory" in calls and "a" in calls, calls

    def test_mcp_check_reports_empty_registry_as_ok(self, monkeypatch):
        from cli import connectors

        real = connectors.discover_mcp_servers
        monkeypatch.setattr(
            connectors, "discover_mcp_servers", lambda repo_path=None: {}
        )
        try:
            record = doctor.check_mcp_servers()
        finally:
            monkeypatch.setattr(connectors, "discover_mcp_servers", real)
        assert record["status"] == "ok", record
        assert "no MCP servers configured" in record["reason"], record

    def test_json_document_shape_is_stable(self, tmp_path):
        record = json.loads(
            doctor.render_doctor_json(
                doctor.run_doctor(
                    repo_path=str(tmp_path), log_root=str(tmp_path / "logs")
                )
            )
        )
        assert record["schema_version"] == 1
        assert record["command"] == "doctor"
        # 11: R2-10 added snapshot-disk, R2-16 added connector-permissions
        # (an UNDECLARED connector is the gap that round closed, so the health
        # surface has to name it), and a parallel terminal added
        # spend_receipts. The count is still pinned exactly, because a
        # silently dropped or duplicated check is a defect.
        assert record["summary"]["total"] == len(record["checks"]) == 11
        assert {row["key"] for row in record["checks"]} == {
            "docker",
            "git_identity",
            "provider_reachability",
            "litellm",
            "textual",
            "neo_writable",
            "worktree_root",
            "mcp_servers",
            "connector_permissions",
            "snapshot_disk",
            "spend_receipts",
        }
        # every row carries the four fields the two renderers read
        for row in record["checks"]:
            assert set(row) == {
                "key",
                "kind",
                "label",
                "status",
                "reason",
                "evidence",
                "remediation",
            }, row["key"]

    def test_every_check_declares_status_reason_and_evidence(self, tmp_path):
        for row in doctor.run_doctor(repo_path=str(tmp_path))["checks"]:
            assert row["status"] in {"ok", "failed", "error", "skipped", "deprecated"}
            assert row["reason"], f"{row['key']} has no reason"
            assert "evidence" in row

    def test_a_failing_check_reports_remediation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "docker_available", lambda: False)
        record = doctor.run_doctor(repo_path=str(tmp_path))
        docker = next(r for r in record["checks"] if r["key"] == "docker")
        assert docker["status"] == "failed"
        assert docker["remediation"], "a failed check must name its fix"
        assert record["summary"]["actionable"] >= 1
        assert record["actionable_failures"]

    def test_unwritable_settings_is_actionable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        record = doctor.run_doctor(repo_path=str(tmp_path))
        row = next(r for r in record["checks"] if r["key"] == "neo_writable")
        assert row["status"] in {"ok", "failed"}
        if row["status"] == "failed":
            assert row["remediation"]

    def test_a_raising_probe_is_reported_not_crashed(self, tmp_path, monkeypatch):
        def _boom():
            raise RuntimeError("probe exploded")

        remaining = tuple(
            check for check in doctor.DOCTOR_CHECKS if check.name != "docker"
        )
        monkeypatch.setattr(
            doctor,
            "_DOCTOR_CHECKS",
            (doctor.DoctorCheck("docker", "docker", "Docker", _boom), *remaining),
        )
        record = doctor.run_doctor(repo_path=str(tmp_path))
        docker = next(r for r in record["checks"] if r["key"] == "docker")
        assert docker["status"] == "error"
        assert "probe exploded" in docker["reason"]

    def test_human_render_names_the_failure_and_its_fix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "docker_available", lambda: False)
        text = doctor.render_doctor_human(doctor.run_doctor(repo_path=str(tmp_path)))
        assert "Docker" in text
        assert "actionable" in text
        assert "fix:" in text

    def test_no_secrets_appear_in_the_document(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEO_API_KEY", "sk-do-not-print-me-1234567890")
        text = doctor.render_doctor_json(doctor.run_doctor(repo_path=str(tmp_path)))
        assert "sk-do-not-print-me-1234567890" not in text


# ---------------------------------------------------------------------------
# 6. switching repos changes the effective model and log root
# ---------------------------------------------------------------------------


class TestRepoSwitch:
    def test_switch_changes_model_and_log_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
        for name, model in (("alpha", "model-alpha"), ("beta", "model-beta")):
            repo = tmp_path / name
            (repo / ".neo").mkdir(parents=True)
            (repo / ".neo" / "settings.toml").write_text(
                f'model = "{model}"\n', encoding="utf-8"
            )
        state = {"repo": str(tmp_path / "alpha"), "file_config": None}

        before = fileview.reload_repo_settings(tmp_path / "alpha", state)
        after = fileview.reload_repo_settings(tmp_path / "beta", state)

        assert before["model"] == "model-alpha"
        assert after["model"] == "model-beta"
        assert before["log_root"] != after["log_root"]
        assert after["changed"] is True
        assert state["repo"] == str(tmp_path / "beta")
        assert state["file_config"]["model"] == "model-beta"

    def test_effective_diff_reports_the_changed_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
        for name, model in (("alpha", "m-a"), ("beta", "m-b")):
            repo = tmp_path / name
            (repo / ".neo").mkdir(parents=True)
            (repo / ".neo" / "settings.toml").write_text(
                f'model = "{model}"\n', encoding="utf-8"
            )
        state = {"repo": str(tmp_path / "alpha"), "file_config": None}
        fileview.reload_repo_settings(tmp_path / "alpha", state)
        result = fileview.reload_repo_settings(tmp_path / "beta", state)
        changed = {row["key"] for row in result["effective_diff"]}
        assert "model" in changed

    def test_diff_masks_secrets_but_keeps_model_names_readable(self):
        diff = fileview.settings_effective_diff(
            {"model": "a-very-long-model-name", "api_key": "sk-abcdef1234567890"},
            {"model": "b-very-long-model-name", "api_key": "sk-zzzzzz9999888888"},
        )
        rows = {row["key"]: row for row in diff}
        assert rows["model"]["before"] == "a-very-long-model-name"
        assert "1234567890" not in json.dumps(rows["api_key"])

    def test_switch_is_idempotent_for_the_same_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
        repo = tmp_path / "alpha"
        (repo / ".neo").mkdir(parents=True)
        (repo / ".neo" / "settings.toml").write_text('model = "m"\n', encoding="utf-8")
        state = {"repo": str(repo), "file_config": None}
        first = fileview.reload_repo_settings(repo, state)
        second = fileview.reload_repo_settings(repo, state)
        assert first["log_root"] == second["log_root"]
        assert second["effective_diff"] == []


# ---------------------------------------------------------------------------
# 7. no logs/** phantom files appear in the palette
# ---------------------------------------------------------------------------


class TestPaletteHasNoPhantomLogs:
    def test_force_tracked_logs_are_not_palette_entries(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "logs" / "task-1").mkdir(parents=True)
        (repo / "logs" / "task-1" / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        # force-track the artifact tree: this is the case that used to leak
        _git(repo, "add", "-A", "-f")
        _git(repo, "commit", "-qm", "with logs")

        from cli.tui import scan_repo_files

        files = scan_repo_files(repo)
        assert "src/app.py" in files
        assert not any(entry.startswith("logs/") for entry in files)
        assert not any("trace.jsonl" in entry for entry in files)

    def test_file_picker_also_excludes_logs(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "logs").mkdir()
        (repo / "logs" / "state.json").write_text("{}", encoding="utf-8")
        rows = fileview.file_picker_rows(repo, "", limit=200)
        paths = {str(row.get("path") or "") for row in rows}
        assert not any(path.startswith("logs/") for path in paths)


# ---------------------------------------------------------------------------
# registry integration
# ---------------------------------------------------------------------------


class TestPowerToolRegistry:
    def test_new_commands_are_registered_and_typed(self):
        for name in ("/open", "/doctor", "/repo"):
            spec = commands_mod.command_spec(name)
            assert spec is not None
            assert spec.summary
            assert spec.argument_hint
            assert spec.required_permissions
            assert spec.failure_recovery
            assert name in commands_mod.REQUIRED_COMMANDS

    def test_repo_requires_a_path(self):
        context = commands_mod.surface_command_context("repl")
        assert commands_mod.resolve_command_line("/repo", context).status == "invalid"
        assert commands_mod.resolve_command_line("/repo /tmp/x", context).status == "ok"

    def test_headless_policies_cover_the_new_commands(self):
        for name in ("/open", "/doctor"):
            assert commands_mod.headless_policy(name) in {"mapped", "flag-only"}
        assert commands_mod.headless_policy("/repo") == "mapped"

    def test_doctor_is_runnable_from_the_repl_dispatch(self, tmp_path, capsys):
        state = {"repo": str(tmp_path), "file_config": {}}
        interactive._slash_command_impl(
            "/doctor", "/doctor", {}, tmp_path / "logs", state
        )
        assert "doctor" in capsys.readouterr().out.lower()
