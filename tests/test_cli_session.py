"""Persistent conversation sessions (agent-session round).

Covers cli/session.py (state file, @path expansion, compaction,
memory-first hooks, clipboard) plus the new REPL slash commands
(/plan toggle, /review bare view, /compact, /copy-diff, /resume with
no id) and the builtin/custom shadowing contract. No model, no
Docker, no network — all offline.
"""

import json
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


def test_corrupt_file_is_explicit_and_never_replaced(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    s = session_mod.load_or_create(log_root, repo, session_id="sess-abc123")
    session_mod.save_session(log_root, s)
    bad = log_root / "_conversations" / "sess-abc123.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(session_mod.SessionCorruptError):
        session_mod.load_or_create(log_root, repo, session_id="sess-abc123")
    assert bad.read_text(encoding="utf-8") == "{not json"
    assert (
        session_mod.load_or_create(
            log_root, repo, session_id="sess-abc123", strict=False
        )["turns"]
        == []
    )


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


def test_hundred_turn_session_survives_three_compactions_and_restart(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo)
    for index in range(100):
        session_mod.append_turn(
            session,
            "user" if index % 2 == 0 else "assistant",
            f"turn-{index} answer-{index}",
            task_id=f"task-{index % 4}",
        )
    assert session_mod.save_session(log_root, session)
    assert session_mod.compact_session(session, log_root, keep_last=7)
    for index in range(100, 120):
        session_mod.append_turn(session, "user", f"turn-{index}")
    assert session_mod.compact_session(session, log_root, keep_last=7)
    for index in range(120, 140):
        session_mod.append_turn(session, "assistant", f"turn-{index}")
    assert session_mod.compact_session(session, log_root, keep_last=7)
    assert len(session["raw_turns"]) == 140
    assert len(session["summaries"]) == 3
    assert len(session["compacted_turns"]) == 133
    reloaded = session_mod.load_latest_session(log_root, repo)
    assert reloaded["session_id"] == session["session_id"]
    assert len(reloaded["raw_turns"]) == 140
    assert len(reloaded["turns"]) == 7
    assert session_mod.retrieve_session_turns(reloaded, "answer-0")


def test_follow_up_context_uses_compacted_answer_and_reports_sources(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo)
    session_mod.append_turn(session, "user", "What is the deployment rule?")
    session_mod.append_turn(session, "assistant", "The deployment marker is cobalt-42.")
    for index in range(14):
        session_mod.append_turn(session, "user", f"follow-up-{index}")
    session_mod.compact_session(session, log_root, keep_last=2)
    session_mod.set_active_run(session, "agent-active")
    session_mod.set_model_profile(session, {"model": "test-model", "api_key": "hidden"})
    session_mod.set_unresolved_questions(session, ["Confirm the staging owner"])
    session_mod.set_workspace_mode(session, "review")
    assert session_mod.save_session(log_root, session)
    reloaded = session_mod.load_or_create(log_root, repo, session["session_id"])
    instructions_dir = repo / ".neo" / "instructions"
    instructions_dir.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("root instruction", encoding="utf-8")
    (repo / ".neo" / "instructions" / "safety.md").write_text(
        "nested safety instruction", encoding="utf-8"
    )
    from memory.project_context import discover_project_instructions

    instructions = discover_project_instructions(repo, repo)
    bundle = session_mod.build_session_context(
        reloaded,
        repo=repo,
        task={"issue_text": "What did we decide about deployment?"},
        project_instructions=instructions,
        skills=[{"name": "review", "origin": "project", "body": "review carefully"}],
        decision_memory=[{"text": "Deploy from main", "source": "state-file"}],
        prior_diff="--- a/file.py\n-old\n+new",
        selected_files=["a.py"],
        token_budget=2000,
    )
    assert "cobalt-42" in bundle["text"]
    assert "root instruction" in bundle["text"]
    assert "nested safety instruction" in bundle["text"]
    assert "review carefully" in bundle["text"]
    assert "Deploy from main" in bundle["text"]
    assert "a.py" in bundle["text"]
    assert bundle["estimated_tokens"] <= bundle["token_budget"]
    status = session_mod.format_session_context_status(bundle)
    assert "context:" in status
    assert "project_instructions" in status
    source_names = {item["source"] for item in bundle["sources"] if item["included"]}
    assert {
        "recent_turns",
        "project_instructions",
        "skills",
        "decision_memory",
        "prior_diff",
        "selected_files",
    }.issubset(source_names)
    assert reloaded["model_profile"] == {"model": "test-model"}


def test_large_session_snapshot_remains_valid_json(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo)
    session_mod.append_turn(session, "user", "x" * 600_000)
    assert session_mod.save_session(log_root, session)
    path = log_root / "_conversations" / f"{session['session_id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["turns"][0]["text"].startswith("x" * 1000)
    assert len(data["raw_turns"]) == 1


def test_session_redacts_nested_secrets_and_context_object_shapes(tmp_path):
    from harness.skills import Skill
    from shared.types import Task

    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo)
    secret = "sk-abcdefghijklmnop123456"
    session_mod.append_turn(session, "user", f"credentials: {secret}")
    session_mod.set_model_profile(
        session,
        {"model": "safe-model", "config": {"token": secret}},
    )
    session["active_task"] = {
        "task_id": "task-safe",
        "issue_text": f"repair {secret}",
        "config": {"api_key": secret},
    }
    assert session_mod.save_session(log_root, session)
    raw = (log_root / "_conversations" / f"{session['session_id']}.json").read_text(
        encoding="utf-8"
    )
    assert secret not in raw
    reloaded = session_mod.load_latest_session(log_root, repo)
    assert secret not in reloaded["turns"][0]["text"]
    assert "token" not in reloaded["model_profile"]["config"]

    class Exchange:
        request = f"request {secret}"
        answer = f"answer {secret}"

    bundle = session_mod.build_session_context(
        {
            "turns": [Exchange()],
            "summary": f"summary {secret}",
        },
        repo=repo,
        task=Task(
            task_id="task-safe",
            repo_path=str(repo),
            issue_text=f"issue {secret}",
            config={"api_key": secret},
        ),
        skills=[Skill("safe-skill", "safe", f"body {secret}", "safe.md", "project")],
        token_budget=2000,
    )
    assert secret not in bundle["text"]


def test_session_revision_rejects_stale_writer(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    first = session_mod.load_or_create(log_root, repo)
    assert session_mod.save_session(log_root, first)
    stale = session_mod.load_or_create(log_root, repo, first["session_id"])
    session_mod.append_turn(first, "user", "newer")
    assert session_mod.save_session(log_root, first)
    session_mod.append_turn(stale, "user", "older")
    assert not session_mod.save_session(log_root, stale)
    assert "concurrent" in stale["_last_save_error"]


def test_context_fails_closed_for_legacy_decision_store(tmp_path):
    class LegacyStore:
        def search(self, query, limit=20):
            return [{"text": "GLOBAL-MUST-NOT-LEAK"}]

    repo = _repo(tmp_path)
    bundle = session_mod.build_session_context(
        {"turns": []},
        repo=repo,
        task="local task",
        decision_store=LegacyStore(),
        token_budget=500,
    )
    assert "GLOBAL-MUST-NOT-LEAK" not in bundle["text"]


def test_context_reserves_instruction_space_and_reports_omissions(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("ROOT-INSTRUCTION-MARKER", encoding="utf-8")
    bundle = session_mod.build_session_context(
        {"turns": []},
        repo=repo,
        task="x" * 20_000,
        token_budget=20,
    )
    assert "ROOT-INSTRUCTION-MARKER" in bundle["text"]
    assert bundle["estimated_tokens"] <= bundle["token_budget"]
    from memory.project_context import discover_project_instructions

    limited = session_mod.build_session_context(
        {"turns": []},
        repo=repo,
        project_instructions=discover_project_instructions(repo, max_chars=5),
        token_budget=1,
    )
    assert limited["instruction_files"]
    status = session_mod.format_session_context_status(limited)
    assert "instruction files:" in status


def test_context_reads_request_answer_turns_and_skill_objects(tmp_path):
    from harness.skills import Skill

    class Exchange:
        request = "request marker"
        answer = "answer marker"

    repo = _repo(tmp_path)
    bundle = session_mod.build_session_context(
        {"turns": [Exchange()]},
        repo=repo,
        skills=[
            Skill("object-skill", "safe", "skill body marker", "safe.md", "project")
        ],
        token_budget=500,
    )
    assert "request marker" in bundle["text"]
    assert "answer marker" in bundle["text"]
    assert "skill body marker" in bundle["text"]


def test_session_id_and_repository_containment(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    session = session_mod.load_or_create(log_root, repo, session_id="sess-safe")
    session_mod.save_session(log_root, session)
    with pytest.raises(ValueError):
        session_mod.load_or_create(log_root, repo, session_id="../escape")
    with pytest.raises(session_mod.SessionRepositoryMismatch):
        session_mod.load_or_create(log_root, other, session_id="sess-safe")


# -- memory-first hooks -------------------------------------------------


def test_memory_hooks_never_raise(tmp_path):
    repo = _repo(tmp_path)
    assert session_mod.session_memory_brief(repo, tmp_path / "logs") == []
    session_mod.ingest_session_facts(
        tmp_path / "logs", "t-1", "issue", str(repo), "success"
    )
    session_mod.ingest_session_facts(None, "", "", "", "")
    assert session_mod.copy_text_to_clipboard("") is False


def test_context_memory_query_is_repository_scoped(tmp_path):
    from memory.decision_store import DecisionStore

    repo_a = _repo(tmp_path)
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    store = DecisionStore(str(tmp_path / "memory" / "decisions.db"))
    try:
        store.record("alpha-only-memory", repo_path=str(repo_a))
        store.record("beta-only-memory", repo_path=str(repo_b))
        bundle = session_mod.build_session_context(
            {"summary": "", "turns": []},
            repo=repo_a,
            task="alpha",
            decision_store=store,
            token_budget=200,
        )
    finally:
        store.close()
    assert "alpha-only-memory" in bundle["text"]
    assert "beta-only-memory" not in bundle["text"]


def test_ingest_does_not_promote_ordinary_session_text(tmp_path):
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
        rows = store.search("login bug", limit=5, repo_path=str(repo))
    finally:
        store.close()
    assert rows == []


def test_explicit_session_decision_promotion_requires_a_turn(tmp_path):
    from memory.decision_store import DecisionStore
    from memory.paths import decisions_db_path

    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo, session_id="sess-promote")
    session_mod.append_turn(session, "user", "keep this decision", turn_id="turn-1")
    session_mod.save_session(log_root, session)
    assert (
        session_mod.promote_session_decision(
            session,
            "keep this decision",
            turn_id="missing",
            repo=repo,
        )
        is None
    )
    decision_id = session_mod.promote_session_decision(
        session,
        "keep this decision",
        turn_id="turn-1",
        repo=repo,
    )
    assert decision_id is not None
    store = DecisionStore(str(decisions_db_path()))
    try:
        row = store.get(decision_id)
    finally:
        store.close()
    assert row is not None
    assert row.provenance["kind"] == "session-decision"
    assert row.provenance["turn_id"] == "turn-1"


def test_run_links_are_reconstructed_from_structured_events(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo, session_id="sess-links")
    session_mod.append_turn(
        session,
        "user",
        "repair the parser",
        turn_id="turn-42",
        run_id="run-42",
        task_id="task-42",
        trace_path="logs/task-42/trace.jsonl",
    )
    session_mod.append_turn(
        session,
        "assistant",
        "parser repaired",
        turn_id="turn-42",
        run_id="run-42",
        task_id="task-42",
    )
    assert session_mod.save_session(log_root, session)
    rebuilt = session_mod.reconstruct_session(log_root, repo, "sess-links")
    assert rebuilt["event_count"] >= 2
    linked = [turn for turn in rebuilt["turns"] if turn.get("turn_id") == "turn-42"]
    assert len(linked) == 2
    assert {turn["run_id"] for turn in linked} == {"run-42"}
    assert session_mod.retrieve_session_turns(rebuilt, "run-42")


def test_fork_export_import_preserve_lineage_and_reject_active_runs(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo, session_id="sess-parent")
    session_mod.append_turn(session, "user", "first decision", turn_id="turn-1")
    session_mod.append_turn(session, "assistant", "first answer", turn_id="turn-1")
    session_mod.save_session(log_root, session)
    fork = session_mod.fork_session(
        log_root,
        "sess-parent",
        repo,
        at_turn_id="turn-1",
    )
    assert fork["parent_session_id"] == "sess-parent"
    assert fork["session_id"] != "sess-parent"
    assert fork["active_run_id"] is None
    destination = tmp_path / "export" / "session.json"
    exported = session_mod.export_session(
        log_root,
        fork,
        destination,
        repo,
        mode="redacted",
    )
    assert Path(exported).is_file()
    imported = session_mod.import_session(destination, log_root, repo)
    assert imported["parent_session_id"] == fork["session_id"]
    assert imported["active_run_id"] is None
    assert session_mod.retrieve_session_turns(imported, "first answer")
    fork["active_run_id"] = "run-active"
    session_mod.save_session(log_root, fork)
    active_export = tmp_path / "export" / "active.json"
    session_mod.export_session(log_root, fork, active_export, repo)
    with pytest.raises(session_mod.SessionImportError):
        session_mod.import_session(
            active_export, log_root, repo, session_id="sess-active-import"
        )


def test_restart_reconstructs_after_three_compactions_and_backup_recovery(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo, session_id="sess-restart")
    for index in range(18):
        session_mod.append_turn(
            session,
            "user" if index % 2 == 0 else "assistant",
            f"durable-turn-{index}",
            turn_id=f"turn-{index}",
        )
    assert session_mod.save_session(log_root, session)
    for keep in (5, 4, 3):
        assert session_mod.compact_session(session, log_root, keep_last=keep)
    session_mod.save_session(log_root, session)
    rebuilt = session_mod.reconstruct_session(log_root, repo, "sess-restart")
    assert rebuilt["compaction_count"] == 3
    assert "durable-turn-0" in rebuilt["summary"]
    assert rebuilt["event_count"] >= 21
    assert len(rebuilt["recent_turns"]) <= 3
    path = log_root / "_conversations" / "sess-restart.json"
    path.write_text("{broken", encoding="utf-8")
    report = session_mod.inspect_session(log_root, "sess-restart", repo)
    assert report["status"] == "corrupt"
    with pytest.raises(session_mod.SessionRecoveryError):
        session_mod.recover_session(log_root, "sess-restart", repo)
    recovered = session_mod.recover_session(
        log_root,
        "sess-restart",
        repo,
        strategy="backup",
    )
    assert recovered["status"] == "ok"
    assert (
        session_mod.load_or_create(log_root, repo, "sess-restart")["session_id"]
        == "sess-restart"
    )


def test_event_journal_corruption_respects_strict_compatibility_mode(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(log_root, repo, session_id="sess-event-compat")
    session_mod.append_turn(session, "user", "durable turn", turn_id="turn-compat")
    assert session_mod.save_session(log_root, session)
    event_path = log_root / "_conversations" / "sess-event-compat.events.jsonl"
    event_path.write_text("{broken-event", encoding="utf-8")
    with pytest.raises(session_mod.SessionCorruptError):
        session_mod.load_or_create(log_root, repo, "sess-event-compat")
    compatible = session_mod.load_or_create(
        log_root,
        repo,
        "sess-event-compat",
        strict=False,
    )
    assert compatible["turns"][0]["text"] == "durable turn"


def test_import_rejects_non_list_event_journal(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    source = session_mod.load_or_create(log_root, repo, session_id="sess-event-import")
    document = session_mod.export_session_document(log_root, source, repo)
    document["events"] = {"not": "a list"}
    with pytest.raises(session_mod.SessionImportError):
        session_mod.import_session(document, log_root, repo)


def test_fresh_recovery_quarantines_corrupt_event_journal(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    session = session_mod.load_or_create(
        log_root, repo, session_id="sess-fresh-recovery"
    )
    assert session_mod.save_session(log_root, session)
    state_path = log_root / "_conversations" / "sess-fresh-recovery.json"
    event_path = log_root / "_conversations" / "sess-fresh-recovery.events.jsonl"
    state_path.write_text("{broken", encoding="utf-8")
    event_path.write_text("{broken-event", encoding="utf-8")
    report = session_mod.inspect_session(log_root, "sess-fresh-recovery", repo)
    assert report["status"] == "corrupt"
    recovered = session_mod.recover_session(
        log_root,
        "sess-fresh-recovery",
        repo,
        strategy="fresh",
    )
    assert recovered["status"] == "recovered_fresh"
    assert recovered["event_quarantine_path"]
    assert not event_path.exists()
    assert Path(recovered["event_quarantine_path"]).is_file()
    assert (
        session_mod.load_or_create(log_root, repo, "sess-fresh-recovery")["turns"] == []
    )


def test_import_overwrite_preserves_compare_and_swap_and_replaces_journal(tmp_path):
    log_root = tmp_path / "logs"
    repo = _repo(tmp_path)
    source = session_mod.load_or_create(log_root, repo, session_id="sess-import-source")
    session_mod.append_turn(source, "user", "original turn", turn_id="turn-original")
    session_mod.save_session(log_root, source)
    exported = session_mod.export_session_document(log_root, source, repo)
    first = session_mod.import_session(
        exported,
        log_root,
        repo,
        session_id="sess-import-overwrite",
    )
    first_revision = first["_revision"]
    session_mod.append_turn(
        first, "assistant", "temporary turn", turn_id="turn-temporary"
    )
    assert session_mod.save_session(log_root, first)
    second = session_mod.import_session(
        exported,
        log_root,
        repo,
        session_id="sess-import-overwrite",
        overwrite=True,
    )
    assert second["_revision"] > first_revision
    assert [turn["text"] for turn in second["turns"]] == ["original turn"]
    assert session_mod.retrieve_session_turns(second, "temporary") == []


def test_checkpoint_restore_rejects_corrupt_snapshot_even_when_forced(tmp_path):
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is unavailable for shadow checkpoint verification")
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    checkpoint = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-snapshot-integrity",
    )
    snapshot = (
        logs
        / "_checkpoints"
        / "sess-snapshot-integrity"
        / checkpoint["checkpoint_id"]
        / "snapshot"
        / "a.py"
    )
    snapshot.write_text("tampered\n", encoding="utf-8")
    (repo / "a.py").write_text("workspace edit\n", encoding="utf-8")
    result = session_mod.restore_checkpoint(
        repo,
        logs,
        checkpoint["checkpoint_id"],
        session_id="sess-snapshot-integrity",
        force=True,
    )
    assert result["ok"] is False
    assert any(
        "integrity check" in item.get("reason", "") for item in result["conflicts"]
    )
    assert (repo / "a.py").read_text(encoding="utf-8") == "workspace edit\n"


def test_selected_checkpoint_does_not_conflict_on_unselected_edits(tmp_path):
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    checkpoint = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-selected-files",
        files=["a.py"],
    )
    (repo / "sub" / "b.py").write_text("unselected edit\n", encoding="utf-8")
    (repo / "new.py").write_text("new unselected file\n", encoding="utf-8")
    review = session_mod.review_checkpoint(
        repo,
        logs,
        checkpoint["checkpoint_id"],
        "sess-selected-files",
    )
    assert review["changed"] is False
    result = session_mod.restore_checkpoint(
        repo,
        logs,
        checkpoint["checkpoint_id"],
        session_id="sess-selected-files",
    )
    assert result["ok"] is True
    assert (repo / "sub" / "b.py").read_text(encoding="utf-8") == "unselected edit\n"
    assert (repo / "new.py").read_text(encoding="utf-8") == "new unselected file\n"


def test_checkpoint_storage_inside_workspace_is_excluded_from_manifest(tmp_path):
    repo = _repo(tmp_path)
    logs = repo / "run-logs"
    logs.mkdir()
    (logs / "operator.txt").write_text("do not capture\n", encoding="utf-8")
    checkpoint = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-inside-workspace",
        files=["a.py"],
    )
    assert all("run-logs" not in item["path"] for item in checkpoint["files"])
    assert any(item["path"] == "a.py" for item in checkpoint["files"])


def test_checkpoint_restore_refuses_later_user_edits_and_reviews_diff(tmp_path):
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is unavailable for shadow checkpoint verification")
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    original = repo / "a.py"
    original.write_text("value = 1\n", encoding="utf-8")
    checkpoint = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-checkpoint",
        label="before user edit",
    )
    checkpoint_id = checkpoint["checkpoint_id"]
    assert checkpoint["git"]["available"] is True
    original.write_text("value = 2\n", encoding="utf-8")
    review = session_mod.review_checkpoint(repo, logs, checkpoint_id, "sess-checkpoint")
    assert review["changed"] is True
    assert any(item["path"] == "a.py" for item in review["files"])
    conflict = session_mod.restore_checkpoint(
        repo,
        logs,
        checkpoint_id,
        session_id="sess-checkpoint",
    )
    assert conflict["ok"] is False
    assert conflict["status"] == "conflict"
    assert original.read_text(encoding="utf-8") == "value = 2\n"
    second = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-checkpoint",
        label="current user state",
    )
    restored = session_mod.restore_checkpoint(
        repo,
        logs,
        second["checkpoint_id"],
        session_id="sess-checkpoint",
    )
    assert restored["ok"] is True
    assert original.read_text(encoding="utf-8") == "value = 2\n"


def test_checkpoint_file_and_conversation_restore_are_separate_and_conflict_safe(
    tmp_path,
):
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is unavailable for shadow checkpoint verification")
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    session = session_mod.load_or_create(logs, repo, session_id="sess-restore")
    session_mod.append_turn(session, "user", "before checkpoint", turn_id="turn-1")
    session_mod.save_session(logs, session)
    checkpoint = session_mod.create_checkpoint(
        repo,
        logs,
        session_id="sess-restore",
        session=session,
    )
    session_mod.append_turn(
        session, "assistant", "later conversation edit", turn_id="turn-1"
    )
    assert session_mod.save_session(logs, session)
    repo_file = repo / "a.py"
    repo_file.write_text("before file mutation\n", encoding="utf-8")
    result = session_mod.restore_checkpoint(
        repo,
        logs,
        checkpoint["checkpoint_id"],
        session_id="sess-restore",
        restore_files=True,
        restore_conversation=True,
    )
    assert result["ok"] is False
    assert "conversation changed" in result["conflicts"][0]["reason"]
    assert repo_file.read_text(encoding="utf-8") == "before file mutation\n"


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
    (repo / ".neo" / "commands").mkdir(parents=True)
    (repo / ".neo" / "commands" / "review.md").write_text(
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
    out = capsys.readouterr().out
    # `/resume` accepts a task id OR a session id (session resume shipped
    # after this pin was written), so the usage line names both.
    assert "usage: /resume <task_id|session_id>" in out


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
