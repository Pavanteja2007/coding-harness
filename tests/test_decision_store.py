"""Tests for memory/decision_store.py — record, search, Boundary 4 ingestion."""
import threading
import time
from pathlib import Path

import pytest

from memory.decision_store import DecisionStore, format_decisions


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    return DecisionStore(str(tmp_path / "mem" / "decisions.db"))


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


def _state(logs_dir: Path, task_id: str, decisions, repo="R:/repo") -> Path:
    d = logs_dir / task_id
    d.mkdir(exist_ok=True)
    p = d / "state.json"
    p.write_text(
        __import__("json").dumps({
            "task_id": task_id,
            "plan": ["1. fix", "2. verify"],
            "completed_steps": ["1. fix"],
            "files_touched": ["src/a.py"],
            "decisions": decisions,
            "remaining_plan": ["2. verify"],
            "repo_path": repo,
        }),
        encoding="utf-8",
    )
    return p


def test_record_and_search(store):
    store.record("Use SQLite for decision memory", category="architecture")
    store.record("mean() bug was in mathutil.py", category="bug")
    store.record("Always set budget caps before benchmark runs", category="convention")
    assert store.count() == 3

    hits = store.search("sqlite")
    assert len(hits) == 1
    assert "SQLite" in hits[0].text
    assert hits[0].category == "architecture"

    hits2 = store.search("sqlite memory")
    assert hits2 and "SQLite" in hits2[0].text  # ranked first (2 words match)


def test_search_empty_returns_recent(store):
    store.record("first")
    store.record("second")
    store.record("third")
    hits = store.search("")
    assert [h.text for h in hits] == ["third", "second", "first"]


def test_search_ranks_by_word_matches(store):
    store.record("alpha beta gamma")
    store.record("alpha beta")
    store.record("alpha")
    hits = store.search("alpha beta gamma")
    assert hits[0].text == "alpha beta gamma"
    assert hits[1].text == "alpha beta"
    assert hits[2].text == "alpha"


def test_ingest_state_file(store, logs_dir):
    p = _state(logs_dir, "task-1", ["chose diff over full-file rewrite", "kept tests as-is"])
    n = store.ingest_state_file(str(p))
    assert n == 2
    assert store.count() == 2
    assert all(d.source == "state-file" for d in store.search(""))
    # re-ingest is a no-op (dedupe by task_id+text)
    assert store.ingest_state_file(str(p)) == 0
    assert store.count() == 2


def test_ingest_growing_state_file(store, logs_dir):
    """The state file is rewritten as the task progresses — ingesting the
    grown version must add only the new decisions."""
    p = _state(logs_dir, "task-2", ["decision one"])
    store.ingest_state_file(str(p))
    _state(logs_dir, "task-2", ["decision one", "decision two"])
    assert store.ingest_state_file(str(p)) == 1
    assert store.count() == 2


def test_poll_and_watch(store, logs_dir):
    _state(logs_dir, "t-a", ["a1"])
    _state(logs_dir, "t-b", ["b1", "b2"])
    assert store.poll(str(logs_dir)) == 3
    assert store.poll(str(logs_dir)) == 0  # idempotent

    # watcher picks up a file written after it starts
    stop = threading.Event()
    t = threading.Thread(target=store.watch, args=(str(logs_dir), 0.2, stop), daemon=True)
    t.start()
    time.sleep(0.3)
    _state(logs_dir, "t-c", ["c1"])
    deadline = time.time() + 3
    seen = False
    while time.time() < deadline:
        if any(d.task_id == "t-c" for d in store.search("")):
            seen = True
            break
        time.sleep(0.1)
    stop.set()
    t.join(timeout=2)
    assert seen, "watcher did not ingest a state file written after start"


def test_poll_recursive_nested_tasklogs(store, logs_dir):
    """Real runs nest state files deeper than logs/{task_id}/ (e.g.
    benchmark drivers stage them under <logs>/ablations/<run>/tasklogs/
    <task_id>/state.json) — poll() must find them at ANY depth."""
    nested = logs_dir / "ablations" / "run-1" / "tasklogs" / "n-1"
    nested.mkdir(parents=True)
    (nested / "state.json").write_text(
        __import__("json").dumps({
            "task_id": "n-1", "plan": [], "completed_steps": [],
            "files_touched": [], "decisions": ["nested run decision"],
            "remaining_plan": [],
        }),
        encoding="utf-8",
    )
    assert store.poll(str(logs_dir)) == 1
    assert any("nested run decision" in d.text for d in store.search("nested"))
    assert store.poll(str(logs_dir)) == 0  # idempotent across depths


def test_poll_expanded_ablation_run(store, logs_dir):
    """Round 3 Task B: Terminal 3's expanded ablation scale (15-20 tasks
    per arm) in the REAL nested layout it uses:
    <logs>/ablations/<run>/tasklogs/<task_id>/state.json, with archived
    {task_id}.old-* dirs beside live ones, zero-decision tasks (incomplete
    runs legitimately record none), and the SAME task_id appearing in
    different runs. poll() must ingest every decision exactly once and
    stay idempotent — mirroring what the production store sees when the
    expanded run lands."""
    import json

    def ablation_dir(run: str) -> Path:
        d = logs_dir / "ablations" / run / "tasklogs"
        d.mkdir(parents=True)
        return d

    def abl_state(run_dir: Path, task_id: str, decisions, archived=False):
        d = run_dir / (task_id + (".old-20260908-010203" if archived else ""))
        d.mkdir(exist_ok=True)
        (d / "state.json").write_text(
            json.dumps({
                "task_id": task_id, "plan": ["1. fix", "2. verify"],
                "completed_steps": ["1. fix"], "files_touched": ["src/a.py"],
                "decisions": decisions, "remaining_plan": ["2. verify"],
            }),
            encoding="utf-8",
        )

    # ON arm: 20 tasks — the expanded-run scale (bug01..bug05 IDs repeat
    # with suffixes, exactly like T3's driver names them).
    on = ablation_dir("v3-heuristic-on")
    for i in range(20):
        abl_state(on, f"abl-on-bug{i:02d}-{i % 5}",
                  [f"on-arm decision {i}"] if i % 4 else [])  # 5 tasks w/ 0 decisions
    # OFF arm: 15 tasks, one archived duplicate dir alongside the live one.
    off = ablation_dir("v3-heuristic-off")
    for i in range(15):
        abl_state(off, f"abl-off-bug{i:02d}-{i % 5}",
                  [f"off-arm decision {i}"] if i % 3 else [])
    abl_state(off, "abl-off-bug00-0", ["archived dup"], archived=True)

    expected_new = 15 + 10 + 1  # live on + live off + archived dup
    assert store.poll(str(logs_dir)) == expected_new
    # every live decision is searchable
    for i in (1, 5, 9, 13, 17):
        assert any(f"on-arm decision {i}" in d.text for d in store.search(f"on-arm {i}"))
    for i in (1, 4, 7, 10, 13):
        assert any(f"off-arm decision {i}" in d.text for d in store.search(f"off-arm {i}"))
    # idempotent: a second full scan (e.g. the MCP server's lazy poll)
    # ingests nothing new, and re-polling a grown state file is a no-op
    # for texts already stored.
    assert store.poll(str(logs_dir)) == 0
    assert store.count() == expected_new


def test_malformed_state_file(store, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.ingest_state_file(str(bad)) == 0
    missing = tmp_path / "nope.json"
    assert store.ingest_state_file(str(missing)) == 0
    # non-list decisions
    d = tmp_path / "weird"
    d.mkdir()
    (d / "state.json").write_text('{"task_id": "x", "decisions": "not a list"}',
                                  encoding="utf-8")
    assert store.ingest_state_file(str(d / "state.json")) == 0


def test_get_and_format(store):
    rid = store.record("formatting check")
    d = store.get(rid)
    assert d is not None and d.text == "formatting check"
    out = format_decisions(store.search("formatting"))
    assert "- formatting check" in out
    assert "no matching" in format_decisions([], "zzz")


def test_empty_text_rejected(store):
    assert store.record("   ") is None
    assert store.count() == 0


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "mem" / "decisions.db")
    DecisionStore(db).record("survives restart")
    s2 = DecisionStore(db)
    assert s2.count() == 1
    assert "survives restart" in s2.search("restart")[0].text
