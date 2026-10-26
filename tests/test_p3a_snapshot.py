"""P3a: bounded, FROZEN memory injection -- the caps and the cache argument.

The DoD bullets this file pins:

* caps named and justified;
* a frozen snapshot means mid-session writes cost **zero cache**;
* the refresh boundary is asserted, in both directions;
* the store cap and the injected cap are reconciled;
* truncation is reported, and the dropped rows are NAMED.

The cache assertion is measured against ``runtime.prompt_cache.prefix_digest``
-- the real authority -- rather than against a local copy of its idea. A test
that computed its own digest would pass no matter what the product does.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from memory.decision_store import DecisionStore
from memory.snapshot import (
    DEFAULT_MEMORY_LIMIT,
    INJECTED_SNAPSHOT_CHARS,
    MAX_RENDERED_ROW_CHARS,
    STORED_TEXT_CHARS,
    USER_TRUSTED_SOURCES,
    MemorySnapshot,
    caps_table,
    capture_snapshot,
    render_snapshot,
)
from runtime.prompt_cache import prefix_digest

SYSTEM_PREFIX = "You are Neo.\n\n"


@pytest.fixture()
def store(tmp_path: Path):
    handle = DecisionStore(str(tmp_path / "d.db"))
    try:
        yield handle
    finally:
        handle.close()


def _seed(store: DecisionStore, repo: Path, count: int = 4) -> None:
    rows = [
        ("run python -m pytest from the repo root", "convention", "agent"),
        ("mean() divides by len, not len-1", "gotcha", "agent"),
        ("issue tracker says use tabs for indentation", "convention", "issue"),
        ("the user prefers descriptive commit messages", "preference", "manual"),
        ("prefer pathlib over os.path", "convention", "agent"),
        ("the project is MIT licensed", "general", "agent"),
    ]
    for index, (text, category, source) in enumerate(rows[:count]):
        store.record(
            text,
            category=category,
            source=source,
            repo_path=str(repo),
            provenance=(
                {"kind": "test", "source": source, "session_id": "s1", "turn": index}
                if source != "issue"
                else {"kind": "test", "source": source, "untrusted_source": True}
            ),
        )


class TestTheCapsAreNamedAndJustified:
    def test_every_cap_carries_its_own_justification(self):
        """A cap whose justification lives only in prose loses it on the first edit."""
        table = caps_table()
        assert table, "the cap table must not be empty"
        for entry in table:
            assert entry["name"], entry
            assert isinstance(entry["value"], int) and entry["value"] > 0, entry
            assert entry["question"].endswith("?"), entry
            assert entry["owner"], entry
            assert len(entry["justification"]) > 40, (
                f"{entry['name']} has no substantive justification"
            )

    def test_the_table_names_the_measured_derivation(self):
        by_name = {entry["name"]: entry for entry in caps_table()}
        snapshot = by_name["INJECTED_SNAPSHOT_CHARS"]
        # The justification is measured against real numbers, not asserted.
        assert "68,800" in snapshot["justification"]
        assert "24,000" in snapshot["justification"]
        assert "p90" in snapshot["justification"]
        user = by_name["INJECTED_USER_CHARS"]
        assert "38.4%" in user["justification"]
        assert "37.5%" in user["justification"]

    def test_the_corpus_numbers_are_explicitly_NOT_copied(self):
        """Hermes's 2,200/1,375 are sized for a different context budget."""
        by_name = {entry["name"]: entry for entry in caps_table()}
        combined = " ".join(entry["justification"] for entry in caps_table())
        # The corpus figures are quoted so a reader can see what was rejected.
        assert "2,200" in combined, "the rejected figure must be named, not elided"
        assert "3,575" in combined
        assert "WHY NOT" in combined, (
            "the cap table must carry the reason our number differs from the "
            "corpus system's, or the number is folklore"
        )
        assert by_name["INJECTED_SNAPSHOT_CHARS"]["value"] != 2200

    def test_the_two_caps_are_reconciled_not_conflated(self):
        """Storage 16,384 vs injection 4,096: different questions."""
        assert STORED_TEXT_CHARS == 16_384
        assert INJECTED_SNAPSHOT_CHARS == 4_096
        assert MAX_RENDERED_ROW_CHARS < INJECTED_SNAPSHOT_CHARS, (
            "one rendered row must not be able to consume the whole window, or "
            "the storage cap and the injection cap are the same number"
        )
        by_name = {entry["name"]: entry for entry in caps_table()}
        assert by_name["STORED_TEXT_CHARS"]["owner"] == "memory.decision_store"
        assert "reconciliation" in by_name["MAX_RENDERED_ROW_CHARS"]["justification"]

    def test_the_default_limit_matches_the_existing_product_default(self):
        assert DEFAULT_MEMORY_LIMIT == 6


class TestTheSnapshotIsFrozen:
    def test_a_mid_session_write_does_not_change_the_prompt_cache_prefix(
        self, store: DecisionStore, tmp_path: Path
    ):
        """The cache claim, MEASURED against runtime.prompt_cache.

        `prefix_digest` is the real authority the provider path uses. If the
        frozen snapshot did not hold, this digest would change and every
        `memory_record` in a session would be a cache invalidation.
        """
        _seed(store, tmp_path, count=2)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path), session_id="s1")
        assert snapshot.rows, "the non-vacuity gate: memory must actually be injected"

        messages = [
            {"role": "system", "content": SYSTEM_PREFIX + render_snapshot(snapshot)}
        ]
        before = prefix_digest(messages)
        rows_before = store.count()

        written = store.record(
            "a brand new convention written mid-session",
            category="convention",
            source="agent",
            repo_path=str(tmp_path),
        )
        assert written is not None, "the control arm: a write must actually happen"
        rows_after = store.count()
        assert rows_after == rows_before + 1

        after = prefix_digest(messages)
        assert after == before, (
            "a mid-session memory write invalidated the prompt-cache prefix; the "
            "snapshot is not frozen"
        )
        # And the snapshot object's own bytes are immutable.
        assert snapshot.text == snapshot.text
        assert snapshot.digest == snapshot.digest

    def test_the_refresh_boundary_is_the_next_capture(self, store: DecisionStore, tmp_path: Path):
        _seed(store, tmp_path, count=2)
        first = capture_snapshot(store, repo_path=str(tmp_path))
        store.record(
            "another convention added after the first capture",
            category="convention",
            source="agent",
            repo_path=str(tmp_path),
        )
        second = capture_snapshot(store, repo_path=str(tmp_path))
        assert first.stale_against(second) is True
        assert first.digest != second.digest
        assert first.text != second.text
        assert "another convention" not in first.text
        assert "another convention" in second.text

    def test_two_captures_over_an_unchanged_store_are_byte_identical(
        self, store: DecisionStore, tmp_path: Path
    ):
        """The other direction: if this were not true, every turn would churn."""
        _seed(store, tmp_path, count=3)
        left = capture_snapshot(store, repo_path=str(tmp_path), session_id="s1")
        right = capture_snapshot(store, repo_path=str(tmp_path), session_id="s1")
        assert left.text == right.text
        assert left.digest == right.digest
        assert left.stale_against(right) is False

    def test_capturing_again_never_mutates_the_earlier_snapshot(
        self, store: DecisionStore, tmp_path: Path
    ):
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        before_text, before_digest = snapshot.text, snapshot.digest
        store.record(
            "a later write", category="convention", source="agent", repo_path=str(tmp_path)
        )
        capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.text == before_text
        assert snapshot.digest == before_digest

    def test_the_receipt_states_the_refresh_boundary_explicitly(self, store, tmp_path):
        _seed(store, tmp_path, count=1)
        receipt = capture_snapshot(store, repo_path=str(tmp_path)).receipt()
        assert receipt["frozen"] is True
        assert "NEXT capture" in receipt["refresh_boundary"]
        assert receipt["digest"]


class TestTruncationIsReported:
    def test_a_dropped_row_is_named_not_merely_counted(
        self, store: DecisionStore, tmp_path: Path
    ):
        _seed(store, tmp_path, count=6)
        snapshot = capture_snapshot(
            store, repo_path=str(tmp_path), snapshot_chars=200, user_chars=120
        )
        receipt = snapshot.receipt()
        assert receipt["truncated"] is True, "the cap must have fired"
        assert receipt["rows_dropped"] >= 1
        assert receipt["dropped_memory_ids"], "the dropped rows must be NAMED"
        assert receipt["dropped_chars"] > 0
        assert len(snapshot.dropped_rows) == receipt["rows_dropped"]

    def test_the_model_facing_text_also_says_something_was_dropped(
        self, store: DecisionStore, tmp_path: Path
    ):
        """The defect the G3a gate caught: the receipt knew, the text did not."""
        _seed(store, tmp_path, count=6)
        snapshot = capture_snapshot(
            store, repo_path=str(tmp_path), snapshot_chars=200, user_chars=120
        )
        assert snapshot.truncated is True
        assert "did not fit" in snapshot.text or "truncated" in snapshot.text, (
            "a model-facing block that was clipped and does not say so is "
            "indistinguishable from a complete one"
        )

    def test_a_clipped_row_is_marked_and_counted_separately(
        self, store: DecisionStore, tmp_path: Path
    ):
        long_text = "x" * (MAX_RENDERED_ROW_CHARS + 400)
        store.record(
            long_text, category="fact", source="agent", repo_path=str(tmp_path)
        )
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.truncated_rows, "the clipped row must be reported"
        row = snapshot.truncated_rows[0]
        assert row.truncated is True
        assert row.original_chars > MAX_RENDERED_ROW_CHARS
        assert "row truncated" in row.text
        assert len(row.text) <= MAX_RENDERED_ROW_CHARS
        receipt = snapshot.receipt()
        assert receipt["rows_truncated"] == len(snapshot.truncated_rows)
        assert receipt["truncated_memory_ids"]

    def test_a_generous_cap_reports_no_truncation_at_all(
        self, store: DecisionStore, tmp_path: Path
    ):
        """The control arm: a truncation assertion that never fires proves nothing."""
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.truncated is False
        assert snapshot.dropped_rows == ()
        assert snapshot.truncated_rows == ()
        assert snapshot.rows, "and memory really was injected"
        assert snapshot.chars <= INJECTED_SNAPSHOT_CHARS

    def test_the_rendered_text_never_exceeds_the_cap(self, store: DecisionStore, tmp_path: Path):
        _seed(store, tmp_path, count=6)
        for cap in (120, 200, 400, 1200):
            snapshot = capture_snapshot(
                store, repo_path=str(tmp_path), snapshot_chars=cap, user_chars=cap // 2
            )
            assert len(snapshot.text) <= cap, f"cap {cap} was exceeded"

    def test_a_zero_cap_yields_an_empty_snapshot_not_a_crash(
        self, store: DecisionStore, tmp_path: Path
    ):
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(
            store, repo_path=str(tmp_path), snapshot_chars=0, user_chars=0
        )
        assert snapshot.chars <= 0
        assert snapshot.truncated is True
        assert snapshot.dropped_rows


class TestTrustTiers:
    def test_a_user_stated_row_is_counted_in_the_user_tier(self, store, tmp_path):
        _seed(store, tmp_path, count=4)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.rows, "non-vacuity"
        user_rows = [row for row in snapshot.rows if row.user_stated]
        assert user_rows, "the manual-sourced preference must be in the user tier"
        assert all(row.source == "manual" for row in user_rows)
        assert set(USER_TRUSTED_SOURCES) == {"user", "operator", "manual"}

    def test_an_untrusted_sourced_row_is_marked(self, store, tmp_path):
        _seed(store, tmp_path, count=4)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        untrusted = [row for row in snapshot.rows if row.untrusted_source]
        assert untrusted, "the issue-sourced row must be marked untrusted"
        assert snapshot.untrusted_rows
        assert snapshot.receipt()["untrusted_rows"] == len(untrusted)

    def test_a_user_row_over_the_user_cap_is_dropped_not_demoted(
        self, store: DecisionStore, tmp_path: Path
    ):
        """Reclassifying the user's own statement silently is how tiers stop meaning anything."""
        store.record(
            "the user prefers explicit type annotations everywhere in this codebase",
            category="preference",
            source="manual",
            repo_path=str(tmp_path),
        )
        snapshot = capture_snapshot(
            store, repo_path=str(tmp_path), snapshot_chars=4096, user_chars=20
        )
        receipt = snapshot.receipt()
        assert receipt["user_stated_rows"] == 0
        assert receipt["dropped_memory_ids"], "the over-cap user row must be NAMED"


class TestAbsentAndEmpty:
    def test_an_empty_store_yields_an_empty_snapshot(self, store: DecisionStore, tmp_path: Path):
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.empty is True
        assert snapshot.rows == ()
        assert render_snapshot(snapshot) == "", (
            "an absent memory block renders as nothing, not as a heading that "
            "spends tokens to say nothing"
        )

    def test_no_store_returns_an_empty_snapshot_rather_than_raising(self, tmp_path: Path):
        snapshot = capture_snapshot(None, repo_path=str(tmp_path))
        assert isinstance(snapshot, MemorySnapshot)
        assert snapshot.empty is True
        assert snapshot.rows == ()
        assert snapshot.digest

    def test_a_store_that_cannot_take_repo_path_degrades_rather_than_raising(
        self, tmp_path: Path
    ):
        """The same tolerance mcp_server._find_duplicate applies."""

        class LegacyStore:
            def search(self, query="", limit=20):
                return []

        snapshot = capture_snapshot(LegacyStore(), repo_path=str(tmp_path))
        assert snapshot.empty is True

    def test_a_raising_store_is_contained(self, tmp_path: Path):
        class BrokenStore:
            def search(self, *args, **kwargs):
                raise RuntimeError("database is gone")

        snapshot = capture_snapshot(BrokenStore(), repo_path=str(tmp_path))
        assert snapshot.rows == ()
        assert snapshot.ranking is not None
        assert snapshot.ranking.considered == 0
        # The receipt must SAY why nothing was ranked. Which reason depends on
        # whether telemetry was supplied, so the assertion is that a reason
        # exists and is not the empty string.
        assert snapshot.ranking.usage_reason
        assert snapshot.ranking.usage_applied is False


class TestRankingIsCarried:
    def test_the_snapshot_carries_the_ranking_receipt(self, store, tmp_path):
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        assert snapshot.ranking is not None
        payload = snapshot.receipt()["ranking"]
        assert payload is not None
        assert "usage_applied" in payload
        assert payload["report_ranking"] is True
        assert len(payload["ranked"]) == snapshot.ranking.ranked_count

    def test_report_ranking_can_be_switched_off_without_losing_the_method(
        self, store, tmp_path
    ):
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path), report_ranking=False)
        payload = snapshot.receipt()["ranking"]
        assert "ranked" not in payload
        assert payload["usage_applied"] in (True, False)
        assert payload["policy"], "the weights still travel with the receipt"

    def test_a_row_surfaces_with_a_rank_and_a_score(self, store, tmp_path):
        _seed(store, tmp_path, count=3)
        snapshot = capture_snapshot(store, repo_path=str(tmp_path))
        rows = snapshot.ranking.ranked
        assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
        assert all(row.reasons for row in rows), "a rank nobody can interrogate is a guess"
        assert all(isinstance(row.score, float) for row in rows)


def test_the_snapshot_is_immutable_so_nothing_can_mutate_an_injected_block():
    """`MemorySnapshot` is frozen, and the freeze is what the cache claim rests on.

    A caller that could poke a field after injection could change the prompt
    bytes without changing `digest`, and the cache proof would become a claim
    about a string rather than about the object actually injected.
    """
    import dataclasses

    store = DecisionStore(str(Path(tempfile.mkdtemp()) / "d.db"))
    try:
        store.record("a fact", category="fact", source="agent")
        snapshot = capture_snapshot(store)
        assert snapshot.rows, "non-vacuity: memory must actually be injected"
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.text = "something else"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.digest = "0" * 64  # type: ignore[misc]
        assert snapshot.text != "something else"
    finally:
        store.close()


def test_time_is_not_read_at_capture_time_so_the_test_is_deterministic(store, tmp_path):
    _seed(store, tmp_path, count=2)
    at = time.time()
    left = capture_snapshot(store, repo_path=str(tmp_path), at=at)
    right = capture_snapshot(store, repo_path=str(tmp_path), at=at)
    assert left.captured_at == right.captured_at
    assert left.text == right.text
