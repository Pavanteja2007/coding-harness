from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cli import interactive as interactive
from cli import main as main
from cli import session as session


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        upper = name.upper()
        if upper.startswith("NEO_") or "API_KEY" in upper:
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    harness = tmp_path / "harness"
    repo = tmp_path / "repo"
    project = repo / ".neo"
    logs = tmp_path / "logs"
    for path in (home, appdata, harness, project, logs):
        path.mkdir(parents=True, exist_ok=True)
    values = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "XDG_CONFIG_HOME": str(appdata / "xdg"),
        "HARNESS_HOME": str(harness),
        "HARNESS_DECISIONS_DB": str(harness / "memory" / "decisions.db"),
        "HARNESS_LOGS_DIR": str(logs),
        "NEO_CONFIG": str(tmp_path / "config" / "settings.toml"),
        "NEO_LEGACY_CONFIG": str(tmp_path / "legacy" / "config.toml"),
        "NEO_PROJECT_DIR": str(project),
        "NEO_TRACE_DIR": str(tmp_path / "trace"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    return {
        "root": tmp_path,
        "repo": repo,
        "logs": logs,
        "global": Path(values["NEO_CONFIG"]),
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_resumable(log_root: Path, task_id: str, repo: Path) -> None:
    task_dir = log_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    plan = ["step one", "step two"]
    _write(
        task_dir / "state.json",
        json.dumps(
            {
                "task_id": task_id,
                "plan": plan,
                "completed_steps": plan[:1],
                "remaining_plan": plan[1:],
            }
        ),
    )
    _write(task_dir / "plan.json", json.dumps({"steps": plan}))
    _write(
        task_dir / "trace.jsonl",
        json.dumps(
            {
                "kind": "task_start",
                "data": {
                    "repo_path": str(repo),
                    "issue_text": "resume me",
                    "config": {},
                },
            }
        )
        + "\n",
    )


def test_session_index_rejects_traversal_before_state_read(env, monkeypatch):
    logs = env["logs"]
    _make_resumable(env["root"], "outside", env["repo"])
    bad_ids = ["../outside", "..\\outside", "/outside", "C:\\outside"]
    index = [
        {
            "task_id": task_id,
            "issue": "bad",
            "repo": str(env["repo"]),
            "status": "failed",
        }
        for task_id in bad_ids
    ]
    _write(logs / ".neo-sessions.jsonl", "\n".join(json.dumps(x) for x in index))
    seen = []
    real = interactive._is_resumable

    def guarded(root, task_id):
        seen.append(task_id)
        return real(root, task_id)

    monkeypatch.setattr(interactive, "_is_resumable", guarded)
    sessions = interactive.list_sessions(logs)

    assert sessions == []
    assert seen == []


def test_resume_rejects_traversal_before_running(env, monkeypatch):
    called = []
    monkeypatch.setattr(
        interactive,
        "_resume_task",
        lambda task_id, log_root, state: called.append(task_id),
    )

    rc = interactive.cmd_resume("../outside", env["logs"])

    assert rc == 2
    assert called == []


def test_cli_resume_rejects_traversal_before_running(env, monkeypatch):
    _write(env["global"], f"log_root = {json.dumps(str(env['logs']))}\n")
    called = []
    monkeypatch.setattr(
        interactive,
        "_resume_task",
        lambda task_id, log_root, state: called.append(task_id),
    )

    rc = main.main(["--resume", "../outside"])

    assert rc == 2
    assert called == []


def test_tui_resume_rejects_traversal_before_starting_thread(env, monkeypatch):
    from cli import tui

    app = tui.NeoApp(
        repo=env["repo"],
        log_root=env["logs"],
        state={"repo": str(env["repo"]), "file_config": {}},
        file_config={},
    )
    app.transcript = lambda _value: None
    created = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            created.append((args, kwargs))

        def start(self):
            created.append("started")

    monkeypatch.setattr(tui.threading, "Thread", FakeThread)
    app._start_resume("../outside")

    assert created == []


def test_at_mentions_reject_outside_paths_and_files(env):
    repo = env["repo"]
    outside = env["root"] / "outside.txt"
    _write(outside, "OUTSIDE_CANARY")
    (repo / "inside.py").write_text("inside = True\n", encoding="utf-8")

    text, inserted = session.expand_at_mentions("read @../outside.txt", repo, [])
    assert inserted == []
    assert "OUTSIDE_CANARY" not in text

    text, inserted = session.expand_at_mentions(
        "read @outside.txt", repo, ["../outside.txt", str(outside)]
    )
    assert inserted == []
    assert "OUTSIDE_CANARY" not in text


def test_at_mentions_reject_symlink_escape(env):
    repo = env["repo"]
    outside = env["root"] / "outside.txt"
    _write(outside, "OUTSIDE_CANARY")
    link = repo / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    text, inserted = session.expand_at_mentions("read @link.txt", repo, ["link.txt"])

    assert inserted == []
    assert "OUTSIDE_CANARY" not in text


def test_at_mentions_resolve_repo_symlink_and_case_safely(env):
    repo = env["repo"]
    (repo / "inside.py").write_text("inside = True\n", encoding="utf-8")
    (repo / "CaseFile.TXT").write_text("case = True\n", encoding="utf-8")
    alias = env["root"] / "repo-alias"
    try:
        alias.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        alias = None

    if alias is not None:
        text, inserted = session.expand_at_mentions(
            "read @inside.py", alias, ["inside.py"]
        )
        assert inserted == ["inside.py"]
        assert "inside = True" in text

    if os.name == "nt":
        text, inserted = session.expand_at_mentions("read @casefile.txt", repo, [])
        assert inserted == ["CaseFile.TXT"]
        assert "case = True" in text
    else:
        text, inserted = session.expand_at_mentions("read @casefile.txt", repo, [])
        assert inserted == []
        assert "case = True" not in text


def test_session_memory_brief_never_crosses_repo_scope(env):
    from memory.decision_store import DecisionStore
    from memory.paths import decisions_db_path

    repo_a = env["repo"]
    repo_b = env["root"] / "repo-b"
    repo_b.mkdir()
    store = DecisionStore(str(decisions_db_path()))
    try:
        store.record("alpha-repo-memory", repo_path=str(repo_a))
        store.record("beta-repo-memory", repo_path=str(repo_b))
        store.record("unscoped-memory-canary")
    finally:
        store.close()

    lines = session.session_memory_brief(repo_a, env["logs"])
    joined = "\n".join(lines)

    assert "alpha-repo-memory" in joined
    assert "beta-repo-memory" not in joined
    assert "unscoped-memory-canary" not in joined
    assert session.session_memory_brief("", env["logs"]) == []
