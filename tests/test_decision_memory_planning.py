"""Round 2 memory-informed planning tests (Terminal 1 + Terminal 4 joint).

Covers the three pieces of the planner->decision-memory wiring:
- memory side: DecisionStore.search repo_path filtering + repo-key
  normalization + open_default_store location
- harness side: decision_memory.query/render (query building, caps,
  never-raises degradation), the planner prompt's new section and its
  placement AFTER `## Retrieved context` (Terminal 3's difficulty
  predictor cuts there — decision memory must not shift difficulty
  scoring), and the state.json repo_path recording that makes ingestion
  stamp decisions per repo.
"""

import json
import os
from pathlib import Path

import pytest

from harness import decision_memory, prompts
from harness.config import get_config
from harness.context import STATE_KEYS, TaskState
from harness.deps import reset_overrides


# ---------------------------------------------------------------------------
# memory side: DecisionStore.search repo scoping
# ---------------------------------------------------------------------------


def test_search_repo_path_filter_scopes_results(tmp_path):
    from memory.decision_store import DecisionStore

    store = DecisionStore(str(tmp_path / "mem.db"))
    repo_a = str(tmp_path / "repo_a")
    repo_b = str(tmp_path / "repo_b")
    store.record(
        "mean bug: divide by len not len-1",
        task_id="t1",
        repo_path=repo_a,
        source="state-file",
    )
    store.record(
        "wrap bug: flush the pending line",
        task_id="t2",
        repo_path=repo_b,
        source="state-file",
    )
    store.record("global bug note with no repo", task_id="t3", source="manual")

    only_a = store.search("bug", repo_path=repo_a)
    assert [d.task_id for d in only_a] == ["t1"]

    only_b = store.search("bug", repo_path=repo_b)
    assert [d.task_id for d in only_b] == ["t2"]

    # no filter: everything still matches (back-compat)
    everything = store.search("bug")
    assert {d.task_id for d in everything} == {"t1", "t2", "t3"}

    # repo with no decisions: empty, not an error
    assert store.search("bug", repo_path=str(tmp_path / "other")) == []


def test_search_repo_path_normalizes_relative_and_absolute(tmp_path, monkeypatch):
    """The harness records the task's repo_path (often absolute); an
    ingestion or query may hold the same repo as a relative path — the
    normcase+resolve key must make them match."""
    from memory.decision_store import DecisionStore

    store = DecisionStore(str(tmp_path / "mem.db"))
    repo = tmp_path / "numlib_repo"
    repo.mkdir()
    store.record(
        "convention: keep helpers in numlib.core",
        task_id="t1",
        repo_path=str(repo),
        source="state-file",
    )

    monkeypatch.chdir(tmp_path)
    hits = store.search("numlib", repo_path="numlib_repo")
    assert len(hits) == 1
    assert "numlib.core" in hits[0].text


def test_open_default_store_uses_harness_home(tmp_path, monkeypatch):
    from memory.decision_store import open_default_store
    from memory.paths import decisions_db_path

    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "iso-home"))
    store = open_default_store()
    try:
        assert str(store.db_path) == str(decisions_db_path().resolve())
        store.record("from the shared opener", source="mcp")
        assert store.count() == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# harness side: decision_memory query/render
# ---------------------------------------------------------------------------


class _FixedStore:
    """Duck-typed DecisionStore for deterministic query-path tests."""

    def __init__(self, results):
        self._results = results
        self.closed = False

    def search(self, query="", limit=20, repo_path=None):
        self.last_query = query
        self.last_repo = repo_path
        self.last_limit = limit
        return self._results

    def close(self):
        self.closed = True


class _FakeDecision:
    def __init__(self, text, task_id="t-9"):
        self.text = text
        self.task_id = task_id


def test_query_passes_repo_filter_and_terms(monkeypatch):
    store = _FixedStore([_FakeDecision("never mutate tests")])
    monkeypatch.setattr(
        "harness.deps.get_decision_store_factory", lambda: lambda: store
    )
    out = decision_memory.query_planning_decisions(
        repo_path="C:/x/repos/numlib",
        issue_text="mean() is wrong",
        retrieval_terms=["mean"],
        limit=5,
    )
    assert out["error"] is None
    assert store.last_repo == "C:/x/repos/numlib"
    assert store.last_limit == 5
    assert "mean" in store.last_query
    assert "numlib" in store.last_query  # repo segments join the query
    assert out["decisions"] == ["never mutate tests (task:t-9)"]
    assert store.closed


def test_query_degrades_when_memory_module_missing(monkeypatch):
    monkeypatch.setattr("harness.deps.get_decision_store_factory", lambda: None)
    out = decision_memory.query_planning_decisions(repo_path="r", issue_text="anything")
    assert out == {"decisions": [], "query": "", "error": "memory module unavailable"}


def test_query_never_raises_on_store_failure(monkeypatch):
    class _Broken:
        def search(self, *a, **k):
            raise RuntimeError("sqlite exploded")

    monkeypatch.setattr(
        "harness.deps.get_decision_store_factory",
        lambda: lambda: (_ for _ in ()).throw(RuntimeError("open failed")),
    )
    out = decision_memory.query_planning_decisions(repo_path="r", issue_text="x")
    assert "open failed" in out["error"]
    assert out["decisions"] == []

    monkeypatch.setattr(
        "harness.deps.get_decision_store_factory", lambda: lambda: _Broken()
    )
    out = decision_memory.query_planning_decisions(repo_path="r", issue_text="x")
    assert "sqlite exploded" in out["error"]
    assert out["decisions"] == []


def test_render_memory_block_caps_and_empty():
    block = decision_memory.render_memory_block(["d1", "d2", "d3"], max_chars=10000)
    assert block == "- d1\n- d2\n- d3"
    assert decision_memory.render_memory_block([]) == "(none recorded yet)"
    capped = decision_memory.render_memory_block(["x" * 60] * 10, max_chars=100)
    assert len(capped) < 400
    assert "more truncated" in capped


# ---------------------------------------------------------------------------
# prompt + predictor interplay (the cross-module hazard)
# ---------------------------------------------------------------------------


def test_planner_prompt_carries_memory_after_retrieval_marker():
    msgs = prompts.render_planner_prompt(
        "issue text",
        "ctx block",
        "- tests/*",
        strategy="structural+grep",
        memory_block="- prefer sed for single-token fixes",
    )
    user = msgs[1]["content"]
    assert "## Relevant past decisions" in user
    assert "prefer sed for single-token fixes" in user
    # placement contract: AFTER the retrieval marker, BEFORE constraints
    assert (
        user.index("## Retrieved context")
        < user.index("## Relevant past decisions")
        < user.index("## Constraints")
    )


def test_planner_prompt_memory_default_is_none_placeholder():
    msgs = prompts.render_planner_prompt("i", "c", "x")
    assert "(none recorded yet)" in msgs[1]["content"]


def test_memory_section_invisible_to_difficulty_predictor():
    """T3's difficulty estimator cuts the planner's first user message at
    '## Retrieved context' — decision memory sits after the cut, so it
    must never shift difficulty scoring (the ablation's routing would
    silently change otherwise)."""
    from runtime.difficulty import _issue_text_from

    msgs = prompts.render_planner_prompt(
        "plain one-liner issue",
        "ctx",
        "(none)",
        memory_block="- race deadlock intermittent concurrency scary words",
    )
    issue = _issue_text_from(msgs[1]["content"])
    assert "race deadlock" not in issue
    assert "plain one-liner issue" in issue


# ---------------------------------------------------------------------------
# state.json repo_path recording (the ingestion-side enabler)
# ---------------------------------------------------------------------------


def test_state_records_repo_path_additively(tmp_path):
    state = TaskState(tmp_path, "t-r", repo_path="C:/repos/numlib")
    state.set_plan(["1. only"])
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    # the six Boundary 4 keys keep their exact prefix order...
    assert list(on_disk.keys())[: len(STATE_KEYS)] == list(STATE_KEYS)
    # ...and repo_path rides along as the additive seventh
    assert on_disk["repo_path"] == "C:/repos/numlib"


def test_state_without_repo_path_keeps_six_keys(tmp_path):
    state = TaskState(tmp_path, "t-plain")
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert list(on_disk.keys()) == list(STATE_KEYS)


def test_ingest_stamps_repo_path_into_store(tmp_path):
    """End to end over the real Boundary 4 surfaces: harness writes
    state.json with repo_path; DecisionStore.ingest_state_file stamps the
    stored rows so a repo-scoped planner query finds them."""
    from memory.decision_store import DecisionStore

    state_dir = tmp_path / "logs" / "t-1"
    state_dir.mkdir(parents=True)
    state = TaskState(
        state_dir, "t-1", repo_path=str(tmp_path / "repos" / "numlib_repo")
    )
    state.set_plan(["1. fix mean"])
    state.record_decision("mean fix: divide by len(values) not len-1")

    store = DecisionStore(str(tmp_path / "mem.db"))
    new = store.ingest_state_file(str(state_dir / "state.json"))
    assert new == 1
    hits = store.search("mean", repo_path=str(tmp_path / "repos" / "numlib_repo"))
    assert len(hits) == 1
    assert "len(values)" in hits[0].text


# ---------------------------------------------------------------------------
# core.run_task wiring (offline, scripted model)
# ---------------------------------------------------------------------------


def _planning_events(logs_root, task_id):
    events = []
    for line in (
        (logs_root / task_id / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("kind") == "decision_memory":
            events.append(ev)
    return events


def test_run_task_queries_memory_into_planner_prompt(tmp_path, monkeypatch):
    """The REAL loop must query decision memory before planning and put
    the answer in the planner's user message — content-receipt proof via
    a scripted model that echoes its prompt back (not code reading)."""
    from shared.types import Task
    from harness.core import run_task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"
    task_id = "memquery-e2e"
    # A store holding a decision recorded against THIS repo, with a word
    # the issue text does not contain ("checkout" marker).
    marker = "checkout-marker-" + "zqx" * 3
    store = _FixedStore(
        [_FakeDecision(f"use the {marker} convention", task_id="prior")]
    )
    monkeypatch.setattr(
        "harness.deps.get_decision_store_factory", lambda: lambda: store
    )

    seen_prompt = {}

    class EchoPlanner:
        def __call__(self, messages, **kwargs):
            if "planning a bug fix" in messages[0]["content"]:
                seen_prompt["planner_user"] = messages[1]["content"]
                return json.dumps(
                    {
                        "analysis": "a",
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix the mean bug",
                                "checkpoint": "target test passes",
                                "files_hint": ["numlib/mathutil.py"],
                            }
                        ],
                    }
                )
            return "SUBMIT"

    import harness.deps as deps

    deps.set_call_model(EchoPlanner())
    task = Task(
        task_id=task_id,
        repo_path=str(fixture),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "protected_paths": ["tests/*"],
            "target_test": "tests/test_mathutil.py::test_mean_even_count",
        },
    )
    try:
        result = run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    # content-receipt: the marker from decision memory is IN the prompt
    assert marker in seen_prompt.get("planner_user", "")
    # the query reached the store scoped to this repo
    assert store.last_repo == str(fixture)
    # trace carries the decision_memory event with the match
    events = _planning_events(tmp_path / "logs", task_id)
    assert events and events[0]["data"]["matched"] == 1


def test_run_task_memory_off_is_the_off_arm(tmp_path, monkeypatch):
    """plan_with_memory=False must skip the query entirely — one code
    path, no silent half-off states (the ablation depends on this)."""
    from shared.types import Task
    from harness.core import run_task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"
    store = _FixedStore([])
    called = {"n": 0}

    def opener():
        called["n"] += 1
        return store

    monkeypatch.setattr("harness.deps.get_decision_store_factory", lambda: opener)

    class EchoPlanner:
        def __call__(self, messages, **kwargs):
            if "planning a bug fix" in messages[0]["content"]:
                return json.dumps(
                    {
                        "analysis": "a",
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix mean",
                                "checkpoint": "target passes",
                                "files_hint": [],
                            }
                        ],
                    }
                )
            return "SUBMIT"

    import harness.deps as deps

    deps.set_call_model(EchoPlanner())
    task = Task(
        task_id="memoff-e2e",
        repo_path=str(fixture),
        issue_text="mean() divides by len-1; should divide by len",
        config={
            "test_command": "python -m pytest -q",
            "protected_paths": ["tests/*"],
            "plan_with_memory": False,
        },
    )
    try:
        result = run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    assert called["n"] == 0
    events = _planning_events(tmp_path / "logs", "memoff-e2e")
    assert events and events[0]["data"].get("skipped") == "plan_with_memory=False"


def test_run_task_memory_query_failure_degrades_cleanly(tmp_path, monkeypatch):
    """A broken decision store must not poison planning: the planner
    still runs, the section says '(none recorded yet)', the trace
    records the error."""
    from shared.types import Task
    from harness.core import run_task

    fixture = Path(__file__).parent / "fixtures" / "bug02_mean"

    def broken_opener():
        raise RuntimeError("db locked")

    monkeypatch.setattr(
        "harness.deps.get_decision_store_factory", lambda: broken_opener
    )
    seen_prompt = {}

    class EchoPlanner:
        def __call__(self, messages, **kwargs):
            if "planning a bug fix" in messages[0]["content"]:
                seen_prompt["planner_user"] = messages[1]["content"]
                return json.dumps(
                    {
                        "analysis": "a",
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix mean",
                                "checkpoint": "target passes",
                                "files_hint": [],
                            }
                        ],
                    }
                )
            return "SUBMIT"

    import harness.deps as deps

    deps.set_call_model(EchoPlanner())
    task = Task(
        task_id="memfail-e2e",
        repo_path=str(fixture),
        issue_text="mean() divides by len-1; should divide by len",
        config={"test_command": "python -m pytest -q", "protected_paths": ["tests/*"]},
    )
    try:
        result = run_task(task, log_root=tmp_path / "logs")
    finally:
        reset_overrides()
    assert "(none recorded yet)" in seen_prompt.get("planner_user", "")
    events = _planning_events(tmp_path / "logs", "memfail-e2e")
    assert events and events[0]["data"]["error"]


def test_config_defaults_for_memory_planning():
    cfg = get_config({})
    assert cfg["plan_with_memory"] is True
    assert cfg["memory_query_limit"] == 6
    assert cfg["memory_max_chars"] == 1500
    merged = get_config({"plan_with_memory": False})
    assert merged["plan_with_memory"] is False
