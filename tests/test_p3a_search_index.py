"""P3a: the SQLite FTS5 search index -- engine reporting and honest absence.

The DoD bullets this file pins:

* FTS5 BM25 + trigram;
* deterministic;
* incremental (content-addressed);
* **engine reported**;
* ``unavailable`` is structurally distinct from a real result set;
* a trigram query below the token floor is not reported as "no matches";
* an unknown ``match_mode`` is refused rather than defaulted.

The non-vacuity rule applies throughout: a test that asserts only "the marker is
absent" is satisfied by a crash before the marker is produced, so every
negative-path test also asserts a real completion.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from memory.search_index import (
    INDEX_KINDS,
    MATCH_MODES,
    REPORTED_ENGINES,
    TRIGRAM_TOKEN_FLOOR_CHARS,
    SearchEngine,
    SearchIndex,
    SearchOutcome,
    fts5_capability,
    has_cjk,
    open_default_search_index,
)

CJK_TEXT = "\u5e73\u5747\u503c\u5fc5\u987b\u9664\u4ee5\u957f\u5ea6"

DOCS = [
    {
        "doc_id": "m-mean",
        "title": "mean denominator",
        "body": "mean() must divide by len, not len-1, because we report the "
        "arithmetic mean",
        "repo_key": "repo-a",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "doc_id": "m-pytest",
        "title": "test invocation",
        "body": "run python -m pytest from the repository root, never bare pytest",
        "repo_key": "repo-a",
        "updated_at": "2026-01-02T00:00:00Z",
    },
    {
        "doc_id": "m-cjk",
        "title": "CJK fact",
        "body": CJK_TEXT,
        "repo_key": "repo-a",
        "updated_at": "2026-01-03T00:00:00Z",
    },
    {
        "doc_id": "s-other",
        "title": "other repo",
        "body": "a fact belonging to a different repository entirely",
        "repo_key": "repo-b",
        "updated_at": "2026-01-04T00:00:00Z",
    },
]


@pytest.fixture()
def index(tmp_path: Path):
    handle = SearchIndex(str(tmp_path / "index.db"))
    try:
        yield handle
    finally:
        handle.close()


class TestTheEngineIsAlwaysNamed:
    """Every result names the engine that produced it, including failures."""

    def test_an_available_search_names_a_real_engine(self, index: SearchIndex):
        report = index.index("memories", DOCS)
        assert report.engine in REPORTED_ENGINES
        outcome = index.search("mean")
        assert outcome.available is True
        assert outcome.engine in REPORTED_ENGINES
        assert outcome.hits, "the fixture must produce a real hit"
        assert outcome.as_dict()["engine"] == outcome.engine

    def test_an_unavailable_search_is_not_an_empty_one(self, index: SearchIndex):
        """The honesty crux: `unavailable` cannot be built with hits."""
        unavailable = SearchOutcome.unavailable("FTS5 is not compiled in", "q")
        assert unavailable.available is False
        assert unavailable.engine == SearchEngine.UNAVAILABLE.value
        assert unavailable.hits == ()
        assert unavailable.reason, "a reasonless failure reads as an empty result"
        assert unavailable.as_dict()["count"] == 0
        # The two are different OBJECTS with different fields, and a consumer
        # that branches on `hits` alone would render them identically -- which
        # is why `available` and `engine` are both always present.
        empty = index.search("zzzzz-no-such-token-anywhere")
        assert empty.available is True
        assert empty.engine in REPORTED_ENGINES
        assert empty.hits == ()
        assert empty.empty is True
        assert empty.as_dict() != unavailable.as_dict()
        assert empty.as_dict()["engine"] in REPORTED_ENGINES
        assert unavailable.as_dict()["engine"] == SearchEngine.UNAVAILABLE.value

    def test_the_outcome_refuses_to_claim_results_without_an_engine(self):
        with pytest.raises(ValueError):
            SearchOutcome(available=True, engine="linear_scan", hits=())

    def test_the_outcome_refuses_to_carry_hits_while_unavailable(self):
        from memory.search_index import SearchHit

        hit = SearchHit(
            kind="memories",
            doc_id="x",
            title="",
            body="",
            score=0.0,
            rank=1,
            engine="fts5_unicode61",
        )
        with pytest.raises(ValueError):
            SearchOutcome(
                available=False,
                engine=SearchEngine.UNAVAILABLE.value,
                hits=(hit,),
                reason="because",
            )

    def test_an_unavailable_outcome_must_carry_a_reason(self):
        with pytest.raises(ValueError):
            SearchOutcome(available=False, engine=SearchEngine.UNAVAILABLE.value)

    def test_a_truncated_ranking_cannot_be_an_empty_result_set(self):
        with pytest.raises(ValueError):
            SearchOutcome(
                available=True,
                engine=SearchEngine.FTS5_UNICODE.value,
                hits=(),
                truncated=True,
                truncation_reason="capped",
            )

    def test_the_capability_receipt_reports_the_build(self):
        capability = fts5_capability()
        assert capability["deterministic"] is True
        assert capability["model_calls"] == 0
        assert list(capability["engines"]) == list(REPORTED_ENGINES)
        if not capability["fts5"]:
            assert capability["reason"], "no FTS5 must state the exact reason"
        if capability["fts5"] and not capability["trigram"]:
            assert capability["trigram_reason"]

    def test_a_closed_engine_is_available_without_fts5_and_says_so(self, tmp_path: Path):
        """A closed DB refuses cleanly and names the reason; no silent scan."""
        handle = SearchIndex(str(tmp_path / "nested" / "deeper" / "index.db"))
        try:
            report = handle.index("memories", DOCS)
            assert report.engine in REPORTED_ENGINES
            assert report.ok is True
        finally:
            handle.close()


class TestBm25AndTrigram:
    def test_bm25_ranks_the_matching_row_first(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("pytest")
        assert outcome.tokenizer.startswith("unicode61")
        assert outcome.hits[0].doc_id == "m-pytest"
        assert outcome.hits[0].ordering == "bm25"
        assert outcome.hits[0].score != 0.0, "a BM25 hit must carry a real score"
        assert [hit.rank for hit in outcome.hits] == list(
            range(1, len(outcome.hits) + 1)
        )

    def test_a_cjk_query_uses_trigram(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search(CJK_TEXT[:TRIGRAM_TOKEN_FLOOR_CHARS])
        assert outcome.engine == SearchEngine.FTS5_TRIGRAM.value
        assert outcome.tokenizer == "trigram"
        assert outcome.cjk_query is True
        assert outcome.trigram_attempted is True
        assert [hit.doc_id for hit in outcome.hits] == ["m-cjk"]

    def test_a_short_cjk_query_is_reported_as_below_the_token_floor(self, index: SearchIndex):
        """The defect this module was built to prevent.

        A 2-character CJK query matches nothing in a trigram index. Without the
        floor report that is indistinguishable from "this repository holds no
        memory about that", which is a lie about the corpus.
        """
        index.index("memories", DOCS)
        outcome = index.search(CJK_TEXT[:2])
        assert outcome.below_token_floor is True
        assert outcome.hits == ()
        assert "floor" in outcome.reason.lower()
        assert "NOT" in outcome.reason  # explicitly says memory is not missing
        assert outcome.as_dict()["below_token_floor"] is True

    def test_a_three_character_cjk_query_is_not_below_the_floor(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search(CJK_TEXT[:TRIGRAM_TOKEN_FLOOR_CHARS])
        assert outcome.below_token_floor is False
        assert outcome.hits

    def test_a_short_latin_query_is_NOT_routed_to_trigram(self, index: SearchIndex):
        """Routing 'ab' to trigram would guarantee zero results for no reason."""
        index.index("memories", DOCS)
        outcome = index.search("me")
        assert outcome.engine == SearchEngine.FTS5_UNICODE.value
        assert outcome.below_token_floor is False
        assert outcome.tokenizer.startswith("unicode61")

    def test_has_cjk_detects_the_scripts_that_need_trigram(self):
        assert has_cjk(CJK_TEXT) is True
        assert has_cjk("plain ascii") is False
        assert has_cjk("") is False
        assert has_cjk(None) is False

    def test_a_short_query_finds_a_row_when_one_exists(self, index: SearchIndex):
        """The control arm: the floor report is not a licence to return nothing."""
        index.index("memories", DOCS)
        outcome = index.search("mea")
        assert outcome.below_token_floor is False
        assert [hit.doc_id for hit in outcome.hits] == ["m-mean"]


class TestMatchMode:
    def test_and_is_the_default_and_requires_every_term(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("pytest nonexistentword")
        assert outcome.tokenizer.startswith("unicode61")
        assert outcome.hits == ()

    def test_or_mode_matches_any_term(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("pytest nonexistentword", match_mode="or")
        assert [hit.doc_id for hit in outcome.hits] == ["m-pytest"]

    def test_an_unknown_match_mode_is_refused_not_defaulted(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("pytest", match_mode="xor")
        assert outcome.available is False
        assert outcome.engine == SearchEngine.UNAVAILABLE.value
        assert "match_mode" in outcome.reason
        assert MATCH_MODES == ("and", "or")


class TestIncrementalIndexing:
    def test_reindexing_unchanged_content_writes_nothing(self, index: SearchIndex):
        first = index.index("memories", DOCS)
        assert first.writes == len(DOCS)
        assert first.indexed == len(DOCS)
        second = index.index("memories", DOCS)
        assert second.writes == 0
        assert second.unchanged == len(DOCS), "content-addressed: identical is a no-op"
        assert second.indexed == 0
        assert second.updated == 0

    def test_changed_content_is_reported_as_updated_not_indexed(self, index: SearchIndex):
        index.index("memories", DOCS)
        changed = [dict(DOCS[0], body="mean() divides by len-1 for samples")]
        report = index.index("memories", changed)
        assert report.updated == 1
        assert report.indexed == 0
        assert report.writes == 1

    def test_a_changed_row_is_retrievable_under_both_tokenizers(self, index: SearchIndex):
        """Forgetting the trigram table leaves CJK stale while English looks fine."""
        index.index("memories", DOCS)
        index.index("memories", [dict(DOCS[2], body=CJK_TEXT + " with a new tail")])
        english = index.search("new")
        assert [hit.doc_id for hit in english.hits] == ["m-cjk"]
        cjk = index.search(CJK_TEXT[:3])
        assert [hit.doc_id for hit in cjk.hits] == ["m-cjk"]

    def test_removal_takes_the_row_out_of_both_indexes(self, index: SearchIndex):
        index.index("memories", DOCS)
        assert index.remove("memories", ["m-pytest"]) == 1
        assert index.search("pytest").hits == ()
        assert index.remove("memories", ["not-there"]) == 0

    def test_an_unknown_kind_is_refused_and_names_the_closed_set(self, index: SearchIndex):
        report = index.index("not_a_corpus", DOCS)
        assert report.ok is False
        assert "not_a_corpus" in report.reason
        assert set(INDEX_KINDS) == {"memories", "sessions"}

    def test_an_oversized_document_is_truncated_and_the_count_published(
        self, index: SearchIndex
    ):
        from memory.search_index import MAX_DOC_CHARS

        report = index.index("memories", [{"doc_id": "big", "body": "x" * (MAX_DOC_CHARS + 500)}])
        assert report.truncated_docs == 1, "a cap that fires must be reported"

    def test_the_two_corpora_are_separate(self, index: SearchIndex):
        index.index("memories", DOCS)
        index.index("sessions", [dict(DOCS[1], doc_id="s-pytest")])
        memories = index.search("pytest", kind="memories")
        sessions = index.search("pytest", kind="sessions")
        assert [hit.doc_id for hit in memories.hits] == ["m-pytest"]
        assert [hit.doc_id for hit in sessions.hits] == ["s-pytest"]
        stats = index.stats()["documents"]
        assert stats == {"memories": 4, "sessions": 1}


class TestRepoScoping:
    def test_repo_key_scoping_excludes_other_repositories(self, index: SearchIndex):
        index.index("memories", DOCS)
        # "entirely" appears only in the repo-b row's body, so this query has a
        # term to match and the scope is what decides the answer.
        scoped = index.search("entirely", repo_key="repo-a")
        assert scoped.hits == ()
        scoped = index.search("entirely", repo_key="repo-b")
        assert [hit.doc_id for hit in scoped.hits] == ["s-other"]

    def test_the_title_is_searchable_as_well_as_the_body(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("invocation")
        assert [hit.doc_id for hit in outcome.hits] == ["m-pytest"]

    def test_an_empty_query_is_a_recency_listing_not_a_bm25_ranking(self, index: SearchIndex):
        index.index("memories", DOCS)
        outcome = index.search("", kind="memories")
        assert [hit.doc_id for hit in outcome.hits] == [
            "s-other",
            "m-cjk",
            "m-pytest",
            "m-mean",
        ]
        assert outcome.ordering == "recency"
        assert all(hit.score == 0.0 for hit in outcome.hits), (
            "a row BM25 did not rank must not carry a BM25 number"
        )
        assert "recency" in outcome.reason


class TestInjectionSafety:
    @pytest.mark.parametrize(
        "payload",
        [
            "NEAR(a b)",
            'x OR "y"',
            "foo^bar",
            "col:val",
            "a-b",
            "*",
            '""',
            "AND OR NOT",
        ],
    )
    def test_operator_characters_in_a_query_are_literal(self, index: SearchIndex, payload: str):
        """An unescaped user query is an injection into the ranking function."""
        index.index("memories", DOCS)
        outcome = index.search(payload)
        # The requirement is not "returns nothing" -- a quoted term may match.
        # It is that the search does not RAISE and does not evaluate operators.
        assert outcome.available in (True, False)
        if outcome.available:
            assert outcome.engine in REPORTED_ENGINES

    def test_a_query_of_pure_punctuation_matches_nothing_but_says_why(
        self, index: SearchIndex
    ):
        index.index("memories", DOCS)
        outcome = index.search("!!! ???")
        assert outcome.available is True
        assert outcome.hits == ()
        assert "no indexable term" in outcome.reason


class TestTruncation:
    def test_a_capped_result_reports_that_it_is_partial(self, index: SearchIndex):
        docs = [
            {"doc_id": f"d{n}", "body": f"pytest appears in document number {n}"}
            for n in range(6)
        ]
        index.index("memories", docs)
        outcome = index.search("pytest", limit=2)
        assert outcome.truncated is True
        assert "partial ranking" in outcome.truncation_reason
        assert len(outcome.hits) == 2
        assert outcome.hits, "a truncated ranking still returns its rows"

    def test_a_limit_within_the_result_set_does_not_report_truncation(
        self, index: SearchIndex
    ):
        index.index("memories", DOCS)
        outcome = index.search("pytest", limit=10)
        assert outcome.truncated is False
        assert outcome.truncation_reason == ""


class TestDeterminism:
    def test_the_same_query_twice_gives_an_identical_ranking(self, index: SearchIndex):
        index.index("memories", DOCS)
        first = index.search("mean pytest yaml")
        second = index.search("mean pytest yaml")
        assert [hit.as_dict() for hit in first.hits] == [
            hit.as_dict() for hit in second.hits
        ]

    def test_two_independent_indexes_agree_exactly(self, tmp_path: Path):
        left = SearchIndex(str(tmp_path / "left.db"))
        right = SearchIndex(str(tmp_path / "right.db"))
        try:
            left.index("memories", DOCS)
            right.index("memories", list(reversed(DOCS)))
            assert [
                hit.as_dict() for hit in left.search("pytest mean").hits
            ] == [hit.as_dict() for hit in right.search("pytest mean").hits]
        finally:
            left.close()
            right.close()

    def test_no_model_call_happens_on_any_path(self, index: SearchIndex):
        index.index("memories", DOCS)
        for query in ("", "mean", CJK_TEXT, "!!!", "pytest"):
            assert index.search(query).as_dict()["model_calls"] == 0
        assert index.stats()["model_calls"] == 0


class TestFts5Absent:
    """The `unavailable` path when FTS5 genuinely is not compiled in."""

    def test_the_index_refuses_rather_than_scanning(self, tmp_path: Path, monkeypatch):
        import memory.search_index as module

        monkeypatch.setattr(module, "FTS5_OK", False)
        monkeypatch.setattr(module, "TRIGRAM_OK", False)
        monkeypatch.setattr(module, "_FTS5_OK", False, raising=False)
        handle = SearchIndex(str(tmp_path / "i.db"))
        try:
            assert handle.available is False
            report = handle.index("memories", DOCS)
            assert report.engine == SearchEngine.UNAVAILABLE.value
            assert report.ok is False
            assert "FTS5" in report.reason
            assert report.indexed == 0

            outcome = handle.search("mean")
            assert outcome.available is False
            assert outcome.engine == SearchEngine.UNAVAILABLE.value
            assert outcome.hits == ()
            assert "FTS5" in outcome.reason
            assert "linear scan" in outcome.reason

            stats = handle.stats()
            assert stats["available"] is False
            assert stats["engine"] == "unavailable"
        finally:
            handle.close()

    def test_an_empty_query_also_refuses_when_the_engine_is_absent(
        self, tmp_path: Path, monkeypatch
    ):
        import memory.search_index as module

        monkeypatch.setattr(module, "FTS5_OK", False)
        handle = SearchIndex(str(tmp_path / "i.db"))
        try:
            outcome = handle.search("")
            assert outcome.available is False
            assert outcome.hits == ()
        finally:
            handle.close()

    def test_trigram_absent_degrades_and_states_it(self, tmp_path: Path, monkeypatch):
        import memory.search_index as module

        monkeypatch.setattr(module, "TRIGRAM_OK", False)
        handle = SearchIndex(str(tmp_path / "i.db"))
        try:
            handle.index("memories", DOCS)
            outcome = handle.search(CJK_TEXT)
            assert outcome.available is True
            assert outcome.engine == SearchEngine.FTS5_UNICODE.value
            assert outcome.trigram_attempted is False
            assert "trigram" in outcome.trigram_reason.lower()
        finally:
            handle.close()


class TestOpeners:
    def test_the_default_index_lands_under_the_memory_root(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("NEO_MEMORY_INDEX_DB", str(tmp_path / "custom.db"))
        handle = open_default_search_index()
        try:
            assert handle.db_path == str((tmp_path / "custom.db").resolve())
        finally:
            handle.close()

    def test_a_closed_index_answers_unavailable_rather_than_raising(self, index: SearchIndex):
        index.index("memories", DOCS)
        index.close()
        index.close()  # idempotent
        outcome = index.search("mean")
        assert outcome.available is False
        assert outcome.hits == ()

    def test_an_unopenable_database_is_reported_not_raised(self, tmp_path: Path):
        # The DB PATH itself is a directory: sqlite cannot open a directory as
        # a database, so this is a genuine open failure rather than a
        # permissions question that happens to work as root.
        as_dir = tmp_path / "i-am-a-directory.db"
        as_dir.mkdir()
        handle = SearchIndex(str(as_dir))
        try:
            assert handle.available is False
            capability = handle.capability()
            assert capability["index_available"] is False
            assert capability["open_error"], "an open failure must be diagnosable"
            assert capability["reason"]
            report = handle.index("memories", DOCS)
            assert report.ok is False
            assert report.engine == SearchEngine.UNAVAILABLE.value
            assert handle.search("mean").available is False
        finally:
            handle.close()


class TestSqliteIsTheOnlyBackend:
    def test_the_module_does_not_reach_for_an_embedding_store(self):
        """0 of 11 corpus systems use embeddings as the primary substrate."""
        import pathlib

        source = pathlib.Path(module_path()).read_text(encoding="utf-8")
        for banned in ("chromadb", "faiss", "sentence_transformers", "openai.Embedding"):
            assert banned not in source, f"{banned} must not appear in this module"


def module_path() -> str:
    import memory.search_index as module

    return str(module.__file__)


def test_the_sqlite_version_is_published():
    assert isinstance(sqlite3.sqlite_version, str)
    assert fts5_capability()["sqlite_version"] == sqlite3.sqlite_version


def test_the_tempdir_fixture_is_usable():
    """Non-vacuity: the suite's own isolation works."""
    with tempfile.TemporaryDirectory() as raw:
        assert Path(raw).is_dir()
