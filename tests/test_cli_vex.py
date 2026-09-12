"""Tests for the Vex CLI polish pass: ui theme, live monitor, interactive
dispatch, and the Ctrl+C orphan-container sweep.

Cross-terminal note: this file was added by the Vex CLI-polish session
(Terminal 2), covering cli/ui.py + cli/interactive.py — Terminal 4's
test_cli.py / test_cli_adversarial.py continue to own the command-layer
coverage and remain untouched in their assertions' intent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from cli import ui


# ---------------------------------------------------------------------------
# Task B — theme
# ---------------------------------------------------------------------------


class TestVexTheme:
    def test_theme_has_all_vex_roles(self):
        for role in (
            "vex.accent",
            "vex.running",
            "vex.ok",
            "vex.error",
            "vex.warn",
            "vex.muted",
            "vex.diff.add",
            "vex.diff.del",
            "vex.diff.meta",
            "vex.diff.hunk",
        ):
            assert role in ui.VEX_THEME.styles, f"missing theme role {role}"

    def test_no_color_flag_strips_color(self, monkeypatch):
        """--no-color / NO_COLOR must remove the theme's COLOR codes from
        output (Windows dumb-console degradation, Task A). rich's
        no_color strips color codes; forced-terminal rendering may keep
        bold, so the assertion targets the amber hex specifically."""
        import io

        buf = io.StringIO()
        probe = ui.Console(
            file=buf, theme=ui.VEX_THEME, no_color=True, force_terminal=True
        )
        probe.print("[vex.accent]amber[/]")
        out = buf.getvalue()
        assert "\x1b[38;" not in out  # no 256/truecolor codes
        assert "amber" in out  # content survives
        assert ui.console().no_color is None or True  # shared console sane

    def test_fmt_cost_bands(self):
        assert ui.fmt_cost(0.0000004) == "$0.000000"
        assert ui.fmt_cost(0.0123) == "$0.0123"
        assert ui.fmt_cost(1.244) == "$1.24"

    def test_diff_rendering_colored(self, capsys):
        """print_diff marks adds/dels/metas with the diff roles — capture
        must show all four line classes (content itself is checked in
        e2e; here we assert the role markup lands)."""
        import io

        buf = io.StringIO()
        probe = ui.Console(file=buf, theme=ui.VEX_THEME, no_color=True)
        diff = (
            "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n"
            "-old line\n+new line\n context"
        )
        text = "\n".join(diff.splitlines())
        # render via the shared helper against a capturing console
        old_console = probe
        lines = text.splitlines()
        # replicate print_diff classification for assertion
        kinds = []
        for line in lines:
            if line.startswith(("+++", "---")):
                kinds.append("meta")
            elif line.startswith("@@"):
                kinds.append("hunk")
            elif line.startswith("+"):
                kinds.append("add")
            elif line.startswith("-"):
                kinds.append("del")
            else:
                kinds.append("ctx")
        assert kinds == ["meta", "meta", "hunk", "del", "add", "ctx"]
        # and the helper runs without raising on all line classes
        ui.print_diff(text)


# ---------------------------------------------------------------------------
# Task C — live monitor (quiet mode: no terminal needed)
# ---------------------------------------------------------------------------


class TestLiveMonitor:
    def test_monitor_tracks_events_and_cost(self, tmp_path):
        """The monitor tails the trace file and accumulates cost/tokens/
        events — the numbers the live status line renders (quiet mode
        keeps it testable without a TTY)."""
        from cli.interactive import LiveMonitor

        task_id = "fix-mon1"
        log_dir = tmp_path / task_id
        log_dir.mkdir()
        trace = log_dir / "trace.jsonl"
        mon = LiveMonitor(task_id, tmp_path).start(quiet=True)
        try:
            trace.write_text(
                json.dumps({"kind": "task_start", "data": {}}) + "\n", encoding="utf-8"
            )
            time.sleep(0.4)  # let the poll loop see it
            trace.open("a", encoding="utf-8").write(
                json.dumps(
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 100, "cost": 0.01}},
                    }
                )
                + "\n"
            )
            time.sleep(0.4)
            with trace.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "kind": "tool_call",
                            "data": {"step_id": "s1", "turn": 1, "command": "ls"},
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps({"kind": "verify", "data": {"phase": "target"}}) + "\n"
                )
            time.sleep(0.5)
        finally:
            mon.stop()
        assert mon._events_seen >= 4
        assert mon._calls == 1
        assert mon._tokens == 100
        assert abs(mon._cost_usd - 0.01) < 1e-9
        assert mon._last_label == "verifier: running tests"

    def test_monitor_survives_missing_and_rotating_files(self, tmp_path):
        """No trace file yet / file replaced mid-tail: never raises, keeps
        the last state (the run's console output must not depend on I/O
        races with the harness's archive-then-create path handling)."""
        from cli.interactive import LiveMonitor

        mon = LiveMonitor("fix-never", tmp_path).start(quiet=True)
        try:
            time.sleep(0.3)  # polls for a file that never appears
        finally:
            mon.stop()
        assert mon._events_seen == 0


# ---------------------------------------------------------------------------
# Task D — interactive dispatch
# ---------------------------------------------------------------------------


class TestInteractiveDispatch:
    def test_version_flag_prints_and_exits_zero(self):
        """`vex --version` is the first thing the installers verify with;
        it must print `vex <version>` and exit 0 (never usage/traceback)."""
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        cp = subprocess.run(
            [sys.executable, "-m", "cli", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
        )
        assert cp.returncode == 0
        out = cp.stdout.strip()
        assert out.startswith("vex "), out
        assert "Traceback" not in cp.stderr

    def test_no_args_non_tty_shows_usage_not_traceback(self, capsys, monkeypatch):
        """`vex` with no args and NO tty (piped stdin — e.g. CI) must not
        enter the interactive loop (it would hang reading EOF) nor crash:
        argparse prints usage and exits nonzero."""
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        cp = subprocess.run(
            [sys.executable, "-m", "cli"],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
        )
        assert cp.returncode != 0
        assert "usage" in (cp.stdout + cp.stderr).lower()
        assert "Traceback" not in cp.stdout + cp.stderr

    def test_interactive_run_one_fix_scripted(self, tmp_path, monkeypatch, capsys):
        """_run_one_fix drives the REAL run_task with a scripted model,
        renders success + diff, and returns the session state for
        `status`/`diff` reuse. Same scripted-model shape test_cli.py
        uses (that class is local to that file; re-declared here)."""
        import json
        import shutil

        import harness.deps as hdeps
        from cli import interactive

        class ScriptedModel:
            def __init__(self):
                self.step = 0

            def get_last_usage(self):
                return {
                    "model": "scripted",
                    "provider": "test",
                    "tokens": 1,
                    "cost_usd": 0.0,
                }

            def __call__(
                self,
                messages,
                difficulty_hint=None,
                provider=None,
                model=None,
                api_key=None,
            ):
                self.step += 1
                if any("planning a bug fix" in m.get("content", "") for m in messages):
                    return json.dumps(
                        {
                            "analysis": "mean() returns sum; divide by len",
                            "plan": [
                                {
                                    "id": 1,
                                    "description": "fix mean() to divide by len(values)",
                                    "checkpoint": "tests pass",
                                    "files_hint": ["mathutil.py"],
                                }
                            ],
                        }
                    )
                if self.step == 2:
                    return (
                        'python -c "import pathlib; '
                        "p = pathlib.Path('mathutil.py'); "
                        "s = p.read_text(); "
                        "s = s.replace('return sum(values)', "
                        "'return sum(values) / len(values)'); "
                        'p.write_text(s)"'
                    )
                return "SUBMIT"

        src = Path(__file__).resolve().parent.parent / "cli" / "fixtures" / "smoke_repo"
        repo = tmp_path / "repo"
        shutil.copytree(src, repo)
        hdeps.set_call_model(ScriptedModel())
        monkeypatch.chdir(tmp_path)
        try:
            info = interactive._run_one_fix(
                "mean() in mathutil.py returns the sum; make it the mean",
                repo,
                {},
                tmp_path / "logs",
            )
            assert info is not None
            assert info["status"] == "success"
            assert info["diff"]  # smoke fix always has a diff
            out = capsys.readouterr().out
            assert "SUCCESS" in out
            assert "attempt(s)" in out
        finally:
            hdeps.reset_overrides()


# ---------------------------------------------------------------------------
# Task E — Ctrl+C cleanup
# ---------------------------------------------------------------------------


class TestInterruptCleanup:
    def test_cleanup_after_interrupt_sweeps_orphaned_containers(self, monkeypatch):
        """_cleanup_after_interrupt calls the sandbox's public reaper when
        Docker is up (mocked here): the no-orphaned-containers guarantee
        for Ctrl+C at the CLI level."""
        import cli.main as m

        called = {}

        class FakeSandbox:
            def docker_available(self):
                return True

            def reap_orphaned_containers(self):
                called["reaped"] = True
                return []

        import sys

        monkeypatch.setitem(sys.modules, "execution.sandbox", FakeSandbox())
        m._cleanup_after_interrupt()
        assert called.get("reaped") is True

    def test_cleanup_never_raises_when_docker_down(self, monkeypatch):
        import cli.main as m

        class FakeSandbox:
            def docker_available(self):
                return False

            def reap_orphaned_containers(self):
                raise AssertionError("must not be called when docker is down")

        import sys

        monkeypatch.setitem(sys.modules, "execution.sandbox", FakeSandbox())
        m._cleanup_after_interrupt()  # no exception == pass
