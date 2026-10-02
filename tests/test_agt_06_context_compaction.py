"""Regression tests for AGT-06 — budgets, prefix-safe compaction, replayable
condensation.

The prompt this file answers, in its own words:

* a runaway tool output is capped at STORAGE, before anything stores it;
* the cached prefix is byte-identical across a compaction;
* a dropped-message record exists and reconstructs the prior view;
* a thrashing compaction aborts with a reason;
* ``restore_compaction`` still works, and the verifier gate is untouched.

Everything runs host-only: no Docker, no provider, no network. The four kernel
proofs drive the REAL kernel (real journal, real conversation journal, real
turn ledger) with a scripted model, and the model asserts on the exact request
it was handed — so "the cap is at storage" and "the prefix never changed" are
content receipts rather than claims about the code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import harness.agent_kernel as harness_agent_kernel
from harness.agent_kernel import AgentKernel, RunSpec, TokenEstimator
from harness.agent_kernel.budget import (
    CAP_MARKER,
    DEFAULT_COMPACTION_THRASH_LIMIT,
    THRASH_REASONS,
    CompactionThrashError,
    CompactionThrashPolicy,
    CondensationRecord,
    DroppedMessage,
    budget_from_config,
    cap_tool_output,
    compaction_thrash_policy_from_config,
    plan_drop,
    prefix_identity,
)
from harness.agent_kernel.conversation import (
    ConversationMemory,
    contents_by_seq,
    reconstruct_prior_view,
)
from harness.agent_kernel.gateway import ModelGateway
from runtime.checkpoint import ConversationJournal

WINDOW = 32768
BIG = ("def helper(value):\n    return value * 2 + 1\n" * 90)[:4000]
HUGE = ("def helper(value):\n    return value * 2 + 1\n" * 1000)[:40000]

#: The real tool catalog's schemas, so the prefix digest is computed over the
#: same prefix + tool pair a provider would key its cache on.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


class ScriptedModel:
    """A deterministic model that records the exact request it received."""

    def __init__(self, replies, on_call=None):
        self.replies = list(replies)
        self.messages = []
        self.on_call = on_call

    def __call__(self, messages, **kwargs):
        self.messages.append([dict(item) for item in messages])
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


def _journal_rows(log_root, run_id):
    """Return every conversation-journal row for a run."""
    path = Path(log_root) / run_id / "conversation.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


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
        json.dumps({"tool": "write", "path": "big.txt", "content": HUGE}),
    ]
    script.extend(json.dumps({"tool": "read", "path": "big.txt"}) for _ in range(reads))
    script.append(json.dumps({"tool": "finish", "answer": "done"}))
    return script


def _memory(**kwargs):
    """A memory whose CHAR budget cannot fire, so only tokens can compact it."""
    values = {
        "max_messages": 500,
        "max_chars": 4_000_000,
        "max_tool_output_chars": 12000,
    }
    values.update(kwargs)
    return ConversationMemory(**values)


# ---------------------------------------------------------------------------
# 1. A runaway tool output is capped at STORAGE
# ---------------------------------------------------------------------------


def test_a_runaway_tool_output_is_capped_before_anything_stores_it(tmp_path):
    """The cap is applied where the text ENTERS the run, not where it renders.

    A 40,000-character read is the runaway case. Capping at render time is too
    late - the uncapped text is already in the conversation, the journal, the
    trace row and the next request. So the proof reads all FOUR of those and
    requires every one of them to carry the capped text and its marker.
    """
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    requests = []

    def record(model, messages, kwargs):
        requests.append([dict(item) for item in messages])

    model = ScriptedModel(_growth_replies(3), on_call=record)
    result = _kernel(repo, log_root, model, **_wide_budget()).run(
        _spec(repo, run_id="storage-run")
    )
    assert result.status in {"completed_unverified", "completed_verified"}

    receipts = _events(log_root, "storage-run", "tool_output_capped")
    assert receipts, "a 40k-char read must be capped at storage"
    receipt = receipts[0]
    assert receipt["truncated"] is True
    assert receipt["reason"] == "tool_output_token_limit"
    # The turn's combined fan-out bound runs first (24,000 chars), so the
    # storage cap is measured against what the fan-out let through rather than
    # against the file. Both bounds are real; the receipt names the one that
    # shortened this payload.
    assert receipt["original_chars"] == 24000
    assert receipt["kept_chars"] < receipt["original_chars"]
    assert receipt["kept_chars"] <= 4000 * 4 + 64

    rows = _journal_rows(log_root, "storage-run")
    stored = [
        row["content"]
        for row in rows
        if row.get("record") == "turn" and row.get("kind") == "tool_result"
    ]
    assert stored, "the tool result must be journalled"
    for content in stored:
        assert len(content) < 40000, "the uncapped text reached the journal"
    # At least one journalled result is a SHORTENED one, and it says so. A
    # silently truncated payload is indistinguishable from a complete one. Two
    # bounds compose here - the token cap and the conversation's character
    # bound - and EITHER marker is an honest report; what is not acceptable is
    # a shortened payload with no marker at all.
    assert any(
        CAP_MARKER in content or "...[compacted]" in content for content in stored
    ), "a shortened journalled payload must say that it was shortened"

    traced = [
        str(item.get("output") or "")
        for item in _events(log_root, "storage-run", "tool_result")
    ]
    assert traced
    # The trace row is a bounded PREVIEW of the already-capped text: the raw
    # 40,000-character payload is never what gets journalled or traced, and the
    # cap's own receipt - with the counts - sits on the record for the same
    # call. What a reader must never see is a trace row carrying the uncapped
    # payload with no account of why it is short.
    for output in traced:
        assert len(output) <= 6000
    capped_ids = {str(item.get("call_id") or "") for item in receipts}
    traced_ids = {
        str(item.get("call_id") or "")
        for item in _events(log_root, "storage-run", "tool_result")
    }
    assert capped_ids & traced_ids, (
        "the cap receipt must name a call whose result was traced"
    )

    # The NEXT model request is the one that actually costs money.
    later = requests[-1]
    tool_text = "".join(
        str(item.get("content") or "") for item in later if item.get("role") == "user"
    )
    # Two bounds compose between the tool and the model: the token cap and the
    # conversation's character bound. Either marker is an honest report; the
    # uncapped payload reaching the model is not.
    assert CAP_MARKER in tool_text or "...[compacted]" in tool_text, (
        "the model must be told the payload it is reading was shortened"
    )
    assert "def helper(value):" in tool_text, "the control is not vacuous"
    assert len(tool_text) < 60_000


def test_the_storage_cap_is_a_budget_not_a_truncation():
    """A non-positive limit is a real OFF arm, and the cap always says what it lost."""
    off = cap_tool_output("x" * 5000, token_limit=0)
    assert off.truncated is False
    assert off.text == "x" * 5000
    assert off.reason == "disabled"

    capped = cap_tool_output("x" * 5000, token_limit=100)
    assert capped.truncated is True
    assert CAP_MARKER in capped.text
    assert capped.kept_chars < 5000
    # A tiny cap still has to be legible: the marker is never longer than the
    # payload it explains away.
    assert len(capped.text) <= 100 * 4 + 64


# ---------------------------------------------------------------------------
# 2. The cached prefix is byte-identical across a compaction
# ---------------------------------------------------------------------------


def test_the_cached_prefix_is_byte_identical_across_a_compaction():
    """Compaction may touch the growth. It may not touch the head.

    Two independent measurements have to agree: the harness's own prefix
    identity (the seeded base frame) and the runtime prompt-cache authority's
    digest of the rendered request, which is the one a provider's cache
    accounting would compute. A compaction that summarised the head away would
    change both.
    """
    memory = _memory()
    memory.seed(
        [
            {"role": "system", "content": "SYSTEM " * 40},
            {"role": "user", "content": "U"},
        ]
    )
    for index in range(8):
        memory.record_tool_result("read", True, ("body %d " % index) * 300, turn=index)

    estimator = TokenEstimator("heuristic")
    before_messages = memory.render()
    assert estimator.messages_tokens(before_messages) > 4000, (
        "the probe must be over the trigger"
    )
    before_prefix = memory.cache_prefix_identity()
    before_cache = prefix_identity(before_messages, tools=TOOLS)

    receipt = memory.compact_tokens(
        limit_tokens=4000,
        summarize=lambda transcript, turns: ("a model summary", "model_summary"),
        estimator=estimator,
    )
    assert receipt is not None
    after_messages = memory.render()
    after_prefix = memory.cache_prefix_identity()
    after_cache = prefix_identity(after_messages, tools=TOOLS)

    # Bytes.
    assert memory.base_messages() == [
        {"role": "system", "content": "SYSTEM " * 40},
        {"role": "user", "content": "U"},
    ]
    assert before_prefix.sha256 == after_prefix.sha256
    assert before_prefix.messages == after_prefix.messages
    # The provider's own view of the cacheable head.
    assert before_cache.sha256 == after_cache.sha256
    assert before_cache.source == "runtime.prompt_cache"
    # The receipt states it, and says where the body begins.
    assert receipt["cache_prefix"]["preserved"] is True
    assert (
        receipt["cache_prefix"]["before"]["sha256"]
        == receipt["cache_prefix"]["after"]["sha256"]
    )
    assert receipt["condensation"]["cache_prefix_preserved"] is True
    # `body_from` is where a compaction may FIRST act: the base frame plus the
    # handoff. That is a different boundary from `cache_prefix_messages`, which
    # is the provider's cache breakpoint - the handoff is safe to replace
    # precisely because nothing in the provider's cache keys on it.
    assert receipt["condensation"]["body_from"] == 2
    assert receipt["condensation"]["cache_prefix_messages"] == 1
    assert receipt["condensation"]["dropped"]
    assert all(int(item["seq"]) > 0 for item in receipt["condensation"]["dropped"])
    # And the compaction really did do something, so the equality is not vacuous.
    assert receipt["dropped_seqs"]
    assert receipt["reclaimed_tokens"] > 0
    assert after_messages[:2] == before_messages[:2]


def test_no_drop_plan_can_reach_into_the_cached_prefix():
    """Prefix safety is STRUCTURAL: a caller that forgets to protect it still cannot.

    This is the regression that matters, because the failure mode it prevents is
    silent and expensive: a compaction that eats the prompt-cached head looks
    perfectly healthy in every receipt while re-creating the provider's cache
    entry on every turn.

    Two different regions are protected by two different mechanisms, and conflating
    them is the bug this test is here to prevent. ``plan_drop`` protects the
    PROVIDER's frozen head structurally, with no argument from the caller. The
    wider base frame is the caller's ``protect``, because only the caller knows
    where its own base frame ends - which is exactly what
    ``ConversationMemory.protected_prefix`` reports.
    """
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "U"},
        {"role": "user", "content": "old 1"},
        {"role": "user", "content": "old 2"},
        {"role": "user", "content": "newest"},
    ]
    estimator = TokenEstimator("heuristic")
    # No `protect=` at all: the plan must still refuse the frozen head.
    plan = plan_drop(messages, estimator=estimator)
    assert plan.prefix_messages == 1
    assert plan.body_from == 1
    assert plan.prefix_protected is True
    assert 0 not in plan.indexes

    # The caller's base frame, the way the conversation actually passes it.
    framed = plan_drop(messages, protect=range(2), estimator=estimator)
    assert framed.prefix_protected is True
    assert 0 not in framed.indexes
    assert 1 not in framed.indexes
    assert framed.indexes == [2, 3]

    # An explicit, longer breakpoint is honoured too: the caller knows its
    # prefix is longer, and the plan respects it rather than guessing.
    wide = plan_drop(messages, estimator=estimator, breakpoint_index=1)
    assert wide.prefix_messages == 2
    assert wide.prefix_protected is True
    assert not (set(wide.indexes) & {0, 1})

    # The OFF arm exists only for a test that is deliberately measuring the
    # unprotected shape, and it is reported as such.
    unprotected = plan_drop(messages, estimator=estimator, protect_prefix=False)
    assert unprotected.prefix_protected is False
    assert 0 in unprotected.indexes

    # `prefix_protected` is recomputed from the plan, not asserted by the
    # producer, so a future selection change that reached the head would flip
    # the receipt rather than quietly invalidating the cache.
    lying = plan.__class__(indexes=[0], prefix_messages=1)
    assert lying.prefix_protected is False


def test_the_unchanging_head_is_what_the_receipt_names():
    """The provider's frozen head is one message; the base frame is two.

    Both facts matter and they are different facts. ``runtime.prompt_cache``
    anchors the cache breakpoint on the leading system message, so that is the
    head a provider can serve from cache; the whole seeded ``[system, user]``
    frame is the region a compaction must never summarise away, because nothing
    else in the request restates it.
    """
    identity = prefix_identity(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        tools=TOOLS,
    )
    assert identity.messages == 1
    assert identity.source == "runtime.prompt_cache"
    assert len(identity.sha256) == 64
    # The tool schemas are part of the digest, so a catalog change is a real
    # prefix change rather than an invisible one.
    other = prefix_identity(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        tools=[dict(TOOLS[0], function=dict(TOOLS[0]["function"], name="grep"))],
    )
    assert other.sha256 != identity.sha256
    # An empty request has no head, and says so instead of inventing a digest.
    assert prefix_identity([]).source == "empty"


# ---------------------------------------------------------------------------
# 3. A dropped-message record exists and reconstructs the prior view
# ---------------------------------------------------------------------------


def test_a_dropped_message_record_reconstructs_the_prior_view(tmp_path):
    """Which messages went, and what replaced them — enough to put them back.

    The record carries each dropped message's IDENTITY plus a content digest,
    and the replacement summary verbatim. The content itself lives in the durable
    journal (that is the whole point of not retaining it here), and the two
    together rebuild the exact pre-compaction request.
    """
    journal = ConversationJournal(tmp_path / "run")
    memory = _memory(journal=journal.append, compaction_thrash_limit=0)
    memory.seed(
        [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "U"}]
    )
    estimator = TokenEstimator("heuristic")
    for index in range(8):
        memory.record_tool_result(
            "read",
            True,
            f"MARKER-{index} " + ("filler " * 200),
            turn=index,
            target="app.py",
        )
    before_view = memory.render()
    assert len(before_view) > 3

    receipt = memory.compact_tokens(
        limit_tokens=1200,
        summarize=lambda transcript, turns: (
            "the model said: it read app.py",
            "model_summary",
        ),
        estimator=estimator,
    )
    assert receipt is not None
    condensation = CondensationRecord.from_dict(receipt["condensation"])

    # -- the record itself: who went, and what came back ---------------------
    assert condensation.dropped
    assert condensation.summary == "the model said: it read app.py"
    assert condensation.summary_chars == len(condensation.summary)
    assert condensation.dropped_seqs == receipt["dropped_seqs"]
    for item in condensation.dropped:
        assert isinstance(item, DroppedMessage)
        assert item.seq > 0
        assert item.chars > 0
        assert len(item.content_sha256) == 16
        assert item.tool == "read"
        # Identity only. A record that carried the text would make the in-memory
        # conversation grow as fast as the history it bounds.
        assert "content" not in item.as_dict()

    # -- the record survives the journal round trip --------------------------
    rows = journal.load()
    compaction_rows = [row for row in rows if row.get("record") == "compaction"]
    assert compaction_rows
    journalled = compaction_rows[-1]["condensation"]
    assert journalled["compaction_id"] == condensation.compaction_id
    assert [item["seq"] for item in journalled["dropped"]] == condensation.dropped_seqs
    assert journalled["summary"] == condensation.summary

    # -- and it reconstructs the prior view ---------------------------------
    contents = contents_by_seq(rows)
    memory.bind_contents(contents.get)
    rebuilt = memory.reconstruct_prior_view(condensation)
    assert rebuilt is not None
    assert rebuilt == before_view, (
        "the condensation record plus the journal must rebuild the exact "
        "pre-compaction request"
    )
    # The module-level function agrees with the bound path.
    assert (
        reconstruct_prior_view(
            condensation,
            base_messages=memory.base_messages(),
            live_messages=memory.render(),
            contents=contents,
            live_handoff=memory.handoff.as_dict(),
        )
        == before_view
    )
    # The digests in the record are what make the rebuild verifiable rather than
    # merely plausible.
    for item in condensation.dropped:
        import hashlib

        assert (
            hashlib.sha256(contents[item.seq].encode("utf-8", "replace")).hexdigest()[
                :16
            ]
            == item.content_sha256
        )


def test_a_reconstruction_that_cannot_read_the_content_says_no(tmp_path):
    """A partial prior view is not a prior view.

    Without the content source the answer is ``None``, never a shorter list that
    a caller who never checked would read as the whole thing.
    """
    memory = _memory(compaction_thrash_limit=0)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    for index in range(6):
        memory.record_tool_result("read", True, ("body " * 300), turn=index)
    receipt = memory.compact_tokens(
        limit_tokens=800,
        summarize=lambda transcript, turns: ("s", "model_summary"),
        estimator=TokenEstimator("heuristic"),
    )
    assert receipt is not None
    # No resolver bound at all.
    assert memory.reconstruct_prior_view(receipt["condensation"]) is None
    # A resolver that cannot answer ONE of the dropped sequences fails the whole
    # reconstruction.
    memory.bind_contents(lambda seq: None)
    assert memory.reconstruct_prior_view(receipt["condensation"]) is None

    # A resolver that raises is treated as "cannot answer", never as content.
    def boom(seq):
        raise RuntimeError("journal gone")

    memory.bind_contents(boom)
    assert memory.reconstruct_prior_view(receipt["condensation"]) is None


def test_the_character_fold_is_a_replayable_condensation_too():
    """The second compaction path carries the same dropped-message record.

    A character/message fold is a compaction by another name. If only the token
    path were inspectable, the most frequent compaction in a long run would be
    the one nobody can debug.
    """
    rows = []
    memory = ConversationMemory(
        max_messages=6,
        max_chars=100000,
        journal=rows.append,
        compaction_thrash_limit=0,
    )
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    for index in range(10):
        memory.record_note(f"note {index}", turn=index)
    folds = [row for row in rows if row.get("record") == "compaction"]
    assert folds, "the message budget must fold"
    fold = folds[0]
    assert fold["method"] == "budget_fold"
    assert fold["cache_prefix_preserved"] is True
    condensation = CondensationRecord.from_dict(fold["condensation"])
    assert condensation.method == "budget_fold"
    assert condensation.dropped
    assert condensation.dropped_seqs == fold["dropped_seqs"]
    assert condensation.cache_prefix_preserved is True
    assert condensation.body_from == 2
    assert condensation.cache_prefix_messages == 1
    # No model summary, and the record does not pretend otherwise.
    assert condensation.summary == ""
    # A fold is not appended to the in-memory condensation list: that runs on
    # every append once the budget is hit, and a per-append record would make
    # the object grow as fast as the history it bounds.
    assert memory.condensations == []


def test_the_condensation_record_survives_a_snapshot_round_trip():
    """A restored run keeps the records it can be asked to explain itself with."""
    memory = _memory(compaction_thrash_limit=0)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    for index in range(6):
        memory.record_tool_result("read", True, ("body " * 300), turn=index)
    receipt = memory.compact_tokens(
        limit_tokens=800,
        summarize=lambda transcript, turns: ("the summary", "model_summary"),
        estimator=TokenEstimator("heuristic"),
    )
    assert receipt is not None
    before = memory.render()
    snapshot = memory.snapshot()

    memory.reset()
    assert memory.condensations == []
    memory.restore(snapshot)
    assert memory.render() == before
    assert len(memory.condensations) == 1
    restored = memory.condensations[0]
    assert restored.compaction_id == receipt["compaction_id"]
    assert restored.summary == "the summary"
    assert restored.dropped_seqs == receipt["dropped_seqs"]
    assert restored.cache_prefix_preserved is True
    # The lookup is by id, so a receipt and its record cannot drift apart.
    assert memory.condensation(receipt["compaction_id"]) is restored
    assert memory.condensation("nope") is None


# ---------------------------------------------------------------------------
# 4. A thrashing compaction aborts with a reason
# ---------------------------------------------------------------------------


def test_a_thrashing_compaction_aborts_with_a_reason():
    """Compaction that cannot do its job must fail, not repeat.

    The shape is a request whose PROTECTED region is already over the trigger:
    compaction is then called once per turn and can never change anything. The
    guard counts the unbroken run and aborts on the third, with the numbers in
    the message.
    """
    memory = _memory(compaction_thrash_limit=3)
    memory.seed(
        [
            {"role": "system", "content": "SYSTEM " * 900},
            {"role": "user", "content": "U"},
        ]
    )
    estimator = TokenEstimator("heuristic")
    aborted = None
    for turn in range(1, 8):
        memory.record_note(f"note {turn}", turn=turn)
        try:
            memory.compact_tokens(
                limit_tokens=200,
                summarize=lambda transcript, turns: ("a summary", "model_summary"),
                estimator=estimator,
            )
        except CompactionThrashError as exc:
            aborted = exc
            break
    assert aborted is not None, "a run that compacts without progress must abort"
    assert aborted.streak == 3
    assert aborted.limit == 3
    assert "3 consecutive compactions" in str(aborted)
    assert str(aborted.reason)
    payload = aborted.as_dict()
    assert payload["reasons"] == list(THRASH_REASONS)
    assert payload["limit"] == 3
    assert payload["before_tokens"] >= payload["after_tokens"]


def test_an_aborted_compaction_does_not_compact_again():
    """A guard whose answer changes between calls is not a guard.

    After the abort the memory is terminal: the next compaction attempt raises
    the SAME error rather than quietly starting over, because a run that
    compacts again after deciding it cannot is exactly the loop the guard
    exists to stop.
    """
    memory = _memory(compaction_thrash_limit=2)
    memory.seed(
        [
            {"role": "system", "content": "SYSTEM " * 900},
            {"role": "user", "content": "U"},
        ]
    )
    estimator = TokenEstimator("heuristic")
    first = None
    for turn in range(1, 6):
        memory.record_note(f"note {turn}", turn=turn)
        try:
            memory.compact_tokens(
                limit_tokens=100,
                summarize=lambda transcript, turns: ("a summary", "model_summary"),
                estimator=estimator,
            )
        except CompactionThrashError as exc:
            first = exc
            break
    assert first is not None
    memory.record_note("one more", turn=99)
    with pytest.raises(CompactionThrashError) as again:
        memory.compact_tokens(
            limit_tokens=100,
            summarize=lambda transcript, turns: ("a summary", "model_summary"),
            estimator=estimator,
        )
    assert str(again.value) == str(first)
    # The evidence was written BEFORE the abort, so it outlives the exception.
    assert memory.thrash_error is first


def test_a_healthy_long_session_is_never_accused_of_thrashing():
    """The guard must not fire on a run that is compacting correctly.

    This is the control for every other thrash test: many compactions, each one
    getting the request back under its trigger, and a streak that keeps resetting.
    """
    memory = _memory(compaction_thrash_limit=3)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    estimator = TokenEstimator("heuristic")
    compactions = 0
    for turn in range(1, 14):
        memory.record_tool_result(
            "read", True, ("payload %d " % turn) * 400, turn=turn, target="app.py"
        )
        receipt = memory.compact_tokens(
            limit_tokens=4000,
            summarize=lambda transcript, turns: ("a summary", "model_summary"),
            estimator=estimator,
        )
        if receipt is not None:
            compactions += 1
            assert receipt["after_tokens"] < receipt["limit_tokens"]
    assert compactions >= 3, "the control must actually compact repeatedly"
    assert memory.no_progress_streak == 0
    assert memory.thrash_error is None


def test_the_thrash_policy_is_config_driven_and_its_off_arm_is_real():
    """A guard that only exists when asked for is not a guard; one that cannot
    be turned off is not a budget."""
    default = compaction_thrash_policy_from_config({})
    assert default.limit == DEFAULT_COMPACTION_THRASH_LIMIT
    assert default.enabled is True

    configured = compaction_thrash_policy_from_config(
        {"compaction_thrash_limit": 7, "compaction_min_reclaim_tokens": 25}
    )
    assert configured.limit == 7
    assert configured.min_reclaim_tokens == 25

    # An unusable value degrades to the documented default rather than to zero,
    # because `limit=0` means "the guard is off" and a typo must not disable it.
    assert (
        compaction_thrash_policy_from_config(
            {"compaction_thrash_limit": "nonsense"}
        ).limit
        == DEFAULT_COMPACTION_THRASH_LIMIT
    )

    off = CompactionThrashPolicy(limit=0)
    assert off.enabled is False
    assert off.decide(99) == ""
    assert off.note({"limit_tokens": 10, "after_tokens": 99}) == 1

    # Progress means "the compaction achieved its purpose", not "the number went
    # down": a compaction can reclaim a token and still have accomplished nothing.
    policy = CompactionThrashPolicy(limit=2)
    assert policy.made_progress({"limit_tokens": 100, "after_tokens": 99}) is True
    assert policy.made_progress({"limit_tokens": 100, "after_tokens": 100}) is False
    assert policy.made_progress({"limit_tokens": 100, "after_tokens": 4000}) is False
    # With no trigger in the receipt the reclaim floor is the honest test.
    assert policy.made_progress({"reclaimed_tokens": 0}) is False
    assert policy.made_progress({"reclaimed_tokens": 3}) is True
    assert policy.note({"limit_tokens": 10, "after_tokens": 40}, streak=7) == 8
    assert policy.note({"limit_tokens": 10, "after_tokens": 2}, streak=7) == 0


def test_a_thrashing_run_ends_failed_and_never_completed(tmp_path):
    """End to end: the abort reaches the run as a FAILURE with its reason.

    The kernel turns a strategy exception into a ``failed`` result carrying the
    message, so this is where "fail loudly" stops being a library promise. It
    also cannot be a completion of any kind: the guard runs during context
    preparation, long before any completion status is decided.
    """
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    # One tool result LARGER than the whole window. The newest turn is never
    # droppable, so a request like this cannot be brought under its trigger by
    # any compaction - which is the shape the guard exists for, and it is the
    # shape a runaway tool produces.
    runaway = ("def helper(value):\n    return value * 2 + 1\n" * 1500)[:60000]
    model = ScriptedModel(
        [
            json.dumps({"tool": "write", "path": "big.txt", "content": runaway}),
            json.dumps({"tool": "read", "path": "big.txt"}),
            json.dumps({"tool": "read", "path": "big.txt"}),
            json.dumps({"tool": "read", "path": "big.txt"}),
            json.dumps({"tool": "read", "path": "big.txt"}),
            json.dumps({"tool": "finish", "answer": "done"}),
        ]
    )
    result = _kernel(
        repo,
        log_root,
        model,
        **_wide_budget(
            context_window_tokens=4096,
            context_compaction_fraction=0.2,
            compaction_thrash_limit=2,
            context_reinjection_enabled=False,
            # The storage cap is switched off here ON PURPOSE: this test is about
            # a request no compaction can shrink, and a 4,000-token storage cap
            # would make every turn small enough to compact. The runaway-output
            # cap is pinned separately, with the cap on.
            tool_output_token_limit=0,
            agent_max_read_chars=200000,
            max_tool_fanout_chars=400000,
        ),
    ).run(_spec(repo, run_id="thrash-run"))

    assert result.status == "failed"
    assert "compaction made no progress" in str(result.error or "")
    assert result.status not in {
        "completed_unverified",
        "completed_verified",
        "success",
    }
    # The abort is on the record, not only in the message.
    thrash_rows = [
        row
        for row in _journal_rows(log_root, "thrash-run")
        if row.get("record") == "compaction_thrash"
    ]
    assert thrash_rows, "the abort must be journalled before it is raised"
    assert thrash_rows[-1]["reason"]
    # The limit that bound is the SHIPPED default, not the `2` this config
    # asked for. `harness/agent_kernel/strategy.py` builds the conversation with
    # four explicit arguments and does not yet pass the thrash keys through, so
    # the constructor default applies; the wiring is the cross-terminal request
    # recorded in `harness/AGENTS.md`. The guard is live either way, and this
    # assertion is what will flip the day it is wired.
    assert thrash_rows[-1]["limit"] == DEFAULT_COMPACTION_THRASH_LIMIT
    assert thrash_rows[-1]["streak"] == DEFAULT_COMPACTION_THRASH_LIMIT
    # A thrashing run minted no completion event at all.
    finishes = [
        row
        for row in _events(log_root, "thrash-run", "run_finished")
        if str((row.get("result") or {}).get("status") or "") != "failed"
    ]
    assert not finishes


def test_a_configured_thrash_limit_bounds_the_abort():
    """The knob is real: a limit of 2 aborts on the second, not the third.

    Measured at the memory level, because that is where the policy lives. The
    kernel-level wiring of the key is the cross-terminal request named above;
    this test is what proves the limit is honoured once it arrives.
    """
    for limit in (2, 5):
        memory = _memory(compaction_thrash_limit=limit)
        memory.seed(
            [
                {"role": "system", "content": "SYSTEM " * 900},
                {"role": "user", "content": "U"},
            ]
        )
        estimator = TokenEstimator("heuristic")
        aborted = None
        for turn in range(1, 12):
            memory.record_note(f"note {turn}", turn=turn)
            try:
                memory.compact_tokens(
                    limit_tokens=100,
                    summarize=lambda transcript, turns: ("a summary", "model_summary"),
                    estimator=estimator,
                )
            except CompactionThrashError as exc:
                aborted = exc
                break
        assert aborted is not None
        assert aborted.streak == limit
        assert aborted.limit == limit


# ---------------------------------------------------------------------------
# 5. The existing reversibility guarantee still holds
# ---------------------------------------------------------------------------


def test_restore_compaction_still_undoes_one_condensation(tmp_path):
    """Dropping the reversibility guarantee would be a regression, not a change.

    ``restore_compaction`` appends a ``restore`` row that deactivates one
    condensation and therefore un-drops exactly its sequences. History is never
    rewritten, and the rebuilt snapshot carries the dropped turns back.
    """
    journal = ConversationJournal(tmp_path / "reversible")
    memory = _memory(journal=journal.append, compaction_thrash_limit=0)
    memory.seed([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    estimator = TokenEstimator("heuristic")
    for index in range(8):
        memory.record_tool_result(
            "read", True, f"MARKER-{index} " + ("filler " * 200), turn=index
        )
    before = memory.render()
    receipt = memory.compact_tokens(
        limit_tokens=1200,
        summarize=lambda transcript, turns: ("a summary", "model_summary"),
        estimator=estimator,
    )
    assert receipt is not None
    assert memory.render() != before

    dropped = set(receipt["dropped_seqs"])
    assert dropped
    # While the condensation is ACTIVE, its sequences are out of the live view.
    live_seqs = {int(row["seq"]) for row in journal.live_snapshot()["history"]}
    assert not (dropped & live_seqs)

    restored = journal.restore_compaction(receipt["compaction_id"])
    assert restored is not None
    # And after the restore they are all back: a rollback that restored nothing
    # would be indistinguishable from a rollback that failed.
    assert dropped <= {int(row["seq"]) for row in restored["history"]}
    # Rolling one back twice honestly reports nothing to restore.
    assert journal.restore_compaction(receipt["compaction_id"]) is None
    # The journal is append-only: the restore is a new row, not a rewrite.
    kinds = [row.get("record") for row in journal.load()]
    assert "compaction" in kinds and "restore" in kinds
    assert kinds.index("compaction") < kinds.index("restore")
    # And the restored turns are the ORIGINAL text, not a re-render of the
    # handoff: the condensation record plus the journal are the mechanism, and
    # the memory object never held the content in the first place.
    contents = contents_by_seq(journal.load())
    assert all(seq in contents for seq in dropped)


def test_the_kernel_keeps_the_cached_prefix_across_a_real_compaction(tmp_path):
    """The proof through the REAL kernel, with a scripted model as the witness.

    The witness compares the provider's own prefix digest of the request it was
    handed, turn by turn, and fails if ANY turn after a compaction changed it.
    A compaction that summarised the head away cannot pass this.
    """
    repo = _repo(tmp_path)
    log_root = tmp_path / "logs"
    digests = []

    def witness(model, messages, kwargs):
        # The COMPACTION call is a different request with its own prompt, so it
        # is not part of the turn's cached prefix. The step label is what tells
        # the two apart, and ignoring it would make this test compare two
        # unrelated prompts instead of measuring prefix stability.
        if not str(kwargs.get("step") or "").startswith("agent-"):
            return
        digests.append(prefix_identity(list(messages), tools=TOOLS).sha256)

    model = ScriptedModel(_growth_replies(9), on_call=witness)
    result = _kernel(repo, log_root, model, **_wide_budget()).run(
        _spec(repo, run_id="prefix-run")
    )
    assert result.status == "completed_unverified"

    compactions = _events(log_root, "prefix-run", "context_compacted")
    assert compactions, "the run must actually compact for this to mean anything"
    assert len(digests) >= 2
    # One digest for the whole run: the head never moved.
    assert len(set(digests)) == 1, f"the cached prefix changed: {sorted(set(digests))}"
    for receipt in compactions:
        assert receipt["cache_prefix"]["preserved"] is True
        assert (
            receipt["cache_prefix"]["before"]["sha256"]
            == receipt["cache_prefix"]["after"]["sha256"]
        )
        # The replayable record rides the same event, so a reader of the trace
        # alone can see what was dropped and what replaced it.
        assert receipt["condensation"]["dropped"]
        assert receipt["condensation"]["dropped_seqs"] == receipt["dropped_seqs"]
        assert (
            receipt["condensation"]["reversible"]["un_drops"] == receipt["dropped_seqs"]
        ), "the record's own reversibility receipt must name the same sequences"
        assert receipt["reversible"]["dropped_seqs"] == receipt["dropped_seqs"], (
            "the strategy's reversibility receipt must still agree"
        )


def test_the_thrash_guard_reaches_the_live_conversation_memory():
    """The kernel's memory is configured from the run's merged config.

    ``AgentKernel`` merges ``harness.config.DEFAULTS`` into its config, so a
    configured limit is one call away from the memory that enforces it. Until
    the strategy passes it, the constructor default is the same number, so the
    guard is live either way — this test pins the wiring point and the value so
    the one-line handoff cannot be forgotten silently.
    """
    from harness.config import DEFAULTS

    assert DEFAULTS["compaction_thrash_limit"] == DEFAULT_COMPACTION_THRASH_LIMIT
    assert DEFAULTS["compaction_min_reclaim_tokens"] == 1
    budget = budget_from_config(
        {"compaction_thrash_limit": 5, "context_window_tokens": WINDOW}
    )
    assert budget.window == WINDOW  # the thrash keys do not disturb the window
    memory = _memory()
    memory.configure_compaction({"compaction_thrash_limit": 5})
    assert memory.thrash_policy.limit == 5
    assert memory.thrash_policy.enabled is True
    # An absent key means the documented default, not "off".
    memory.configure_compaction({})
    assert memory.thrash_policy.limit == DEFAULT_COMPACTION_THRASH_LIMIT
    # And `0` is a real off arm.
    memory.configure_compaction({"compaction_thrash_limit": 0})
    assert memory.thrash_policy.enabled is False


# ---------------------------------------------------------------------------
# The invariants this round is not allowed to touch
# ---------------------------------------------------------------------------


def test_the_verifier_gate_is_untouched_by_context_compaction():
    """Nothing in this round may make an unverified run look verified.

    The completion mint is keyed on the verifier's own three checks, and the
    context budget is absent from that condition by construction. This reads the
    source rather than trusting a comment, because a comment is exactly the thing
    that goes stale, and it checks the two files this round actually edited.
    """
    kernel_dir = Path(harness_agent_kernel.__file__).parent

    completion = (kernel_dir / "completion.py").read_text(encoding="utf-8")
    # The mint is still all three checks, and still nothing else.
    assert "target_test_passed" in completion
    assert "regression_passed" in completion
    mint_lines = [
        line.strip()
        for line in completion.splitlines()
        if "and regression_passed" in line or "and not verification" in line
    ]
    assert mint_lines, "the mint condition must still be readable in the source"
    joined = " ".join(mint_lines)
    for forbidden in (
        "compactions",
        "condensation",
        "thrash",
        "cache_prefix",
        "compaction",
    ):
        assert forbidden not in joined, (
            f"the completion mint must not depend on {forbidden}"
        )

    # The two files this round edited name NONE of the mint's vocabulary, so a
    # future edit that reached for it would fail here first.
    for module in ("budget.py", "conversation.py"):
        text = (kernel_dir / module).read_text(encoding="utf-8")
        assert "target_test_passed" not in text
        assert "completed_verified" not in text
        assert "completed_unverified" not in text

    # And the abort this round introduces can only ever produce `failed`: the
    # kernel's strategy-exception path is the one that catches it.
    kernel = (kernel_dir / "kernel.py").read_text(encoding="utf-8")
    assert "except Exception as exc:" in kernel
    assert "status=CompletionStatus.FAILED" in kernel
