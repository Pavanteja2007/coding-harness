"""Persistent conversation sessions (agent-session round).

Covers cli/session.py (state file, @path expansion, compaction,
memory-first hooks, clipboard) plus the new REPL slash commands
(/plan toggle, /review bare view, /compact, /copy-diff, /resume with
no id) and the builtin/custom shadowing contract. No model, no
Docker, no network — all offline.
"""

import os
from pathlib import Path

import pytest

from cli import commands as commands_mod
from cli import session as session_mod


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Isolated home + harness home so memory/files never touch prod."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    hhome = tmp_path / "harness-home"
    hhome.mkdir()
    monkeypatch.setenv("HARNESS_HOME", str(hhome))
    monkeypatch.setenv("HARNESS_DECISIONS_DB", str(hhome / "decisions.db"))
    yield tmp_path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (repo / "sub").mkdir(exist_ok=True)
    (repo / "sub" / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return repo


# -- state file ---------------------------------------------------------


def test_roundtrip_and_history(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    s = session_mod.load_or_create(log_root, repo)
    assert s["session_id"].startswith("sess-")
    session_mod.append_turn(s, "user", "fix it")
    session_mod.append_history(s, "fix it")
    session_mod.append_history(s, "fix it")  # consecutive dupe dropped
    session_mod.save_session(log_root, s)
    s2 = session_mod.load_or_create(log_root, repo, s["session_id"])
    assert len(s2["turns"]) == 1
    assert s2["history"] == ["fix it"]
    assert (log_root / "_conversations" / (s["session_id"] + ".json")).is_file()


def test_corrupt_file_yields_fresh_session(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    s = session_mod.load_or_create(log_root, repo, session_id="sess-abc123")
    session_mod.save_session(log_root, s)
    bad = log_root / "_conversations" / "sess-abc123.json"
    bad.write_text("{not json", encoding="utf-8")
    s2 = session_mod.load_or_create(log_root, repo, session_id="sess-abc123")
    assert s2["turns"] == [] and s2["summary"] == ""


def test_save_never_raises_on_bad_root():
    session_mod.save_session(Path("/nonexistent-root-xyz/logs"), {"session_id": ""})
    assert session_mod.load_or_create(None, None)["turns"] == []


# -- @path expansion ----------------------------------------------------


def test_expand_exact_and_unique_basename(tmp_path):
    repo = _repo(tmp_path)
    text, inserted = session_mod.expand_at_mentions(
        "look at @a.py", repo, ["a.py", "sub/b.py"]
    )
    assert inserted == ["a.py"]
    assert "@path context:" in text and "x = 1" in text
    _text2, inserted2 = session_mod.expand_at_mentions(
        "look at @b.py", repo, ["a.py", "sub/b.py"]
    )
    assert inserted2 == ["sub/b.py"]


def test_expand_unknown_and_plain_passthrough(tmp_path):
    repo = _repo(tmp_path)
    text, inserted = session_mod.expand_at_mentions("look at @nope.py", repo, ["a.py"])
    assert inserted == [] and text == "look at @nope.py"
    assert session_mod.expand_at_mentions("plain line", repo, []) == (
        "plain line",
        [],
    )
    # garbage never raises
    assert session_mod.expand_at_mentions(None, None, None)[1] == []


def test_expand_skips_binary(tmp_path):
    repo = _repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    _text, inserted = session_mod.expand_at_mentions(
        "check @blob.bin", repo, ["blob.bin"]
    )
    assert inserted == []


# -- compaction ---------------------------------------------------------


def test_compact_keeps_recent_and_summarizes(tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    repo = _repo(tmp_path)
    s = session_mod.load_or_create(log_root, repo)
    for i in range(10):
        session_mod.append_turn(
            s, "user", f"message {i}", task_id="agent-1" if i == 0 else None
        )
    summary = session_mod.compact_session(s, log_root, keep_last=3)
    assert summary
    assert len(s["turns"]) == 3
    assert "message 0" in summary  # old turn summarized
    assert "message 9" not in summary  # recent turn kept, not summarized


def test_compact_nothing_to_do(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    s = session_mod.load_or_create(log_root, repo)
    assert session_mod.compact_session(s, log_root) == ""


# -- memory-first hooks -------------------------------------------------


def test_memory_hooks_never_raise(tmp_path):
    repo = _repo(tmp_path)
    assert session_mod.session_memory_brief(repo, tmp_path / "logs") == [] or True
    session_mod.ingest_session_facts(
        tmp_path / "logs", "t-1", "issue", str(repo), "success"
    )
    session_mod.ingest_session_facts(None, "", "", "", "")
    assert session_mod.copy_text_to_clipboard("") is False


def test_ingest_records_session_row(tmp_path):
    from memory.decision_store import DecisionStore
    from memory.paths import decisions_db_path

    log_root = tmp_path / "logs"
    log_root.mkdir()
    repo = _repo(tmp_path)
    session_mod.ingest_session_facts(
        log_root, "t-9", "the login bug", str(repo), "success"
    )
    store = DecisionStore(str(decisions_db_path()))
    try:
        rows = store.search("login bug", limit=5)
    finally:
        store.close()
    assert any("t-9" in (r.task_id or "") for r in rows)


# -- slash commands (REPL) ----------------------------------------------


def test_builtin_shadow_contract():
    assert commands_mod.load_command("plan") is None
    assert commands_mod.load_command("compact") is None
    assert commands_mod.load_command("copy-diff") is None
    assert commands_mod.load_command("history") is None
    # /review stays custom-resolvable by design
    assert "/review" not in commands_mod.BUILTIN_SLASH_COMMANDS


def test_slash_plan_toggles_preview(tmp_path, capsys):
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    _slash_command("/plan", "/plan", {}, tmp_path, state)
    assert state["plan_preview"] is True
    captured = capsys.readouterr()
    assert "plan preview: on" in captured.out
    _slash_command("/plan", "/plan", {}, tmp_path, state)
    assert state["plan_preview"] is False


def test_slash_review_bare_no_run(tmp_path, capsys):
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    _slash_command("/review", "/review", {}, tmp_path, state)
    captured = capsys.readouterr()
    assert "no diff from the last run" in captured.out
    assert "no rationale recorded" in captured.out


def test_slash_review_with_custom_template_runs_it(tmp_path, monkeypatch, capsys):
    from cli.interactive import _slash_command

    repo = tmp_path / "repo"
    (repo / ".vex" / "commands").mkdir(parents=True)
    (repo / ".vex" / "commands" / "review.md").write_text(
        "Review $ARGUMENTS now.", encoding="utf-8"
    )
    ran = {}

    def fake_run(issue, repo_arg, state, log_root, file_config=None):
        ran["issue"] = issue
        return {"task_id": "t-1", "diff": "", "status": "success"}

    monkeypatch.setattr("cli.interactive._run_one_fix", fake_run)
    state = {"repo": str(repo), "file_config": {}}
    _slash_command(
        "/review the auth module", "/review the auth module", {}, tmp_path, state
    )
    assert "Review the auth module now." in ran["issue"]


def test_slash_compact_and_copy_diff_guards(tmp_path, capsys):
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    _slash_command("/compact", "/compact", {}, tmp_path, state)
    assert "no conversation to compact" in capsys.readouterr().out
    conv = session_mod.load_or_create(tmp_path, tmp_path)
    for i in range(16):
        session_mod.append_turn(conv, "user", f"m{i}")
    state["conversation"] = conv
    _slash_command("/compact", "/compact", {}, tmp_path, state)
    assert "compacted" in capsys.readouterr().out
    _slash_command("/copy-diff", "/copy-diff", {}, tmp_path, state)
    assert "no diff from the last run" in capsys.readouterr().out


def test_slash_resume_no_id_without_sessions(tmp_path, capsys):
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    _slash_command("/resume", "/resume", {}, tmp_path, state)
    assert "usage: /resume <task_id>" in capsys.readouterr().out


def test_slash_history_lists_session_lines(tmp_path, capsys):
    from cli.interactive import _slash_command

    conv = session_mod.load_or_create(tmp_path, tmp_path)
    session_mod.append_history(conv, "fix the login bug")
    session_mod.append_history(conv, "check the docs")
    state = {"repo": str(tmp_path), "file_config": {}, "conversation": conv}
    _slash_command("/history", "/history", {}, tmp_path, state)
    out = capsys.readouterr().out
    assert "fix the login bug" in out
    _slash_command("/history login", "/history login", {}, tmp_path, state)
    out = capsys.readouterr().out
    assert "fix the login bug" in out and "check the docs" not in out
    state2 = {"repo": str(tmp_path), "file_config": {}}
    _slash_command("/history", "/history", {}, tmp_path, state2)
    assert "no input history yet" in capsys.readouterr().out


def test_slash_unknown_never_tracebacks(tmp_path, capsys):
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    out = _slash_command("/\x00bad\xffcmd", "/\x00bad\xffcmd", {}, tmp_path, state)
    assert out in (None, "unknown", "continue")


def test_slash_bad_inputs_never_raise(tmp_path, capsys):
    """Hostile/garbage slash lines degrade to honest lines, never raise."""
    from cli.interactive import _slash_command

    state = {"repo": str(tmp_path), "file_config": {}}
    bad = [
        "/diff undo ../../etc/passwd",
        "/diff undo ",
        "/diff undo all extra words here",
        "/resume ???",
        "/resume \x00",
        "/plan",
        "/plan ",
        "/copy-diff",
        "/copy",
        "/compact",
        "/trace notanumber!!",
        "/feed \x00\xff",
        "/approve",
        "/reject",
        "/status",
        "/sessions \x00",
        "/history \x00",
        "/model ",
        "/steer",
        # "/cancel" is excluded: it deliberately raises SIGINT semantics
        # at the main thread (tested by the cancel-specific suites).
        "/quiet",
    ]
    for line in bad:
        _slash_command(line, line.lower(), {}, tmp_path, state)
    capsys.readouterr()  # must not raise; output is honest lines


def test_fold_resumed_updates_last_and_conversation(tmp_path):
    from cli.interactive import _fold_resumed

    conv = session_mod.load_or_create(tmp_path, tmp_path)
    state = {"repo": str(tmp_path), "file_config": {}, "conversation": conv}
    last: dict = {}
    _fold_resumed(
        {"task_id": "agent-r1", "diff": "+x", "status": "success", "answer": "did it"},
        last,
        tmp_path,
        state,
    )
    assert last["task_id"] == "agent-r1"
    assert last["diff"] == "+x"
    assert any(str(t.get("task_id") or "") == "agent-r1" for t in conv["turns"])
    _fold_resumed(None, last, tmp_path, state)  # no-op, never raises


def test_resume_task_agent_replays_history(tmp_path, monkeypatch):
    """Agent /resume keeps the task id and replays prior turns."""
    import json as _json

    from cli import interactive as iv

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    logs = tmp_path / "logs"
    tdir = logs / "agent-r1"
    (tdir / "pristine").mkdir(parents=True)
    (tdir / "trace.jsonl").write_text(
        _json.dumps(
            {
                "kind": "task_start",
                "data": {
                    "mode": "agent",
                    "repo_path": str(repo),
                    "issue_text": "fix a.py",
                    "config": {},
                },
            }
        )
        + "\n"
        + _json.dumps(
            {"kind": "tool_call", "data": {"command": "EDIT a.py", "tool": "edit"}}
        )
        + "\n"
        + _json.dumps({"kind": "edit_applied", "data": {"path": "a.py"}})
        + "\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_run(request, repo_arg, state, log_root, **kw):
        seen.update(kw)
        seen["task_id"] = kw.get("task_id")
        seen["request"] = request
        return {
            "task_id": kw.get("task_id"),
            "diff": "",
            "status": "success",
            "answer": "resumed ok",
        }

    monkeypatch.setattr(iv, "_run_one_agent", fake_run)
    state = {"repo": str(repo), "file_config": {}}
    out = iv._resume_task("agent-r1", logs, state)
    assert out is not None and out["status"] == "success"
    assert seen["task_id"] == "agent-r1"  # same id, not a restart
    assert "fix a.py" in (seen.get("resume_history") or "")
