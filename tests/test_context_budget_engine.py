"""Regression tests for the bounded context engine (VEX-CEILING-02).

The prompt this file answers, in its own words:

* a 5,000-message synthetic session must have flat memory and flat render cost;
* a 32k-window model must never receive an over-window request;
* compaction must fire BEFORE the limit and the run must still complete;
* a summarizer failure must fall back to a cheaper model and RECORD the fallback;
* a killed run must resume with its context budget and prior turns intact;
* conversation-only rewind must not touch files;
* files-only rewind must not mutate the conversation;
* both-rewind must restore both exactly.

Everything runs through the REAL kernel (real journal, real turn ledger, real
conversation journal, real pre-image store) with a scripted model. The model
asserts on the exact request it is handed, so "never over-window" and "the
fallback summary is in context" are content receipts rather than claims about
the code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.agent_kernel import AgentKernel, ContextBudget, RunSpec, TokenEstimator
from harness.agent_kernel.budget import (
    CATEGORIES,
    classify_category,
    plan_drop,
    render_meter_line,
)
from harness.agent_kernel.context import rewind_run
from harness.agent_kernel.conversation import ConversationMemory
from harness.agent_kernel.gateway import ModelGateway
from runtime.checkpoint import ConversationJournal, TurnFileState

WINDOW = 32768
# Realistic large-file body: mixed words, not one repeated character. (A long
# single-character run is a known pathological input for the shared redaction
# pass - see logs/ceiling/terminal-02.json - so a test payload must not rely on
# it being fast.)
BIG = ("def helper(value):\n    return value * 2 + 1\n" * 90)[:4000]
HUGE = ("def helper(value):\n    return value * 2 + 1\n" * 1000)[:40000]


class ScriptedModel:
    """A deterministic model that records the exact request it received."""

    def __init__(self, replies, on_call=None):
        self.replies = list(replies)
        self.messages = []
        self.models = []
        self.on_call = on_call

    def __call__(self, messages, **kwargs):
        self.messages.append([dict(item) for item in messages])
        self.models.append(str(kwargs.get("model") or ""))
        if self.on_call is not None:
            self.on_call(self, messages, kwargs)
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _git_init(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, capture_output=True, check=False, timeout=30
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": "/usr/bin:/bin",
        },
    )
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": "/usr/bin:/bin",
        },
    )


def _git_status(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return completed.stdout.strip()


def _spec(repo, run_id="run-1", session_id="session-1", request="change the value"):
    return RunSpec(
        session_id=session_id,
        run_id=run_id,
        request=request,
        repository_identity=str(repo),
    )


def _kernel(repo, log_root, scripted, **config):
    values = {"agent_approval": "auto", "steering_enabled": False}
    values.update(config)
    return AgentKernel(
        repo_path=str(repo),
        log_root=Path(log_root),
        model_gateway=ModelGateway(call_fn=scripted),
        config=values,
    )


def _events(log_root, run_id, *names):
    """Return the payloads of the named journal events, in order."""
    path = Path(log_root) / run_id / "trace.jsonl"
    wanted = set(names)
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("event") or "") in wanted:
            found.append(row.get("payload") or {})
    return found


def _wide_budget(**overrides):
    """A config whose CHAR budget cannot fire, so only tokens can bound it."""
    values = {
        "context_window_tokens": WINDOW,
        "context_reserved_output_tokens": 0,
        "context_compaction_fraction": 0.6,
        "agent_conversation_messages": 400,
        "agent_conversation_chars": 4_000_000,
        "agent_conversation_tool_chars": 12000,
        "agent_max_read_chars": 40000,
        "agent_max_turns": 40,
    }
    values.update(overrides)
    return values


def _growth_replies(reads, read_chars=40000):
    """A reply script that makes the context genuinely grow.

    A real long session grows because it reads real files. The first turn writes
    a large file and every later turn reads it back, so each tool result is
    thousands of tokens and only the token budget can bound the request.
    """
    script = [
        json.dumps(
            {
                "tool": "write",
                "path": "big.txt",
                "content": HUGE,
            }
        )
    ]
    script.extend(json.dumps({"tool": "read", "path": "big.txt"}) for _ in range(reads))
    script.append(json.dumps({"tool": "finish", "answer": "done"}))
    return script


# ---------------------------------------------------------------------------
# 1. Flat memory and flat render cost for a 5,000-message session
# ---------------------------------------------------------------------------


def test_five_thousand_message_session_has_flat_memory_and_flat_render_cost():
    """5,000 recorded turns must cost the same as 50.

    The regression this pins is unbounded accumulation: a rolling conversation
    that keeps every turn, or re-renders a growing list every turn, degrades a
    multi-hour session into a multi-hour memory leak. Every sample after the
    budget is reached must be identical in retained messages, measured tokens,
    and the memory object's own size.
    """
    memory = ConversationMemory(max_messages=48, max_chars=24000)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    estimator = TokenEstimator("heuristic")
    budget = ContextBudget(window=WINDOW, fraction=0.6, estimator=estimator)
    samples = []
    sizes = []
    for index in range(5000):
        memory.record_tool_result(
            "read", True, f"body {index} {BIG}", turn=index, target="app.py"
        )
        if index % 500 == 0 or index == 4999:
            rendered = memory.render()
            # Flatness is about what a turn COSTS: retained messages, measured
            # tokens, and the object's own serialized size. The dropped-turn
            # counter is cumulative by design and is asserted separately.
            samples.append((len(rendered), budget.measure(rendered).used))
            sizes.append(len(json.dumps(memory.snapshot(), default=str)))
    assert len(samples) >= 10
    assert len(set(samples[-8:])) == 1, samples[-8:]
    retained_messages, measured = samples[-1]
    assert retained_messages <= 50
    assert measured <= budget.threshold + 2000, samples[-1]
    # All 5,000 turns are accounted for: retained plus folded into the handoff.
    assert memory.total_recorded == 5000
    assert memory.dropped_messages == 5000 - len(memory.snapshot()["history"])
    # The snapshot is bounded too: restoring costs the same as rendering.
    assert max(sizes[-4:]) - min(sizes[-4:]) == 0
    snapshot = memory.snapshot()
    before = memory.render()
    memory.reset()
    assert memory.snapshot()["history"] == []
    memory.restore(snapshot)
    assert memory.render() == before


# ---------------------------------------------------------------------------
# 2 + 3. A 32k-window model is never handed an over-window request, and
#         compaction fires before the limit while the run still completes
# ---------------------------------------------------------------------------


def test_thirty_two_k_window_never_receives_an_over_window_request(tmp_path):
    """Every request the model sees must fit the 32k window - and it compacts.

    The guard is inside the model: it measures the request it was actually
    handed and fails if it is over-window. The run therefore proves the engine
    compacted rather than merely claiming it did.
    """
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    seen = []

    def guard(model, messages, kwargs):
        used = TokenEstimator("heuristic").messages_tokens(messages)
        seen.append(used)
        assert used <= WINDOW, f"over-window request: {used} > {WINDOW}"

    model = ScriptedModel(_growth_replies(9), on_call=guard)
    result = _kernel(repo, log_root, model, **_wide_budget()).run(
        _spec(repo, run_id="window-run")
    )

    assert result.status == "completed_unverified"
    assert seen and max(seen) <= WINDOW
    compactions = _events(log_root, "window-run", "context_compacted")
    assert compactions, "a 32k window with 4k-char tool results must compact"
    for receipt in compactions:
        # Fired BEFORE the limit, at the configured fraction, not at the edge.
        assert receipt["trigger_utilization"] < 1.0
        assert receipt["before_tokens"] >= receipt["threshold"]
        assert receipt["after_tokens"] < receipt["before_tokens"]
        # Nothing was silently lost: the receipt says what survived.
        assert receipt["survived"]["retained_messages"] >= 1
        assert receipt["dropped_seqs"]
        assert receipt["reversible"]["dropped_seqs"] == receipt["dropped_seqs"]


def test_compaction_fires_before_the_limit_and_the_run_completes(tmp_path):
    """The whole point: bounded context, same p95 target, finished run."""
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    model = ScriptedModel(_growth_replies(12))
    result = _kernel(repo, log_root, model, **_wide_budget()).run(
        _spec(repo, run_id="compaction-run")
    )
    assert result.status == "completed_unverified"

    budgets = _events(log_root, "compaction-run", "context_budget")
    requests = [item for item in budgets if item.get("stage") == "request"]
    assert requests
    utilizations = [float(item["utilization"]) for item in requests]
    # Target: context utilization p95 <= 80%.
    ordered = sorted(utilizations)
    p95 = ordered[max(0, round(0.95 * (len(ordered) - 1)))]
    assert p95 <= 0.8, f"p95 context utilization {p95}"
    assert max(utilizations) <= 1.0
    # Every category is measured, not just the total.
    for item in requests:
        assert set(item["categories"]).issubset(set(CATEGORIES))
        assert sum(item["categories"].values()) > 0
    # The meter is in the result, so --json consumers need no journal read.
    meter = (result.metadata or {}).get("context") or {}
    assert meter["meter"]["used"] > 0
    assert meter["budget"]["window"] == WINDOW
    assert meter["compactions"], "the run must report its compactions"
    assert Path(meter["artifact"]).is_file()


# ---------------------------------------------------------------------------
# 4. Summarizer failure falls back to a cheaper model, and the receipt says so
# ---------------------------------------------------------------------------


def _compaction_run(tmp_path, run_id, summarizer_reply, **overrides):
    """Run long enough to compact, with a scripted summarizer of our choosing."""
    repo = _repo(tmp_path, name=f"repo-{run_id}")
    log_root = tmp_path / "logs"
    state = {"compacting": False}

    def reply_for(model, messages, kwargs):
        step = str(kwargs.get("step") or "")
        if step.startswith("context-compaction"):
            state["compacting"] = True
            model_name = str(kwargs.get("model") or "")
            state.setdefault("summarizer_models", []).append(model_name)
            if model_name == "cheap-model":
                return "SUMMARY-FROM-FALLBACK: app.py holds `value = 1`."
            return summarizer_reply
        if state["compacting"]:
            return json.dumps({"tool": "finish", "answer": "compacted run done"})
        state["reads"] = state.get("reads", 0) + 1
        if state["reads"] == 1:
            return json.dumps(
                {
                    "tool": "write",
                    "path": "big.txt",
                    "content": HUGE,
                }
            )
        return json.dumps({"tool": "read", "path": "big.txt"})

    class RoutingModel(ScriptedModel):
        def __call__(self, messages, **kwargs):
            self.messages.append([dict(item) for item in messages])
            self.models.append(str(kwargs.get("model") or ""))
            return reply_for(self, messages, kwargs)

    model = RoutingModel([])
    config = _wide_budget()
    config.update(
        {
            "context_compaction_model": "primary-model",
            "context_compaction_fallback_model": "cheap-model",
        }
    )
    config.update(overrides)
    config["model"] = "primary-model"
    kernel = _kernel(repo, log_root, model, **config)
    result = kernel.run(_spec(repo, run_id=run_id))
    return repo, log_root, model, result, state


def test_summarizer_failure_falls_back_to_a_cheaper_model_and_records_it(tmp_path):
    """An empty primary summary is a recorded fallback, not a silent trim."""
    _repo, log_root, model, result, state = _compaction_run(
        tmp_path, "fallback-run", ""
    )
    assert result.status == "completed_unverified"
    compactions = _events(log_root, "fallback-run", "context_compacted")
    assert compactions, "the run must have compacted to exercise the fallback"
    receipt = compactions[0]
    assert receipt["method"] == "fallback_model"
    assert receipt["fallback_model"] == "cheap-model"
    # The cheaper model was actually the one asked.
    assert "cheap-model" in state.get("summarizer_models", [])
    # The fallback summary reached the model's own message list.
    final = "\n".join(item.get("content", "") for item in model.messages[-1])
    assert "SUMMARY-FROM-FALLBACK" in final
    # The fallback is durable, not just a live effect.
    rows = [
        json.loads(line)
        for line in (Path(log_root) / "fallback-run" / "compactions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(row.get("method") == "fallback_model" for row in rows)


def test_summarizer_failure_without_a_fallback_is_recorded_as_a_structural_trim(
    tmp_path,
):
    """No configured fallback is honest about what ran - never a fake summary."""
    _repo, log_root, model, _result, _state = _compaction_run(
        tmp_path,
        "no-fallback-run",
        "",
        context_compaction_fallback_model="",
    )
    receipt = _events(log_root, "no-fallback-run", "context_compacted")[0]
    assert receipt["method"] == "structural_trim"
    assert receipt["fallback_model"] == ""
    assert _events(
        log_root, "no-fallback-run", "context_compaction_fallback_unavailable"
    ), "an unconfigured fallback must be reported, not assumed"
    # The deterministic floor still carried the facts: no silent context loss.
    final = "\n".join(item.get("content", "") for item in model.messages[-1])
    assert "Compacted earlier turns" in final
    assert "read" in final


def test_a_compaction_is_reversible_from_its_own_receipt(tmp_path):
    """A recorded compaction can be rolled back to the exact dropped turns."""
    _repo, log_root, _model, _result, _state = _compaction_run(
        tmp_path, "reversible-run", "SUMMARY: nothing durable yet."
    )
    receipt = _events(log_root, "reversible-run", "context_compacted")[0]
    journal = ConversationJournal(Path(log_root) / "reversible-run")
    dropped = set(receipt["dropped_seqs"])
    assert dropped
    assert not (dropped & {row["seq"] for row in journal.live_snapshot()["history"]})
    restored = journal.restore_compaction(receipt["compaction_id"])
    assert restored is not None
    live = {row["seq"] for row in restored["history"]}
    assert dropped <= live, "a restored compaction must bring its turns back"
    # Rolling back twice is honest: the second call reports nothing to restore.
    assert journal.restore_compaction(receipt["compaction_id"]) is None


# ---------------------------------------------------------------------------
# 5. A hard-killed run resumes with its context budget and prior turns intact
# ---------------------------------------------------------------------------


def test_killed_run_resumes_with_its_context_budget_and_prior_turns(tmp_path):
    """A real ``os._exit`` mid-run must not lose the context budget or turns.

    The child runs the REAL kernel, grows its context until it compacts, and is
    hard-killed. The parent then asserts the durable artifacts survived: the
    conversation journal, the compaction receipt, the per-turn ledger, and the
    turn's own measured meter.
    """
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    driver = Path(__file__).resolve().parent / "context_budget_kill_driver.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(repo),
            str(log_root),
            "kill-context-run",
            "2",
            json.dumps(
                {
                    "context_window_tokens": WINDOW,
                    "context_reserved_output_tokens": 0,
                    "agent_conversation_tool_chars": 12000,
                    "agent_max_read_chars": 40000,
                }
            ),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 70, completed.stderr

    run_dir = log_root / "kill-context-run"
    assert (run_dir / "conversation.jsonl").is_file()
    assert (run_dir / "context.json").is_file()
    assert (run_dir / "turns.jsonl").is_file()

    meter = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))
    assert meter["budget"]["window"] == WINDOW
    assert meter["meter"]["used"] > 0
    assert meter["compactions"], "the pre-kill run must have compacted"

    journal = ConversationJournal(run_dir)
    assert journal.live_snapshot()["history"], "prior turns must survive the kill"

    ledger = [
        json.loads(line)
        for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [int(row["turn"]) for row in ledger] == list(range(1, len(ledger) + 1)), (
        "every pre-kill turn must still be on disk, in order"
    )

    # A resume in this process continues from the durable state, and the model
    # can still see the pre-kill turns.
    resumed = ScriptedModel([json.dumps({"tool": "finish", "answer": "resumed"})])
    kernel = _kernel(repo, log_root, resumed, **_wide_budget())
    result = kernel.run(_spec(repo, run_id="kill-context-run"), resume=True)
    assert result.status == "completed_unverified"
    prompt = "\n".join(item.get("content", "") for item in resumed.messages[0])
    assert "Durable state recovered" in prompt
    assert (result.metadata or {})["context"]["budget"]["window"] == WINDOW


# ---------------------------------------------------------------------------
# 6 + 7 + 8. Three-way rewind
# ---------------------------------------------------------------------------


class _EditModel:
    """Writes a distinct marker per write turn, then finishes. Deterministic.

    ``writes`` is how many turns mutate; ``finish_at`` is which turn issues the
    finish call. The two are separate because the rewind oracle must reach the
    same turn INDEXING as the rewound run, not merely the same file contents.
    """

    def __init__(self, writes, finish_at=None):
        self.writes = int(writes)
        self.finish_at = int(finish_at if finish_at is not None else writes + 1)
        self.calls = 0
        self.messages = []

    def __call__(self, messages, **kwargs):
        self.messages.append([dict(item) for item in messages])
        self.calls += 1
        if self.calls > self.writes or self.calls == self.finish_at:
            return json.dumps({"tool": "finish", "answer": "done"})
        return json.dumps(
            {
                "tool": "write",
                "path": "app.py",
                "content": f"value = {self.calls}\n",
            }
        )


def _run_edits(repo, log_root, run_id, writes, session_id, finish_at=None):
    model = _EditModel(writes, finish_at)
    result = _kernel(repo, log_root, model).run(
        _spec(repo, run_id=run_id, session_id=session_id)
    )
    return model, result


class _InterruptedModel:
    """Writes a marker per turn, then interrupts at the start of ``stop_at``.

    ``KeyboardInterrupt`` is a ``BaseException``, so the kernel's fail-closed
    handler does not swallow it: the run stops exactly where a real interrupt
    would stop it, leaving the durable state at "the start of turn ``stop_at``".
    That is the state a rewind to ``stop_at`` must reproduce.
    """

    def __init__(self, stop_at):
        self.stop_at = int(stop_at)
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        if self.calls >= self.stop_at:
            raise KeyboardInterrupt("interrupted before this turn's work")
        return json.dumps(
            {"tool": "write", "path": "app.py", "content": f"value = {self.calls}\n"}
        )


def _run_edits_interrupted(repo, log_root, run_id, stop_at, session_id):
    model = _InterruptedModel(stop_at)
    with pytest.raises(KeyboardInterrupt):
        _kernel(repo, log_root, model).run(
            _spec(repo, run_id=run_id, session_id=session_id)
        )
    return model


def _tree(repo: Path) -> dict:
    return {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def test_conversation_only_rewind_does_not_touch_files(tmp_path):
    """Undoing a turn's conversation must leave the working tree byte-identical."""
    repo = _repo(tmp_path)
    _git_init(repo)
    log_root = tmp_path / "logs"
    _run_edits(repo, log_root, "conv-run", 4, "session-conv")
    before_tree = _tree(repo)
    before_status = _git_status(repo)

    receipt = rewind_run(
        run_dir=log_root / "conv-run",
        repository_identity=str(repo),
        turn=4,
        scope="conversation",
    )
    assert receipt["scope"] == "conversation"
    assert receipt["conversation"]["dropped_rows"] > 0
    assert receipt["conversation"]["ledger"]["last_turn"] == 3
    # Files: untouched, byte for byte, including git's own view.
    assert _tree(repo) == before_tree
    assert _git_status(repo) == before_status
    # Conversation: actually rewound, with the previous file rotated aside.
    journal = ConversationJournal(log_root / "conv-run")
    assert all(
        int(row.get("turn") or 0) < 4 for row in journal.load() if row.get("turn")
    )
    assert Path(receipt["conversation"]["backup"]).is_file()


def test_files_only_rewind_does_not_mutate_the_conversation(tmp_path):
    """Undoing a turn's edits must leave every conversation record alone."""
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    _run_edits(repo, log_root, "files-run", 4, "session-files")
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 4\n"
    before_journal = (log_root / "files-run" / "conversation.jsonl").read_bytes()
    before_ledger = (log_root / "files-run" / "turns.jsonl").read_bytes()
    before_checkpoint = (log_root / "files-run" / "checkpoint.json").read_bytes()

    receipt = rewind_run(
        run_dir=log_root / "files-run",
        repository_identity=str(repo),
        turn=4,
        scope="files",
    )
    assert receipt["scope"] == "files"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 3\n"
    assert receipt["files"]["restored"] == ["app.py"]
    # Conversation: byte-identical, including the journal and the checkpoint.
    assert (
        log_root / "files-run" / "conversation.jsonl"
    ).read_bytes() == before_journal
    assert (log_root / "files-run" / "turns.jsonl").read_bytes() == before_ledger
    assert (
        log_root / "files-run" / "checkpoint.json"
    ).read_bytes() == before_checkpoint


def test_both_rewind_restores_files_and_journal_state_exactly(tmp_path):
    """A both-axis rewind must reproduce the pre-turn state on BOTH axes.

    The oracle is a second, independent run of the same scripted model stopped
    at the same turn: its file tree, its git status, and its conversation journal
    are what the rewound run must equal.
    """
    repo = _repo(tmp_path, name="repo-main")
    _git_init(repo)
    log_root = tmp_path / "logs"
    _run_edits(repo, log_root, "both-run", 4, "session-both")
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 4\n"
    after_tree = _tree(repo)

    receipt = rewind_run(
        run_dir=log_root / "both-run",
        repository_identity=str(repo),
        turn=4,
        scope="both",
    )
    assert receipt["scope"] == "both"
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 3\n"
    assert _tree(repo) != after_tree

    # The independent oracle: a run INTERRUPTED at the start of turn 4, which
    # is the state the rewind claims to have restored. It is produced by a
    # different mechanism (a real BaseException) in a different run directory.
    oracle_repo = _repo(tmp_path, name="repo-oracle")
    _git_init(oracle_repo)
    _run_edits_interrupted(oracle_repo, log_root, "oracle-run", 4, "session-oracle")
    assert (oracle_repo / "app.py").read_text(encoding="utf-8") == "value = 3\n"
    assert _tree(oracle_repo) == _tree(repo), "the file axis is not exact"
    assert _git_status(oracle_repo) == _git_status(repo), "git status must match"

    def _journal_state(path):
        rows = ConversationJournal(path).load()
        return [
            (
                row.get("record"),
                row.get("turn"),
                row.get("seq"),
                row.get("content"),
            )
            for row in rows
        ]

    assert _journal_state(log_root / "oracle-run") == _journal_state(
        log_root / "both-run"
    ), "the conversation axis is not exact"

    def _ledger_turns(path):
        return [
            (int(row["turn"]), row["status"])
            for row in (
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        ]

    assert _ledger_turns(log_root / "oracle-run" / "turns.jsonl") == _ledger_turns(
        log_root / "both-run" / "turns.jsonl"
    ), "the turn ledger must match the pre-turn state"

    # A conversation-only rewind is NOT a substitute for both: the file stays put.
    rewind_run(
        run_dir=log_root / "both-run",
        repository_identity=str(repo),
        turn=2,
        scope="conversation",
    )
    assert (repo / "app.py").read_text(encoding="utf-8") == "value = 3\n"


def test_rewind_rejects_an_unknown_scope_and_a_missing_run_is_honest(tmp_path):
    """A picker must not silently rewind the wrong axis."""
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    _run_edits(repo, log_root, "guard-run", 2, "session-guard")
    with pytest.raises(ValueError):
        rewind_run(
            run_dir=log_root / "guard-run",
            repository_identity=str(repo),
            turn=2,
            scope="everything",
        )
    empty = rewind_run(
        run_dir=tmp_path / "no-such-run",
        repository_identity=str(repo),
        turn=1,
        scope="conversation",
    )
    assert empty["conversation"]["kept_rows"] == 0
    assert empty["conversation"]["dropped_rows"] == 0
    assert empty["conversation"]["ledger"]["kept"] == 0


# ---------------------------------------------------------------------------
# Budget unit contracts the run-level tests depend on
# ---------------------------------------------------------------------------


def test_categories_and_drop_plan_protect_the_instructions():
    """The base frame, handoff, and newest tool result are never droppable."""
    messages = [
        {"role": "system", "content": "SYSTEM RULES"},
        {"role": "user", "content": "## Active request\ndo the thing"},
        {"role": "user", "content": "## Compacted earlier turns (structured handoff)"},
        {"role": "assistant", "content": "thinking about it"},
        {"role": "user", "content": "TOOL RESULT read (ok):\nbody"},
        {"role": "user", "content": "TOOL RESULT edit (ok):\nnewest"},
    ]
    assert classify_category(messages[0]) == "system_rules"
    assert classify_category(messages[2]) == "memory"
    assert classify_category(messages[4]) == "tool_results"
    assert classify_category(messages[3]) == "assistant"
    assert classify_category(messages[1]) == "user_messages"
    plan = plan_drop(messages, protect=range(3), estimator=TokenEstimator("heuristic"))
    assert 3 not in plan.protected and 3 in plan.indexes
    assert plan.indexes == [3, 4], plan.indexes
    assert 5 not in plan.indexes, "the newest tool result must survive"
    assert set(plan.protected) == {0, 1, 2, 5}, plan.protected
    # With a reclaim target, only the oldest messages are taken.
    targeted = plan_drop(
        messages,
        protect=range(3),
        reclaim_tokens=1,
        estimator=TokenEstimator("heuristic"),
    )
    assert targeted.indexes == [3], targeted.indexes


def test_budget_is_config_driven_and_window_aware():
    from harness.agent_kernel.budget import budget_from_config

    budget = budget_from_config(
        {
            "context_window_tokens": 32768,
            "context_reserved_output_tokens": 4096,
            "context_compaction_fraction": 0.6,
            "context_window_by_model": {"gpt-4o-mini": 128000},
        },
        model="gpt-4o-mini",
    )
    assert budget.window == 128000
    assert budget.usable_window == 123904
    assert budget.threshold == int(123904 * 0.6)
    assert budget.estimator.name in {"heuristic", "tiktoken"}
    assert render_meter_line(budget.measure([])).startswith("context 0/")


def test_token_estimator_never_raises_and_reports_itself():
    """A missing tokenizer degrades to the heuristic and says so."""
    for mode in ("heuristic", "auto", "tiktoken", "nonsense", ""):
        estimator = TokenEstimator(mode)
        assert estimator.name in {"heuristic", "tiktoken"}
        assert estimator.tokens("") == 0
        assert estimator.tokens("x" * 5000) > 0
        assert estimator.message_tokens({"role": "user", "content": "hi"}) > 0
        assert estimator.messages_tokens([{"role": "user", "content": "hi"}]) > 0


def test_turn_file_state_restores_per_turn_bytes_not_first_touch(tmp_path):
    """The exactness claim: turn 3's image, not the file's first original."""
    root = tmp_path / "run"
    files = {"app.py": "v0\n"}
    store = TurnFileState(
        root,
        read_bytes=lambda rel: files.get(rel, "").encode("utf-8"),
        write_bytes=lambda rel, data: files.__setitem__(
            rel, "" if data is None else data.decode("utf-8")
        ),
        read_pristine=lambda rel: b"v0\n" if rel in files else None,
    )
    store.capture(1, [])
    files["app.py"] = "v1\n"
    store.capture(2, ["app.py"])
    files["app.py"] = "v2\n"
    store.capture(3, ["app.py"])
    files["app.py"] = "v3\n"
    store.capture(4, ["app.py"])

    # Turn 3's image is v2 - the bytes that turn 3 actually started from.
    receipt = store.rewind(3, tracked=["app.py"])
    assert files["app.py"] == "v2\n", receipt
    assert receipt["from_pre_image"] == ["app.py"]
    # Turn 1 had no image: app.py was still pristine at its start, so the
    # pristine source is the exact answer, not a later turn's image.
    receipt = store.rewind(1, tracked=["app.py"])
    assert files["app.py"] == "v0\n", receipt
    assert receipt["from_pristine"] == ["app.py"]
    assert store.warnings == []


def test_turn_file_state_deletes_a_file_the_run_created(tmp_path):
    """A file created during a rewound turn is removed, not left behind."""
    root = tmp_path / "run"
    files = {"kept.txt": "k\n"}
    store = TurnFileState(
        root,
        read_bytes=lambda rel: files.get(rel, "").encode("utf-8"),
        write_bytes=lambda rel, data: (
            files.pop(rel, None)
            if data is None
            else files.__setitem__(rel, data.decode("utf-8"))
        ),
        read_pristine=lambda rel: None,
    )
    store.capture(1, [])
    files["new.txt"] = "created\n"
    store.capture(2, ["new.txt"])
    receipt = store.rewind(1, tracked=["new.txt"])
    assert "new.txt" not in files, receipt
    assert receipt["deleted"] == ["new.txt"]


def test_context_meter_and_rewind_picker_are_readable_from_the_run_artifacts(tmp_path):
    """The meter and the picker read the run's own files - no live state."""
    from cli.runview import context_meter_line, read_context_meter, read_rewind_targets

    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    _run_edits(repo, log_root, "view-run", 3, "session-view")
    meter = read_context_meter(log_root / "view-run")
    targets = read_rewind_targets(log_root / "view-run")
    assert [item["turn"] for item in targets] == [4, 3, 2, 1][: len(targets)]
    assert any("write" in item["tools"] for item in targets)
    assert targets[0]["files_so_far"], targets[0]
    assert meter["meter"]["used"] > 0
    assert meter["meter"]["window"] == 32768
    rendered = context_meter_line(meter)
    assert "tok" in rendered and "peak" in rendered, rendered
    assert read_rewind_targets(tmp_path / "nope") == []
    assert read_context_meter(tmp_path / "nope") == {}


def test_compaction_is_observable_in_traceview(tmp_path):
    """`python -m shared.traceview` must show the compaction, not hide it."""
    from shared.traceview import reconstruct_task, summarize

    repo = _repo(tmp_path, name="repo-trace")
    log_root = tmp_path / "logs"
    _compaction_run_for_trace(log_root, repo)
    events = reconstruct_task("trace-run", logs_root=log_root)
    kinds = {str(item.get("event")) for item in events}
    assert "context_compacted" in kinds
    assert "context_budget" in kinds
    summary = summarize(events)
    assert summary["by_event"].get("context_compacted", 0) >= 1
    rendered = "\n".join(
        str(item.get("event")) for item in events if "context" in str(item.get("event"))
    )
    assert "context_compacted" in rendered


def _compaction_run_for_trace(log_root, repo):
    """A minimal run that compacts, with a trivial summarizer."""
    state = {"compacting": False, "reads": 0}

    def call(messages, **kwargs):
        if str(kwargs.get("step") or "").startswith("context-compaction"):
            state["compacting"] = True
            return "SUMMARY: the run read big.txt repeatedly."
        if state["compacting"]:
            return json.dumps({"tool": "finish", "answer": "done"})
        state["reads"] += 1
        if state["reads"] == 1:
            return json.dumps({"tool": "write", "path": "big.txt", "content": HUGE})
        return json.dumps({"tool": "read", "path": "big.txt"})

    kernel = AgentKernel(
        repo_path=str(repo),
        log_root=Path(log_root),
        model_gateway=ModelGateway(call_fn=call),
        config={
            "agent_approval": "auto",
            "steering_enabled": False,
            "model": "primary-model",
            **_wide_budget(),
        },
    )
    return kernel.run(_spec(repo, run_id="trace-run"))
