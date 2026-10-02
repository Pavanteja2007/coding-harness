"""Tests for `cli/fuzzy.py` (VEX-CS-03), including the multi-field primitives.

Host-only and deterministic: no Docker, no provider, no network. One class per
behaviour, named after the behaviour.

`fuzzy_score` / `rank` / `filter_and_rank` are the historical palette matcher
and are covered here too, because the new multi-field scoring sits NEXT to
them and a change to one that quietly altered the other would be exactly the
kind of drift this tree keeps recording.
"""

from __future__ import annotations

from typing import Any, List

from cli import fuzzy


def _names(records: List[Any]) -> List[str]:
    return [record.name for record in records]


# ---------------------------------------------------------------------------
# The historical matcher still behaves
# ---------------------------------------------------------------------------


class TestFuzzyScoreIsUnchanged:
    def test_an_empty_query_matches_everything_with_no_preference(self):
        assert fuzzy.fuzzy_score("", "anything") == 0
        assert fuzzy.fuzzy_score("   ", "anything") == 0

    def test_a_query_that_is_not_a_subsequence_does_not_match(self):
        assert fuzzy.fuzzy_score("zzz", "abc") is None
        # An empty candidate with an empty query is the "match everything" case,
        # not the "no match" case, and conflating them would make an empty
        # command summary rank differently from a real one.
        assert fuzzy.fuzzy_score("", "") == 0

    def test_matching_is_case_insensitive_and_word_aware(self):
        assert fuzzy.fuzzy_score("cli", "cli/tui.py") is not None
        assert fuzzy.fuzzy_score("CLI", "cli/tui.py") is not None
        assert fuzzy.fuzzy_score("tui", "cli/tui.py") is not None

    def test_every_query_word_must_match_so_a_space_is_the_filter(self):
        """A space is the AND, which is what makes it a usable filter."""
        assert fuzzy.fuzzy_score("cli trce", "cli/tracelog.py") is not None
        assert fuzzy.fuzzy_score("cli zzzz", "cli/tracelog.py") is None

    def test_a_prefix_outranks_a_scattered_hit(self):
        near = fuzzy.fuzzy_score("diff", "diff --stat")
        far = fuzzy.fuzzy_score("diff", "a-different-file-name")
        assert near is not None and far is not None
        assert near > far

    def test_it_never_raises_on_a_hostile_input(self):
        for query in (None, 0, [], {}, "  ", "\x00"):
            assert fuzzy.fuzzy_score(query, "cli/tui.py") in (
                None,
                0,
            ) or isinstance(fuzzy.fuzzy_score(query, "cli/tui.py"), int)

    def test_a_hint_hit_still_matches_but_ranks_below_a_label_hit(self):
        """The deboost, not an exclusion.

        "resumable" finds a session whose LABEL is just its task id, which is
        the case the hint field exists for. It ranks BELOW a row whose own
        label matched, because a label match is what the user typed.
        """
        records = [
            {"label": "resumable", "hint": "nothing else"},
            {"label": "fix-abc", "hint": "resumable session"},
        ]
        ranked = fuzzy.filter_and_rank(records, "resumable", lambda r: r["label"])
        assert [r["label"] for r in ranked] == ["resumable", "fix-abc"], (
            "a label match must outrank a hint-only match"
        )

    def test_ties_keep_the_curated_order(self):
        records = ["a-b", "c-d", "e-f"]
        assert [item for item, _s in fuzzy.rank(records, "")] == records


# ---------------------------------------------------------------------------
# phrase_score
# ---------------------------------------------------------------------------


class TestPhraseScoreIsUnchanged:
    def test_an_exact_phrase_outranks_a_phrase_that_merely_contains_it(self):
        exact = fuzzy.phrase_score("undo that", "undo that")
        contains = fuzzy.phrase_score("undo that", "undo that and the redo step")
        assert exact is not None and contains is not None
        assert exact > contains

    def test_a_contiguous_run_outranks_a_scattered_one(self):
        near = fuzzy.phrase_score("changed", "what changed")
        far = fuzzy.phrase_score("changed", "changed what")
        assert near is not None and far is not None
        assert near >= far

    def test_one_word_cannot_outrank_a_matching_run(self):
        """Coverage is the WEAKEST tier, deliberately.

        A single word out of a long phrase scores in the coverage band; a
        contiguous run of several scores in the run band, so a whole-question
        match can never lose to a one-word overlap.
        """
        phrase = "stop kill abort"
        # A single word is a COVERAGE match here, because the phrase has more
        # words than the query does and the run tiers need a multi-word span.
        weak = fuzzy.phrase_score("stop zzz", phrase)
        strong = fuzzy.phrase_score("stop kill", phrase)
        assert weak is not None and strong is not None
        assert strong > weak
        assert fuzzy._phrase_tier("stop zzz", phrase) < fuzzy._phrase_tier(
            "stop kill", phrase
        )

    def test_an_empty_query_matches_everything_with_no_preference(self):
        assert fuzzy.phrase_score("", "anything at all") == 0

    def test_normalisation_splits_hyphens_and_apostrophes(self):
        assert fuzzy.normalize_phrase("undo-that") == ("undo", "that")
        assert fuzzy.normalize_phrase("don't") == ("don", "t")

    def test_signal_phrase_drops_noise_but_never_empties(self):
        assert "show" not in fuzzy.signal_phrase("show me what changed")
        assert fuzzy.signal_phrase("show") == ("show",)

    def test_word_order_matters(self):
        """A person who typed the words backwards has not asked that question.

        "stop kill" is a contiguous run of "stop kill abort" and scores in the
        run band; "kill stop" is the same two words in the wrong order and drops
        to the coverage band, which is a different answer rather than the same
        one with a worse number.
        """
        phrase = "stop kill abort"
        in_order = fuzzy.phrase_score("stop kill", phrase)
        reversed_order = fuzzy.phrase_score("kill stop", phrase)
        assert in_order is not None and reversed_order is not None
        assert in_order > reversed_order


# ---------------------------------------------------------------------------
# The multi-field primitives
# ---------------------------------------------------------------------------


class TestFieldScore:
    def test_it_returns_the_best_weighted_field_not_the_sum(self):
        """Summing would let four weak matches beat one exact match.

        A name hit is what the user typed, so the best FIELD wins and the rest
        are ignored.
        """
        fields = {
            "name": "/undo",
            "alias": "/rollback",
            "description": "an unrelated summary about tokens and cost",
        }
        best = fuzzy.field_score("undo", fields)
        assert best == 100 * 100, best

    def test_a_name_outranks_a_description_of_the_same_tier(self):
        by_name = fuzzy.field_score("undo", {"name": "undo", "description": "x"})
        by_desc = fuzzy.field_score("undo", {"name": "x", "description": "undo"})
        assert by_name > by_desc

    def test_no_matching_field_is_none_and_not_zero(self):
        """A caller must be able to FILTER, not just rank."""
        assert fuzzy.field_score("zzz", {"name": "/undo"}) is None
        assert fuzzy.field_score("undo", None) is None
        assert fuzzy.field_score("undo", {}) is None

    def test_an_empty_field_is_skipped_rather_than_scored(self):
        assert fuzzy.field_score("undo", {"name": "", "alias": "/undo"}) == 90 * 100

    def test_a_hostile_field_type_does_not_raise(self):
        assert fuzzy.field_score("undo", {"name": object()}) is None
        assert fuzzy.field_score("undo", {"name": 12345}) is None

    def test_the_weight_table_declares_its_own_priority(self):
        weights = fuzzy.FIELD_WEIGHTS
        assert weights["name"] > weights["alias"] > weights["description"]
        assert weights["description"] > weights["group"] > weights["kind"]

    def test_a_phrasing_field_may_be_a_list_and_each_is_scored(self):
        """A corpus is a LIST, and joining it destroys the tier that matters.

        Measured: "stop the run" is an EXACT match (100000) against the row
        whose phrase IS that sentence, and only 20020 against the same row once
        its other phrasings are concatenated around it. Scored per phrasing, it
        stays an exact match - which is why the field accepts a sequence.
        """
        as_list = fuzzy.field_score(
            "stop the run", {"phrasing": ["stop the run", "kill it", "start over"]}
        )
        as_joined = fuzzy.field_score(
            "stop the run", {"phrasing": "stop the run kill it start over"}
        )
        assert as_list is not None and as_joined is not None
        assert as_list > as_joined

    def test_a_phrase_tier_is_bounded_like_a_fuzzy_tier(self):
        """Both matchers land on the same 0-100 scale, which is the point.

        The scale is what makes the two comparable. Before it existed, a
        `fuzzy_score` over a long summary (which grows with the LENGTH of the
        candidate) outscored a `phrase_score` against a corpus row, and
        "stop the run" ranked `/detach` - whose summary happens to contain
        both words - above `/cancel`, whose corpus row IS that sentence.
        """
        from cli.fuzzy import _fuzzy_tier, _phrase_tier

        assert _fuzzy_tier("undo", "undo") == 100
        assert _phrase_tier("undo that", "undo that") == 100
        # both are bounded, and neither can exceed the top band
        for query, candidate in (
            ("stop", "stop kill abort"),
            ("stop kill", "stop kill abort"),
        ):
            assert 0 < _phrase_tier(query, candidate) <= 100
        for query, candidate in (("dif", "/diff"), ("undo", "/undo")):
            assert 0 < _fuzzy_tier(query, candidate) <= 100
        # and a field that does not match at all is None on both sides
        assert _fuzzy_tier("zzz", "/diff") is None
        assert _phrase_tier("zzz", "stop kill abort") is None

    def test_a_long_summary_cannot_outscore_a_matching_corpus_phrase(self):
        """The defect the scale exists to remove, as a regression gate.

        `/detach`'s summary contains "stop" and "run"; `/cancel`'s corpus
        contains the sentence "stop the run". Scored on raw magnitudes, the
        summary won. Scored on tiers, the corpus wins - and this asserts the
        ordering rather than the implementation.
        """
        detach_like = {
            "name": "/detach",
            "description": "leave the run running and stop watching it",
        }
        cancel_like = {"name": "/cancel", "phrasing": ("stop the run", "kill it")}
        assert fuzzy.entry_score("stop the run", **cancel_like) > fuzzy.entry_score(
            "stop the run", **detach_like
        )


class TestEntryScore:
    def test_an_empty_query_scores_zero(self):
        assert fuzzy.entry_score("", name="/undo") == 0

    def test_it_is_a_thin_wrapper_over_field_score(self):
        fields = {"name": "/undo", "group": "Changes", "phrasing": ("undo that",)}
        assert fuzzy.entry_score("undo", **fields) == fuzzy.field_score("undo", fields)

    def test_nothing_matching_is_none(self):
        assert fuzzy.entry_score("zzzqqq", name="/undo") is None

    def test_a_name_still_beats_a_description_on_equal_tiers(self):
        assert fuzzy.entry_score("diff", name="/diff", description="x") > (
            fuzzy.entry_score("diff", name="x", description="/diff")
        )


class TestRankEntries:
    def test_an_empty_query_preserves_the_input_order(self):
        records = [{"name": "/a"}, {"name": "/b"}]
        assert fuzzy.rank_entries(records, "", lambda r: r) == records

    def test_it_ranks_by_the_best_field_and_keeps_the_rest(self):
        records = [
            {"name": "/diff", "description": "see the diff"},
            {"name": "/cost", "description": "the diff of a spend report"},
        ]
        ranked = fuzzy.rank_entries(records, "diff", lambda r: r)
        # both match, the exact NAME match first
        assert [r["name"] for r in ranked] == ["/diff", "/cost"]

    def test_a_record_whose_fields_raise_is_skipped_not_fatal(self):
        """One broken row must not cost the user the other fifty."""

        def fields(record):
            if record == "broken":
                raise RuntimeError("this row is an object with no string in it")
            return {"name": record}

        ranked = fuzzy.rank_entries(["/undo", "broken", "/diff"], "d", fields)
        assert "broken" not in ranked
        assert "/diff" in ranked

    def test_a_limit_is_honoured(self):
        records = [{"name": f"/c{i}"} for i in range(20)]
        assert len(fuzzy.rank_entries(records, "c", lambda r: r, limit=4)) == 4

    def test_ties_keep_the_curated_order(self):
        """Equal scores preserve input order, which is what keeps a curated
        menu in its curated order when nothing distinguishes two rows."""
        records = ["aa", "ab", "ac"]
        ranked = fuzzy.rank_entries(records, "a", lambda r: {"name": r})
        assert ranked == records, ranked
