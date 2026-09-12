"""Tests for the Vex CLI Side Task 2: session persistence (--continue /
--resume / --list-sessions + /sessions //resume), slash commands
(/status /diff /approve /reject /cancel /quiet /help), the
~/.vex/config.toml config file (precedence: flags > file > defaults),
and plan preview before execution.

Same cross-terminal note as test_cli_vex.py: Terminal 2 session; T4's
test_cli.py/test_cli_adversarial.py keep owning command-layer coverage.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cli import vexconfig


# ---------------------------------------------------------------------------
# Task C — config file (tested first: A and D build on it)
# ---------------------------------------------------------------------------


class TestVexConfig:
    def test_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "none.toml"))
        assert vexconfig.load_vex_config() == {}

    def test_loads_flat_and_vex_table(self, tmp_path, monkeypatch):
        f = tmp_path / "config.toml"
        f.write_text(
            'model = "m1"\nbudget_cap_usd = 1.5\n[vex]\nplan_preview = true\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("VEX_CONFIG", str(f))
        cfg = vexconfig.load_vex_config()
        assert cfg["model"] == "m1"
        assert cfg["budget_cap_usd"] == 1.5
        assert cfg["plan_preview"] is True

    def test_broken_toml_warns_and_ignores(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "config.toml"
        f.write_text("model = [unclosed", encoding="utf-8")
        monkeypatch.setenv("VEX_CONFIG", str(f))
        assert vexconfig.load_vex_config() == {}
        assert "not valid TOML" in capsys.readouterr().err

    def test_wrong_typed_keys_dropped(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "config.toml"
        f.write_text(
            'model = 123\nmax_retries = "three"\nplan_preview = true', encoding="utf-8"
        )
        monkeypatch.setenv("VEX_CONFIG", str(f))
        cfg = vexconfig.load_vex_config()
        assert "model" not in cfg and "max_retries" not in cfg
        assert cfg["plan_preview"] is True

    def test_flags_override_file(self, tmp_path, monkeypatch):
        """Precedence: explicit config wins over the file — the core
        Task C contract."""
        f = tmp_path / "config.toml"
        f.write_text('model = "file-model"\nbudget_cap_usd = 9.0\n', encoding="utf-8")
        monkeypatch.setenv("VEX_CONFIG", str(f))
        merged = vexconfig.apply_config_defaults(
            {"model": "flag-model", "plan_preview": True}
        )
        assert merged["model"] == "flag-model"  # flag wins
        assert merged["budget_cap_usd"] == 9.0  # file fills the gap
        assert merged["plan_preview"] is True  # explicit passes through

    def test_unknown_keys_pass_through(self, tmp_path, monkeypatch):
        """Future/other-terminal knobs ride along untouched (the harness
        config merge passes unknown keys through — same philosophy)."""
        f = tmp_path / "config.toml"
        f.write_text('some_future_knob = "x"\n', encoding="utf-8")
        monkeypatch.setenv("VEX_CONFIG", str(f))
        assert vexconfig.load_vex_config()["some_future_knob"] == "x"

    def test_no_toml_parser_degrades_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "c.toml"))
        (tmp_path / "c.toml").write_text('model = "m"', encoding="utf-8")
        import builtins

        real_import = builtins.__import__

        def no_toml(name, *a, **k):
            if name in ("tomllib", "tomli"):
                raise ModuleNotFoundError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_toml)
        assert vexconfig.load_vex_config() == {}  # never crashes the CLI


# ---------------------------------------------------------------------------
# Task A — session persistence
# ---------------------------------------------------------------------------


def _mk_run(
    log_root: Path,
    task_id: str,
    *,
    completed=2,
    remaining=1,
    finished=False,
    issue="fix the mean bug",
    repo="/x",
):
    d = log_root / task_id
    d.mkdir(parents=True, exist_ok=True)
    plan = [f"{i}. step {i}" for i in range(1, completed + remaining + 1)]
    (d / "state.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "plan": plan,
                "completed_steps": plan[:completed],
                "remaining_plan": plan[completed:],
                "files_touched": [],
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    (d / "plan.json").write_text(
        json.dumps({"steps": [], "attempts": 1, "cost_usd": 0.0}), encoding="utf-8"
    )
    events = [
        {
            "ts": 0,
            "kind": "task_start",
            "data": {
                "task_id": task_id,
                "repo_path": repo,
                "issue_text": issue,
                "config": {},
            },
        }
    ]
    if finished:
        events.append({"ts": 1, "kind": "result", "data": {"status": "success"}})
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


class TestSessionPersistence:
    def test_record_and_list_newest_first(self, tmp_path):
        from cli import interactive

        interactive.record_session(tmp_path, "t-a", "issue a", "/r", "success")
        time.sleep(0.02)
        interactive.record_session(tmp_path, "t-b", "issue b", "/r", "failed")
        sessions = interactive.list_sessions(tmp_path)
        assert [s["task_id"] for s in sessions] == ["t-b", "t-a"]

    def test_resumable_detection(self, tmp_path):
        from cli import interactive

        _mk_run(tmp_path, "t-open", completed=1, remaining=2, finished=False)
        _mk_run(tmp_path, "t-done", completed=3, remaining=0, finished=True)
        _mk_run(
            tmp_path, "t-finished-trace", completed=1, remaining=2, finished=True
        )  # result event present
        sessions = {
            s["task_id"]: s["resumable"]
            for s in interactive.list_sessions(tmp_path, limit=10)
        }
        # entries only exist for RECORDED sessions; test detection directly:
        assert interactive._is_resumable(tmp_path, "t-open") is True
        assert interactive._is_resumable(tmp_path, "t-done") is False
        assert interactive._is_resumable(tmp_path, "t-finished-trace") is False
        assert interactive._is_resumable(tmp_path, "no-such") is False

    def test_most_recent_resumable_skips_finished(self, tmp_path):
        from cli import interactive

        _mk_run(tmp_path, "t-older-open", finished=False)
        _mk_run(tmp_path, "t-finished", finished=True)
        _mk_run(tmp_path, "t-newer-open", finished=False)
        interactive.record_session(tmp_path, "t-older-open", "i", "/r", "failed")
        time.sleep(0.02)
        interactive.record_session(tmp_path, "t-finished", "i", "/r", "success")
        time.sleep(0.02)
        interactive.record_session(tmp_path, "t-newer-open", "i", "/r", "failed")
        s = interactive.most_recent_resumable(tmp_path)
        assert s is not None and s["task_id"] == "t-newer-open"

    def test_resume_rebuilds_task_with_resume_flag(self, tmp_path, monkeypatch):
        """_resume_task rebuilds the Task from the run's own task_start
        event (repo/issue/config) with config['resume']=True and drives
        the shared executor."""
        from cli import interactive

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _mk_run(tmp_path, "t-res", repo=str(repo_dir), issue="the issue text")
        captured = {}

        def fake_execute(task, log_root, preview=False, issue_for_index=None):
            captured["task"] = task
            captured["preview"] = preview
            return {
                "task_id": task.task_id,
                "log_root": log_root,
                "diff": None,
                "status": "success",
            }

        monkeypatch.setattr(interactive, "_execute_task", fake_execute)
        interactive._resume_task("t-res", tmp_path, {})
        task = captured["task"]
        assert task.task_id == "t-res"
        assert task.repo_path == str(repo_dir)
        assert task.issue_text == "the issue text"
        assert task.config["resume"] is True
        assert captured["preview"] is False  # plan approved pre-crash

    def test_resume_missing_run_reports_cleanly(self, tmp_path, capsys):
        from cli import interactive

        interactive._resume_task("no-such-task", tmp_path, {})
        out = capsys.readouterr().out
        assert "no run found" in out

    def test_cli_bare_session_flags(self, tmp_path, monkeypatch, capsys):
        """`vex --list-sessions` / `--continue` / `--resume <id>` work
        without a subcommand (subparsers are required for everything
        else — these are handled pre-parse) and with piped stdin."""
        from cli import main as m

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "c.toml"))
        (tmp_path / "c.toml").write_text(
            f'log_root = "{(tmp_path / "logs").as_posix()}"\n', encoding="utf-8"
        )
        logs = tmp_path / "logs"
        _mk_run(logs, "t-x", finished=False)
        from cli import interactive

        interactive.record_session(logs, "t-x", "an issue", "/r", "failed")

        rc = m.main(["--list-sessions"])
        out = capsys.readouterr().out
        assert rc == 0 and "t-x" in out and "recent sessions" in out

        rc = m.main(["--resume", "nope"])
        assert rc in (0, 1)  # clean report either way
        assert "no run found" in capsys.readouterr().out

        # --continue resumes the most recent resumable one
        resumed = {}

        def fake_resume(task_id, log_root, state):
            resumed["id"] = task_id

        import cli.interactive as it

        monkeypatch.setattr(it, "_resume_task", fake_resume)
        rc = m.main(["--continue"])
        assert rc == 0 and resumed["id"] == "t-x"

    def test_resume_missing_id_usage_error(self, capsys):
        from cli import main as m

        rc = m.main(["--resume"])
        assert rc == 2


# ---------------------------------------------------------------------------
# Task B — slash commands
# ---------------------------------------------------------------------------


class TestSlashCommands:
    def test_help(self, capsys):
        from cli.interactive import _slash_command

        assert _slash_command("/help", "/help", {}, Path("logs"), {}) is None
        out = capsys.readouterr().out
        for cmd in (
            "/status",
            "/diff",
            "/sessions",
            "/resume",
            "/approve",
            "/reject",
            "/cancel",
            "/quiet",
        ):
            assert cmd in out

    def test_unknown_slash_hint(self, capsys):
        from cli.interactive import _slash_command

        assert _slash_command("/nope", "/nope", {}, Path("logs"), {}) == "unknown"
        assert "unknown command" in capsys.readouterr().out

    def test_status_and_diff_need_a_run(self, capsys):
        from cli.interactive import _slash_command

        _slash_command("/status", "/status", {}, Path("logs"), {})
        assert "no run in this session yet" in capsys.readouterr().out
        _slash_command("/diff", "/diff", {}, Path("logs"), {})
        assert "no diff from the last run" in capsys.readouterr().out

    def test_approve_reject_use_approval_protocol(self, tmp_path, capsys):
        """/approve //reject write decision.json via runtime.approval —
        the EXISTING worker-gate protocol, not a new mechanism."""
        from cli.interactive import _decide_pending, _slash_command
        from runtime import approval as ap

        tid = "t-ap"
        gate = tmp_path / f"{tid}.runtime" / "approval"
        gate.mkdir(parents=True)
        ap.request_approval.__wrapped__ if False else None
        # write a pending request the way the worker does
        import runtime.fsutil as fu

        fu.atomic_write_json(
            gate / "request.json", {"task_id": tid, "diff": "+x", "issue_text": "i"}
        )

        # nothing decided on an unknown task
        assert _decide_pending(tmp_path, "other", approve=True) is None
        # decide on the real one
        assert _decide_pending(tmp_path, tid, approve=True) is True
        dec = json.loads((gate / "decision.json").read_text(encoding="utf-8"))
        assert dec["decision"] == "approve"
        assert "approved" in capsys.readouterr().out
        # reject path
        fu.atomic_write_json(
            gate / "request.json", {"task_id": tid, "diff": "+x", "issue_text": "i"}
        )
        assert _decide_pending(tmp_path, tid, approve=False) is False
        dec = json.loads((gate / "decision.json").read_text(encoding="utf-8"))
        assert dec["decision"] == "reject"

    def test_cancel_signals_main_thread(self, monkeypatch):
        """/cancel delivers SIGINT semantics (the same path Ctrl+C uses:
        checkpoints kept, resumable) — verified by intercepting the
        signal, not by actually interrupting the test runner."""
        from cli import interactive

        raised = {}
        import signal

        real = signal.raise_signal

        def fake(sig):
            raised["sig"] = sig

        monkeypatch.setattr(signal, "raise_signal", fake)
        interactive._interrupt_main()
        assert raised["sig"] == signal.SIGINT

    def test_quiet_toggle(self, capsys):
        from cli.interactive import _slash_command

        state = {"quiet": False}
        _slash_command("/quiet", "/quiet", {}, Path("logs"), state)
        assert state["quiet"] is True
        assert "quiet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Task D — plan preview
# ---------------------------------------------------------------------------


class TestPlanPreview:
    def test_preview_watcher_renders_plan_and_accepts(
        self, tmp_path, capsys, monkeypatch
    ):
        """The watcher finds the FIRST plan event, renders steps with
        checkpoints, and an approved answer lets it return cleanly."""
        from cli import interactive

        tid = "t-plan"
        d = tmp_path / tid
        d.mkdir()
        (d / "trace.jsonl").write_text(
            json.dumps(
                {
                    "kind": "plan",
                    "data": {
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix mean",
                                "checkpoint": "tests pass",
                            },
                            {
                                "id": 2,
                                "description": "cleanup",
                                "checkpoint": "suite green",
                            },
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import threading

        cancel = threading.Event()
        monkeypatch.setattr("builtins.input", lambda *a: "y")
        interactive._plan_preview_watch(tmp_path, tid, cancel, poll_s=0.05)
        out = capsys.readouterr().out
        assert "plan preview" in out
        assert "1. fix mean" in out and "tests pass" in out
        assert "2. cleanup" in out
        assert "approved" in out

    def test_preview_reject_signals_and_hints_resume(
        self, tmp_path, capsys, monkeypatch
    ):
        from cli import interactive
        import signal

        tid = "t-plan2"
        d = tmp_path / tid
        d.mkdir()
        (d / "trace.jsonl").write_text(
            json.dumps(
                {
                    "kind": "plan",
                    "data": {
                        "plan": [{"id": 1, "description": "x", "checkpoint": "y"}]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import threading

        cancel = threading.Event()
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        raised = {}
        monkeypatch.setattr(signal, "raise_signal", lambda sig: raised.update(sig=sig))
        interactive._plan_preview_watch(tmp_path, tid, cancel, poll_s=0.05)
        out = capsys.readouterr().out
        assert "rejected" in out and "--continue" in out
        assert raised["sig"] == signal.SIGINT

    def test_preview_cancelled_before_plan(self, tmp_path):
        """Caller cancellation (run finished early) closes the watcher
        without prompting — no stray input() blocking the session."""
        from cli import interactive
        import threading

        cancel = threading.Event()
        cancel.set()  # already cancelled: no trace file needed
        # returns promptly (no prompt, no exception)
        interactive._plan_preview_watch(tmp_path, "t-none", cancel, poll_s=0.05)

    def test_run_one_fix_honors_preview_config(self, tmp_path, monkeypatch, capsys):
        """plan_preview=true from the config file flows into the Task
        config and turns the preview on; preview=false (or absent) leaves
        it off — the skippable-autonomy contract."""
        from cli import interactive

        captured = {}

        def fake_execute(task, log_root, preview=False, issue_for_index=None):
            captured["preview"] = preview
            return {
                "task_id": task.task_id,
                "log_root": log_root,
                "diff": None,
                "status": "success",
            }

        monkeypatch.setattr(interactive, "_execute_task", fake_execute)
        monkeypatch.chdir(tmp_path)

        # config file says preview ON
        monkeypatch.setenv("VEX_CONFIG", str(tmp_path / "c.toml"))
        (tmp_path / "c.toml").write_text("plan_preview = true\n", encoding="utf-8")
        interactive._run_one_fix("fix it", tmp_path, {}, tmp_path / "logs")
        assert captured["preview"] is True

        # config file says OFF (autonomous default)
        (tmp_path / "c.toml").write_text("plan_preview = false\n", encoding="utf-8")
        interactive._run_one_fix("fix it", tmp_path, {}, tmp_path / "logs")
        assert captured["preview"] is False

        # session state beats the file (explicit > config precedence)
        interactive._run_one_fix(
            "fix it", tmp_path, {"plan_preview": True}, tmp_path / "logs"
        )
        assert captured["preview"] is True
