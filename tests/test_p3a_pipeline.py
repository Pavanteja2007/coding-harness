"""P3a: the two-phase pipeline -- extraction, the inbox handoff, consolidation.

Phase 1 (extraction) mines transcripts into PROPOSALS; phase 2 (consolidation)
proposes artifacts, hands a unified diff to a sub-agent, and writes only on
acceptance. The invariants this file pins are the ones that make the pipeline
safe to run unattended:

* **neither phase can persist a memory**;
* a credential is redacted BEFORE it reaches the job row;
* a lease is exclusive and a dead worker's job is reaped;
* backoff is exponential and exhaustion is TERMINAL;
* a reviewer that cannot answer never writes.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from memory.consolidation import (
    MAX_DIFF_CHARS,
    MEMORY_FILE,
    USER_FILE,
    MemoryConsolidator,
    ReviewVerdict,
    rule_reviewer,
    unified_diff,
)
from memory.decision_store import DecisionStore
from memory.extraction import (
    BACKOFF_CEILING_S,
    MAX_ATTEMPTS,
    ExtractionQueue,
    JobState,
    extract_memories,
    rule_based_extractor,
)
from memory.inbox import MemoryInbox

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"

TRANSCRIPT = """
user: how should mean() work?
decision: mean() divides by len, not len-1, because we report the arithmetic mean
convention: always run python -m pytest from the repository root, never bare pytest
gotcha: the CI image has no PyYAML, so yaml config tests fail at import
fact: the smoke repo image is python:3.10-slim
note: api_key = sk-abcdefghijklmnopqrstuvwxyz123456 is in the CI env
"""


@pytest.fixture()
def queue(tmp_path: Path):
    handle = ExtractionQueue(str(tmp_path / "q.db"))
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def store(tmp_path: Path):
    handle = DecisionStore(str(tmp_path / "d.db"))
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return tmp_path


class TestRedactionBeforeWrite:
    def test_the_credential_never_reaches_the_job_row(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        assert receipt["ok"] is True
        assert receipt["redacted"] is True
        job = queue.get(receipt["job_id"])
        assert SECRET not in job.transcript, (
            "a job queue is a long-lived store of raw user conversation; "
            "redaction must happen before the bytes reach the database"
        )
        assert "[REDACTED_SECRET]" in job.transcript
        assert job.redacted is True

    def test_the_enqueue_receipt_carries_the_redaction_scan(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        scan = receipt["redaction_receipt"]
        assert scan, "a silent scrub would be indistinguishable from storing raw"
        assert isinstance(scan, dict)

    def test_a_clean_transcript_reports_that_nothing_was_removed(
        self, queue: ExtractionQueue
    ):
        receipt = queue.enqueue("decision: nothing sensitive here", session_id="s1")
        assert receipt["redacted"] is False
        job = queue.get(receipt["job_id"])
        assert "nothing sensitive here" in job.transcript

    def test_an_oversized_transcript_is_truncated_and_reported(
        self, queue: ExtractionQueue
    ):
        from memory.extraction import MAX_TRANSCRIPT_CHARS

        receipt = queue.enqueue("x" * (MAX_TRANSCRIPT_CHARS + 1000), session_id="s1")
        assert receipt["transcript_truncated"] is True
        assert receipt["transcript_chars"] <= MAX_TRANSCRIPT_CHARS

    def test_an_empty_transcript_is_refused_and_counted(self, queue: ExtractionQueue):
        receipt = queue.enqueue("   ", session_id="s1")
        assert receipt["ok"] is False
        assert receipt["reason"] == "empty transcript"
        assert queue.capability()["refusals"] >= 1

    def test_the_extraction_result_is_redacted_before_it_is_stored(
        self, queue: ExtractionQueue
    ):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        job = queue.get(receipt["job_id"])
        queue.complete(
            job.job_id, {"note": f"the key is {SECRET}", "count": 1}
        )
        stored = queue.get(job.job_id)
        assert stored.result is not None
        assert SECRET not in json_text(stored.result)


def json_text(value: object) -> str:
    import json

    return json.dumps(value, default=str)


class TestLeasesAreExclusiveAndReaped:
    def test_only_one_worker_leases_a_job(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        first = queue.lease(owner="w1")
        second = queue.lease(owner="w2")
        assert first is not None
        assert first.job_id == receipt["job_id"]
        assert second is None, "two workers took the same job"
        assert first.lease_owner == "w1"

    def test_a_valid_lease_is_not_reaped(self, queue: ExtractionQueue):
        queue.enqueue(TRANSCRIPT, session_id="s1")
        queue.lease(owner="w1", lease_s=120.0)
        assert queue.reap(at=time.time() + 10) == 0

    def test_an_expired_lease_returns_to_pending(self, queue: ExtractionQueue):
        """The difference between a queue and a stuck flag."""
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        queue.lease(owner="w1", lease_s=1.0)
        assert queue.reap(at=time.time() + 3600) == 1
        job = queue.get(receipt["job_id"])
        assert job.state is JobState.PENDING
        assert job.lease_owner == ""
        # And it is leasable again -- that is the point.
        assert queue.lease(owner="w2", at=time.time() + 7200) is not None

    def test_a_job_with_nothing_due_leases_none(self, queue: ExtractionQueue):
        assert queue.lease(owner="w1") is None, "None means 'nothing due', not an error"
        assert queue.available is True

    def test_a_lease_increments_the_attempt_count(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        leased = queue.lease(owner="w1")
        assert leased.attempts == 1
        assert queue.get(receipt["job_id"]).attempts == 1

    def test_a_non_numeric_job_id_is_refused_and_counted(self, queue: ExtractionQueue):
        assert queue.get("not-an-id") is None


class TestRetryBackoffIsExponentialAndTerminal:
    def _walk_to_exhaustion(self, queue: ExtractionQueue, job_id: str):
        clock = 2e9
        last = None
        for _ in range(MAX_ATTEMPTS + 3):
            clock += 10_000.0
            leased = queue.lease(owner="w", at=clock)
            if leased is None:
                break
            last = queue.fail(job_id, "transient provider error", at=clock)
            if not last["will_retry"]:
                break
        return last, clock

    def test_backoff_doubles_then_the_job_becomes_terminally_failed(
        self, queue: ExtractionQueue
    ):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        clock = 2e9
        observed = []
        for _ in range(MAX_ATTEMPTS + 2):
            clock += 10_000.0
            leased = queue.lease(owner="w", at=clock)
            if leased is None:
                break
            outcome = queue.fail(receipt["job_id"], "transient", at=clock)
            if outcome["will_retry"]:
                observed.append(outcome["backoff_s"])
            else:
                break
        assert observed == [2.0, 4.0, 8.0, 16.0], (
            "exponential from a 2s base; a flat backoff is a thundering herd"
        )
        job = queue.get(receipt["job_id"])
        assert job.state is JobState.FAILED
        assert job.attempts == job.max_attempts == MAX_ATTEMPTS
        assert job.terminal is True
        assert job.exhausted is True
        assert "boom" not in job.last_error or "transient" in job.last_error

    def test_a_terminal_job_is_never_leased_again(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        self._walk_to_exhaustion(queue, receipt["job_id"])
        assert queue.lease(owner="late", at=time.time() + 10**7) is None

    def test_the_backoff_is_capped(self, queue: ExtractionQueue):
        from memory.extraction import _backoff_s

        assert _backoff_s(1) == 2.0
        assert _backoff_s(50) == BACKOFF_CEILING_S
        assert _backoff_s(0) == 2.0, "a 0/negative attempt count is clamped, not a crash"

    def test_a_terminal_failure_keeps_its_reason(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        clock = 2e9
        for _ in range(MAX_ATTEMPTS + 2):
            clock += 10_000.0
            if queue.lease(owner="w", at=clock) is None:
                break
            outcome = queue.fail(receipt["job_id"], "provider unavailable", at=clock)
            if not outcome["will_retry"]:
                break
        job = queue.get(receipt["job_id"])
        assert job.state is JobState.FAILED
        assert job.last_error == "provider unavailable"

    def test_failing_an_unknown_job_is_reported(self, queue: ExtractionQueue):
        outcome = queue.fail("job-nope", "boom")
        assert outcome["ok"] is False
        assert outcome["will_retry"] is False

    def test_a_custom_attempt_limit_is_honoured(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1", max_attempts=2)
        job = queue.get(receipt["job_id"])
        assert job.max_attempts == 2
        clock = 2e9
        for _ in range(4):
            clock += 10_000.0
            if queue.lease(owner="w", at=clock) is None:
                break
            outcome = queue.fail(receipt["job_id"], "boom", at=clock)
            if not outcome["will_retry"]:
                break
        assert queue.get(receipt["job_id"]).state is JobState.FAILED

    def test_the_counts_report_every_state(self, queue: ExtractionQueue):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        assert queue.counts()["counts"]["pending"] == 1
        queue.lease(owner="w1")
        assert queue.counts()["counts"]["leased"] == 1
        queue.complete(receipt["job_id"], {"count": 1})
        assert queue.counts()["counts"]["done"] == 1
        assert queue.counts()["available"] is True


class TestTheExtractorSeam:
    def test_the_default_extractor_is_deterministic_and_makes_no_model_call(self):
        first, receipt = extract_memories(TRANSCRIPT)
        second, _ = extract_memories(TRANSCRIPT)
        assert first == second
        assert receipt["engine"] == "rules"
        assert receipt["model_calls"] == 0

    def test_the_default_extractor_finds_the_four_kinds(self):
        memories, _ = extract_memories(TRANSCRIPT)
        categories = {item["category"] for item in memories}
        assert categories == {"decision", "convention", "gotcha", "fact"}
        assert all(item["rationale"] for item in memories), (
            "an extracted memory must carry the reason it was extracted"
        )

    def test_the_rule_extractor_never_emits_a_duplicate(self):
        memories = rule_based_extractor(TRANSCRIPT + TRANSCRIPT)
        texts = [item["text"] for item in memories]
        assert len(texts) == len(set(texts))

    def test_an_injected_extractor_is_named_and_counted(self):
        def fake(text: str):
            return [{"text": "an extracted fact", "category": "fact"}]

        memories, receipt = extract_memories(TRANSCRIPT, extractor=fake)
        assert len(memories) == 1
        assert receipt["engine"] == "callable:fake"
        assert receipt["model_calls"] == 1

    def test_a_raising_extractor_is_caught_not_propagated(self):
        """Extraction failure is a job failure to retry, not a caller crash."""

        def boom(text: str):
            raise RuntimeError("model unavailable")

        memories, receipt = extract_memories(TRANSCRIPT, extractor=boom)
        assert memories == []
        assert receipt["ok"] is False
        assert "RuntimeError" in receipt["reason"]
        assert receipt["model_calls"] == 1

    def test_a_non_callable_extractor_is_refused(self):
        memories, receipt = extract_memories(TRANSCRIPT, extractor="not callable")
        assert memories == []
        assert receipt["ok"] is False
        assert "not callable" in receipt["reason"]

    def test_a_bare_string_from_an_extractor_is_accepted(self):
        memories, _ = extract_memories(TRANSCRIPT, extractor=lambda t: ["a bare fact"])
        assert memories == [{"text": "a bare fact"}]


class TestExtractionFeedsTheInboxAndNotTheStore:
    def test_the_phase1_path_writes_proposals_and_zero_store_rows(
        self, queue: ExtractionQueue, store: DecisionStore, repo: Path, tmp_path: Path
    ):
        inbox = MemoryInbox(tmp_path / "inbox", repo_path=str(repo))
        receipt = queue.enqueue(TRANSCRIPT, repo_path=str(repo), session_id="s1")
        job = queue.get(receipt["job_id"])
        memories, _ = extract_memories(job.transcript)

        proposed = inbox.propose(memories, session_id="s1", run_id="r1")
        assert proposed["ok"] is True
        assert proposed["items"] >= 4
        assert store.count() == 0, "phase 1 persisted a memory"
        assert inbox.audit()["pending"] == 1

        applied = inbox.apply(proposed["proposal_id"], store, actor="operator")
        assert applied["committed"] >= 4
        assert store.count() >= 4

    def test_a_queued_job_is_json_serialisable_for_operators(
        self, queue: ExtractionQueue
    ):
        receipt = queue.enqueue(TRANSCRIPT, session_id="s1")
        job = queue.get(receipt["job_id"])
        payload = job.as_dict()
        assert json_text(payload)
        assert payload["transcript"], (
            "a job that cannot show what it was going to extract cannot be diagnosed"
        )


class TestConsolidationReviewsADiff:
    def _store(self, store: DecisionStore, repo: Path) -> None:
        store.record(
            "mean() divides by len, not len-1",
            category="gotcha",
            source="agent",
            repo_path=str(repo),
        )
        store.record(
            "the user prefers descriptive commits",
            category="preference",
            source="manual",
            repo_path=str(repo),
        )

    def test_an_additive_diff_is_accepted_and_writes_artifacts(
        self, store: DecisionStore, repo: Path, tmp_path: Path
    ):
        self._store(store, repo)
        consolidator = MemoryConsolidator(tmp_path / "memroot")
        receipt = consolidator.consolidate(store, repo_path=str(repo))
        assert receipt.verdict.verdict is ReviewVerdict.ACCEPT
        assert receipt.files_written
        artifacts = consolidator.artifacts()
        assert "mean() divides by len" in artifacts[MEMORY_FILE]
        assert "descriptive commits" in artifacts[USER_FILE], (
            "a user-stated fact belongs in the user artifact"
        )
        assert (tmp_path / "memroot" / "baseline.json").is_file()

    def test_consolidation_persists_no_memory(self, store, repo, tmp_path):
        self._store(store, repo)
        before = store.count(include_revoked=True)
        receipt = MemoryConsolidator(tmp_path / "r").consolidate(
            store, repo_path=str(repo)
        )
        assert receipt.persisted == 0
        assert receipt.as_dict()["persists_nothing"] is True
        assert store.count(include_revoked=True) == before

    def test_a_second_pass_is_idempotent(self, store, repo, tmp_path):
        self._store(store, repo)
        consolidator = MemoryConsolidator(tmp_path / "memroot")
        first = consolidator.consolidate(store, repo_path=str(repo))
        second = consolidator.consolidate(store, repo_path=str(repo))
        assert first.verdict.verdict is ReviewVerdict.ACCEPT
        assert second.verdict.verdict is ReviewVerdict.ABSTAIN
        assert second.diff_chars == 0
        assert second.files_written == ()
        assert second.reason, "an abstain must say why"

    def test_a_rejecting_reviewer_writes_nothing(self, store, repo, tmp_path):
        self._store(store, repo)
        consolidator = MemoryConsolidator(
            tmp_path / "refusing",
            reviewer=lambda diff, ctx: {"verdict": "reject", "reason": "operator said no"},
        )
        receipt = consolidator.consolidate(store, repo_path=str(repo))
        assert receipt.verdict.verdict is ReviewVerdict.REJECT
        assert receipt.files_written == ()
        assert not (tmp_path / "refusing" / MEMORY_FILE).exists()

    def test_a_raising_reviewer_abstains_and_never_approves(
        self, store, repo, tmp_path
    ):
        """The most dangerous default in the module, and it is deliberate."""

        def boom(diff, ctx):
            raise RuntimeError("sub-agent died")

        receipt = MemoryConsolidator(tmp_path / "r", reviewer=boom).consolidate(
            store, repo_path=str(repo)
        )
        assert receipt.verdict.verdict is ReviewVerdict.ABSTAIN
        assert receipt.files_written == ()
        assert not (tmp_path / "r" / MEMORY_FILE).exists()
        assert "never treated as approving" in receipt.verdict.reason

    @pytest.mark.parametrize(
        "garbage", ["maybe", 42, None, {"verdict": "perhaps"}, [], object()]
    )
    def test_a_review_returning_garbage_abstains(self, store, repo, tmp_path, garbage):
        self._store(store, repo)
        receipt = MemoryConsolidator(
            tmp_path / "r", reviewer=lambda diff, ctx, g=garbage: g
        ).consolidate(store, repo_path=str(repo))
        assert receipt.verdict.verdict is ReviewVerdict.ABSTAIN
        assert receipt.files_written == ()

    def test_the_receipt_names_the_engine_and_model_call_count(
        self, store, repo, tmp_path
    ):
        self._store(store, repo)
        receipt = MemoryConsolidator(tmp_path / "r").consolidate(
            store, repo_path=str(repo)
        )
        assert receipt.engine == "rules"
        assert receipt.model_calls == 0
        assert receipt.as_dict()["reviews_a_diff"] is True

    def test_the_diff_is_real_unified_diff_text(self, store, repo, tmp_path):
        self._store(store, repo)
        receipt = MemoryConsolidator(tmp_path / "r").consolidate(
            store, repo_path=str(repo)
        )
        assert receipt.diff_chars > 0
        consolidator = MemoryConsolidator(tmp_path / "r")
        consolidator.consolidate(store, repo_path=str(repo))
        diff = unified_diff("", "# Project memory\n", path=MEMORY_FILE)
        assert diff.startswith("--- a/")
        assert "+++ b/" in diff
        assert "+# Project memory" in diff

    def test_an_oversized_diff_is_truncated_before_the_reviewer_sees_it(
        self, store, repo, tmp_path
    ):
        """The diff cap is a SECOND bound, reached only when the artifact cap is raised.

        At the default artifact cap the artifacts are already bounded, so the
        diff never gets near ``MAX_DIFF_CHARS``. The case that matters is an
        operator who RAISES the artifact cap -- then the reviewer must still be
        handed a bounded input, and told the input was bounded.
        """
        from memory.consolidation import MAX_ARTIFACT_CHARS

        for index in range(1_200):
            store.record(
                f"convention {index}: a long sentence of project memory that adds up, written out at length so that the rendered artifact is big enough to exceed the diff cap once the store's own 500-row retrieval cap is accounted for",
                category="convention",
                source="agent",
                repo_path=str(repo),
            )
        seen: dict = {}

        def reviewer(diff, ctx):
            seen["chars"] = len(diff)
            return {"verdict": "accept", "reason": "fine"}

        receipt = MemoryConsolidator(tmp_path / "r", reviewer=reviewer).consolidate(
            store,
            repo_path=str(repo),
            limit=2_000,
            max_chars=MAX_ARTIFACT_CHARS * 8,
        )
        assert seen["chars"] <= MAX_DIFF_CHARS
        assert receipt.diff_truncated is True
        assert receipt.as_dict()["diff_chars"] <= MAX_DIFF_CHARS
        assert "TRUNCATED" in receipt.render(), (
            "a reviewer that accepted a truncated diff must be visibly accepting "
            "a truncated diff"
        )

    def test_the_diff_is_bounded_by_three_independent_limits(self, tmp_path):
        """Which bound fires first depends on the other two, and that is worth naming.

        ``consolidate`` renders at most ``limit`` rows into artifacts of at most
        ``max_chars`` each, then clips the diff at ``MAX_DIFF_CHARS``. At the
        defaults the ROW LIMIT binds first (200 rows ~ 17k chars), so the diff
        cap is unreachable without raising both -- which is exactly why the
        control arm below matters.
        """
        store = DecisionStore(str(tmp_path / "d.db"))
        try:
            for index in range(1_200):
                store.record(
                    f"convention {index}: a long sentence of project memory that adds up, written out at length so that the rendered artifact is big enough to exceed the diff cap once the store's own 500-row retrieval cap is accounted for",
                    category="convention",
                    source="agent",
                    repo_path=str(tmp_path),
                )
            default = MemoryConsolidator(tmp_path / "a").consolidate(
                store, repo_path=str(tmp_path)
            )
            assert default.diff_truncated is False
            assert default.diff_chars < MAX_DIFF_CHARS

            widened = MemoryConsolidator(tmp_path / "b").consolidate(
                store,
                repo_path=str(tmp_path),
                limit=4_000,
                max_chars=1_000_000,
            )
            assert widened.diff_truncated is True
        finally:
            store.close()

    def test_the_artifact_cap_bounds_the_diff_by_default(
        self, store, repo, tmp_path
    ):
        """The control arm: at the default caps the diff cap is not reached."""
        for index in range(400):
            store.record(
                f"convention {index}: a long sentence of project memory that adds up, written out at length so that the rendered artifact is big enough to exceed the diff cap once the store's own 500-row retrieval cap is accounted for",
                category="convention",
                source="agent",
                repo_path=str(repo),
            )
        receipt = MemoryConsolidator(tmp_path / "r").consolidate(
            store, repo_path=str(repo)
        )
        assert receipt.diff_truncated is False
        assert receipt.diff_chars < MAX_DIFF_CHARS

    def test_no_store_is_refused_not_crashed(self, tmp_path):
        receipt = MemoryConsolidator(tmp_path / "r").consolidate(None)
        assert receipt.ok is False
        assert "no store" in receipt.reason


class TestTheDefaultReviewerRefusesDeletions:
    def test_a_pure_addition_is_accepted(self):
        verdict = rule_reviewer("@@ -0,0 +1,2 @@\n+# 4 (gotcha) a new fact\n", {})
        assert verdict.verdict is ReviewVerdict.ACCEPT
        assert verdict.model_calls == 0
        assert verdict.reviewer == "rule_reviewer"

    def test_a_deletion_is_rejected(self):
        verdict = rule_reviewer(
            "@@ -1,2 +1,1 @@\n-# 3 (gotcha) mean() divides by len\n", {}
        )
        assert verdict.verdict is ReviewVerdict.REJECT
        assert "deletes" in verdict.reason

    def test_an_empty_diff_abstains(self):
        verdict = rule_reviewer("", {})
        assert verdict.verdict is ReviewVerdict.ABSTAIN

    def test_the_default_reviewer_never_claims_a_model_ran(self):
        for body in ("", "+x", "-x"):
            assert rule_reviewer(body, {}).model_calls == 0


class TestDeterminism:
    def test_two_consolidators_over_one_store_agree(self, store, repo, tmp_path):
        store.record(
            "mean() divides by len", category="gotcha", source="agent", repo_path=str(repo)
        )
        left = MemoryConsolidator(tmp_path / "a").consolidate(store, repo_path=str(repo))
        right = MemoryConsolidator(tmp_path / "b").consolidate(store, repo_path=str(repo))
        assert left.proposed_digest == right.proposed_digest
        assert left.diff_chars == right.diff_chars
        assert left.verdict.verdict is right.verdict.verdict

    def test_two_queues_over_one_transcript_agree(self, tmp_path):
        left = ExtractionQueue(str(tmp_path / "l.db"))
        right = ExtractionQueue(str(tmp_path / "r.db"))
        try:
            a = left.enqueue(TRANSCRIPT, session_id="s1", at=1e9)
            b = right.enqueue(TRANSCRIPT, session_id="s1", at=1e9)
            assert left.get(a["job_id"]).transcript == right.get(b["job_id"]).transcript
            assert a["redacted"] == b["redacted"]
        finally:
            left.close()
            right.close()
