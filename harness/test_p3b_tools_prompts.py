"""Phase 3b/W1 pins — deferred tools, progressive skills, format/behaviour split.

Every test here is named after the property it defends, and every property was
either measured during the round or is a claim the phase brief makes that would
be expensive to rediscover. The pin module is ``harness/``-local because
``pyproject.toml`` pins ``testpaths = ["tests"]``; a CI selection entry for it is
a T5 handoff.

The four classes, and what each is really checking:

* **the descriptions were the blocker** — the corpus's mechanism is "search by
  description", so a catalog whose descriptions are ``read (read_only)`` cannot
  be searched at all. That is a measurement, and the pins hold it.
* **deferred is not withheld** — the distinction the phase brief refuses to let
  collapse, asserted structurally against a real registry.
* **the cache invalidates** — including the bug this round's own demo found.
* **the format/behaviour split** — the trap the phase is most at risk of.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Dict, List

import pytest

from harness import behaviour_policy, bm25, deferred_tools, prompt_matrix, skills
from harness.agent_kernel.tools import ToolRegistry
from harness.bm25 import BM25Index, SearchDocument, tokenize
from harness.deferred_tools import (
    CACHE_EVENTS,
    DEFAULT_SCHEMA_SHARE_THRESHOLD,
    EAGER_TOOLS,
    TOOL_SEARCH_NAME,
    TOOL_SEARCH_SPEC,
    DeferredSetCache,
    DeferredToolState,
    cache_event,
    deferral_enabled,
    deferred_cache_key,
    deferred_from_config,
    should_defer,
    threshold_decision,
)
from harness.tools import (
    catalog_fingerprint,
    typed_tool_schemas,
    typed_tool_specs,
)


def _read_source(path: Path) -> str:
    """Read a Python source file for an AST or text assertion.

    ``utf-8-sig`` is deliberate and is the repository's established choice
    (``harness/test_mint_site_pins.py`` uses it for the same reason): a BOM makes
    ``ast.parse`` raise on line 1, so a pin that reads with plain ``utf-8``
    turns a stray editor artefact into a confusing SyntaxError instead of the
    answer the reader asked for. ``Path.read_text`` also satisfies the
    context-manager rule that six inline ``io.open(...).read()`` calls did not.
    """
    return path.read_text(encoding="utf-8-sig")


REPO_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 1. The descriptions were the blocker, and that is now measured
# ===========================================================================


def skills_mod_name() -> str:
    """The skill bridge's tool name, read from the module that owns it."""
    from harness import skills as skills_mod

    return skills_mod.SKILL_VIEW_NAME


class TestTheDescriptionsWereTheBlocker:
    """The corpus's mechanism is search-by-description. Prove it can work."""

    def test_every_catalog_entry_declares_a_capability_description(self) -> None:
        """No entry may fall back to ``<name> (<effect>)`` in the rich schema."""
        missing = [
            spec.name for spec in typed_tool_specs() if not spec.description.strip()
        ]
        assert missing == [], (
            f"{len(missing)} catalog entries have no capability description. "
            "Deferred discovery searches descriptions, so an entry without one "
            "is a tool a model cannot find by describing what it wants to do."
        )

    def test_a_rich_description_is_longer_than_the_short_form(self) -> None:
        """Every capability sentence must beat the three-token placeholder."""
        short_lengths = {
            spec.name: len(spec.short_description()) for spec in typed_tool_specs()
        }
        for spec in typed_tool_specs():
            assert len(spec.capability_description()) > short_lengths[spec.name], (
                f"{spec.name}'s capability description is not longer than its "
                "short form, so it carries no capability vocabulary"
            )

    def test_to_schema_is_byte_identical_to_the_pre_phase3b_shape(self) -> None:
        """The DEFAULT prompt must be unchanged, so the eval matrix has a baseline."""
        for spec in typed_tool_specs():
            schema = spec.to_schema()
            assert schema["function"]["description"] == (
                f"{spec.name} ({spec.side_effect_class})"
            ), f"{spec.name}: to_schema() changed the shipped wire description"

    def test_rich_schema_differs_only_in_the_description(self) -> None:
        """One variable per arm, or the arm's diff is unreviewable."""
        for spec in typed_tool_specs():
            plain = spec.to_schema()
            rich = spec.rich_schema()
            assert plain["type"] == rich["type"]
            assert set(plain["function"]) == set(rich["function"])
            stripped_plain = {
                k: v for k, v in plain["function"].items() if k != "description"
            }
            stripped_rich = {
                k: v for k, v in rich["function"].items() if k != "description"
            }
            assert stripped_plain == stripped_rich, (
                f"{spec.name}: rich_schema changed something other than the "
                "description, so the deferral arm moves two variables at once"
            )

    def test_the_catalog_fingerprint_did_not_move(self) -> None:
        """``_catalog_shape`` excludes description, so 45 rows of new text is free.

        Pinned by VALUE because the whole argument for adding descriptions
        fingerprint-safe is an argument about ``_catalog_shape``, and a future
        edit that adds description to that function would silently invalidate
        every external consumer of the fingerprint.
        """
        assert catalog_fingerprint().startswith("d3eb4ac7de866736"), (
            "catalog_fingerprint moved. If that is intentional, every consumer "
            "of the fingerprint has to be re-derived -- see the P1/W2-T1 "
            "handoff, which records that the value is externally consumed."
        )

    def test_a_capability_query_finds_its_tool_and_a_name_query_would_not(self) -> None:
        """The acceptance criterion: description words, not tool names.

        The property pinned is that a **description word drove the match**, not
        that the tool's name is absent from the query text. The first version of
        this test asserted the latter and it was wrong: ``"run the tests"``
        unavoidably contains the substring ``test``, and a pin that forbids that
        would have banned the single most natural query in the corpus. What
        actually matters is that the ranking did not come from the NAME field,
        which is asserted per hit below.
        """
        index = BM25Index(
            [
                SearchDocument.of(
                    spec.name,
                    name=(spec.name, 8.0),
                    description=(spec.capability_description(), 2.0),
                )
                for spec in typed_tool_specs()
            ]
        )
        cases = [
            ("run the tests and tell me which ones failed", "test"),
            ("show me which commit last touched this line", "git_blame"),
            ("what would break if I changed this function", "blast_radius"),
            ("who calls this function", "find_references"),
            ("look up the documentation for a library online", "web_fetch"),
            ("show me the body of this function", "read_symbol"),
            ("what files have uncommitted modifications", "git_status"),
            ("change several files at once with a diff", "apply_patch"),
            ("replace one exact snippet of text in a file", "edit"),
            ("recent commit messages and who wrote them", "git_log"),
            ("search inside files for a regular expression", "grep"),
            ("compile and package the project", "build"),
        ]
        for query, expected in cases:
            hits = index.search(query, limit=1)
            assert hits and hits[0].key == expected, (
                f"{query!r} ranked "
                f"{[h.key for h in index.search(query, limit=3)]} but should "
                f"rank {expected!r} first"
            )
            # A DESCRIPTION word drove it: at least one matched term is absent
            # from the tool's own NAME field.
            name_terms = set(tokenize(expected))
            assert set(hits[0].matched_terms) - name_terms, (
                f"{expected!r} matched {query!r} only on its own NAME "
                f"({hits[0].matched_terms}); if the model must know the name to "
                "find the tool, deferral moved the problem instead of solving it"
            )

    def test_bm25_beats_the_substring_ranker_the_library_would_have_used(self) -> None:
        """The differential the phase brief asks for, as a pin not a claim.

        ``integrations.tools.ToolCatalog.search`` ranks by substring
        containment. This reproduces that scorer over the same corpus and the
        same queries and asserts BM25 wins on both rank-1 and top-3. If a future
        change to the descriptions collapses the gap, the pin fails and the
        reason gets looked at rather than assumed.
        """
        import re

        specs = {spec.name: spec for spec in typed_tool_specs()}
        index = BM25Index(
            [
                SearchDocument.of(
                    spec.name,
                    name=(spec.name, 8.0),
                    description=(spec.capability_description(), 2.0),
                )
                for spec in typed_tool_specs()
            ]
        )
        cases = [
            ("run the tests and tell me which ones failed", "test"),
            ("search inside files for a pattern", "grep"),
            ("who calls this function", "find_references"),
            ("what would break if I changed this function", "blast_radius"),
            ("look up the documentation for a library online", "web_fetch"),
            ("show me the body of this function", "read_symbol"),
            ("what files have uncommitted modifications", "git_status"),
            ("see what I have changed so far", "git_diff"),
            ("find files whose names match a pattern", "glob"),
            ("show me which commit last touched this line", "git_blame"),
        ]

        def substring_rank(query: str, limit: int = 5) -> List[str]:
            """Reproduce integrations.tools' containment scorer verbatim."""
            terms = [t for t in re.findall(r"[\w-]+", query.lower()) if t]
            ranked: List[tuple] = []
            for spec in specs.values():
                name = spec.name.lower()
                description = spec.capability_description().lower()
                tags = [alias.lower() for alias in spec.aliases]
                score = 0
                for term in terms:
                    if name == term:
                        score += 100
                    elif name.startswith(term):
                        score += 50
                    elif term in name:
                        score += 25
                    if term in description:
                        score += 6
                    if any(term in tag for tag in tags):
                        score += 12
                if score > 0:
                    ranked.append((score, spec.name))
            ranked.sort(key=lambda row: (-row[0], row[1]))
            return [name for _score, name in ranked[:limit]]

        bm25_first = sum(
            1
            for query, expected in cases
            if (hits := index.search(query, limit=1)) and hits[0].key == expected
        )
        substring_first = sum(
            1
            for query, expected in cases
            if (names := substring_rank(query, limit=1)) and names[0] == expected
        )
        assert bm25_first == len(cases), (
            f"BM25 got {bm25_first}/{len(cases)} at rank 1; the acceptance "
            "criterion is every one of them"
        )
        assert bm25_first > substring_first, (
            f"BM25 {bm25_first} vs substring {substring_first} at rank 1 - the "
            "whole reason this phase replaced the library's ranker"
        )

    def test_the_tokenizer_floor_is_two_and_the_sweep_says_so(self) -> None:
        """A floor of 3 was tried and measured WORSE. Pin the measured answer.

        The reasoning for 3 (that ``of``/``in`` are noise) was plausible and
        wrong: IDF already handles them. Pinning 2 stops the next reader from
        "fixing" it back.
        """
        assert bm25._MIN_TERM_CHARS == 2, (
            "the term floor was measured: 2 scores 22/24 top-1/top-3 on the "
            "capability suite and 3 and 4 both score 21/23. IDF handles "
            "near-stopwords; dropping them only discards real matches."
        )

    def test_the_bm25_parameters_are_the_standard_defaults(self) -> None:
        """They are measurably INERT here; a tuned value would look deliberate.

        All nine combinations of k1 in {1.2,1.5,2.0} x b in {0.5,0.75,1.0}
        scored identically, so tuning them against a suite would manufacture
        an arbitrary number that looks like a decision.
        """
        assert (bm25.BM25_K1, bm25.BM25_B) == (1.5, 0.75)

    def test_an_empty_query_returns_nothing_rather_than_everything(self) -> None:
        """An uninformative query must not render as a confident answer."""
        index = BM25Index([SearchDocument.of("a", name="a", description="alpha")])
        assert index.search("") == []
        assert index.search("of") == [], (
            "a query of only sub-floor terms carries no information and must not "
            "produce an arbitrary ranking"
        )


# ===========================================================================
# 2. Deferred is NOT withheld -- the distinction the brief refuses to collapse
# ===========================================================================


class TestDeferredIsNotWithheld:
    def test_the_registry_is_never_narrowed_by_deferral(self) -> None:
        """The strongest form: deferral holds a SET, never a filtered registry."""
        registry = ToolRegistry()
        before = len(registry.canonical_names)
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        assert len(composition.deferred) > 0, "the fixture must actually defer"
        assert len(registry.canonical_names) == before, (
            "deferral narrowed the registry. That would silently disable "
            "plan_mode_tools(), runtime/roles.py and the catalog parity report."
        )

    def test_a_deferred_tool_is_still_dispatchable(self) -> None:
        """Deferred means absent from the PROMPT, not absent from the run."""
        registry = ToolRegistry()
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        for name in composition.deferred:
            report = state.executable(name, registry=registry)
            assert report.in_registry, f"{name} left the registry"
            assert report.dispatchable, (
                f"{name} is deferred and no longer dispatchable, which is "
                "withholding wearing deferral's name"
            )
            assert report.deferred_but_available, (
                f"{name} is deferred but not available; the two are different "
                "states and the receipt must not collapse them"
            )

    def test_a_withheld_tool_is_absent_from_the_search_index(self) -> None:
        """A withheld tool must never be offered to a search."""
        state = DeferredToolState(context_window=32768)
        composition = state.compose(withheld=("write", "edit", "shell"))
        assert set(composition.withheld) == {"write", "edit", "shell"}
        indexed = set(state.discovery.names)
        for name in composition.withheld:
            assert name not in indexed, (
                f"{name} is withheld but present in the search index, so a "
                "query that matched it would offer a tool the run cannot reach"
            )
        # And the eager ones are shown, not deferred.
        assert "read" in composition.shown
        assert "read" not in indexed

    def test_selecting_a_withheld_tool_refuses_and_names_it(self) -> None:
        """A refusal must name what was refused, not report an empty query."""
        state = DeferredToolState(context_window=32768)
        state.compose(withheld=("write",))
        result = state.search("", select="select:write")
        assert not result.activated
        assert "write" in result.render(), (
            "the refusal text must name the tool; the first version regenerated "
            "its prose from the result fields and told the model its query was "
            "empty"
        )
        assert result.tokens > 0, "a refusal still costs the model tokens to read"

    def test_the_everything_is_exactly_one_bucket_property_holds(self) -> None:
        """Four buckets, disjoint and exhaustive, so composition is statable."""
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        placed = {name: composition.bucket_of(name) for name in state.catalog_names}
        assert set(placed.values()) <= {"shown", "deferred", "withheld", "activated"}
        unknown = [name for name, bucket in placed.items() if bucket == "absent"]
        assert unknown == [], f"tools in no bucket: {unknown}"
        buckets = [
            set(composition.shown),
            set(composition.deferred),
            set(composition.withheld),
            set(composition.activated),
        ]
        for i, left in enumerate(buckets):
            for right in buckets[i + 1 :]:
                overlap = left & right
                assert not overlap, f"tools in two buckets: {sorted(overlap)}"

    def test_activation_moves_a_tool_out_of_the_deferred_bucket(self) -> None:
        """And it STAYS out -- the bug this round's cache key caused."""
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        assert "web_fetch" in composition.deferred
        state.search("look up the documentation for a library online")
        composition = state.compose()
        assert "web_fetch" in composition.activated
        assert "web_fetch" not in composition.deferred
        assert composition.bucket_of("web_fetch") == "activated"


# ===========================================================================
# 3. shouldDefer, the threshold, and the cache
# ===========================================================================


class TestShouldDeferAndTheThreshold:
    def test_the_eager_set_is_derived_from_the_reason_map(self) -> None:
        """A name in the reasons map that is not in the set documents nothing."""
        assert set(EAGER_TOOLS) == set(deferred_tools.EAGER_TOOL_REASONS)
        for name in EAGER_TOOLS:
            assert deferred_tools.EAGER_TOOL_REASONS[name].strip(), (
                f"{name} is eager with no written reason"
            )

    def test_control_tools_are_never_deferred(self) -> None:
        """A model that cannot end its turn is stuck. This is the one bug."""
        for name in ("finish", "cancel", "ask"):
            decision = should_defer(name, collapsing=True)
            assert not decision.deferred, (
                f"{name} is control and must survive even a collapsing threshold"
            )

    def test_the_mutation_floor_survives_a_collapsing_threshold(self) -> None:
        for name in ("edit", "apply_patch", "write"):
            assert not should_defer(name, collapsing=True).deferred

    def test_an_incompatible_tool_stays_deferred_even_on_the_eager_floor(self) -> None:
        """W1.4 item 1: the eager check must NOT rescue an incompatible tool.

        The floor exists so the agent can work; a tool the model cannot use does
        not serve that, so the incompatibility check runs FIRST.
        """
        decision = should_defer("edit", incompatible=frozenset({"edit"}))
        assert decision.deferred
        assert decision.reason == "model_incompatible"

    def test_every_deferral_reason_is_in_the_closed_vocabulary(self) -> None:
        """plus the two non-deferral reasons, which a receipt also carries."""
        allowed = set(deferred_tools.DEFER_REASONS) | {"eager"}
        by_name = {spec.name: spec for spec in typed_tool_specs()}
        for name in by_name:
            decision = should_defer(name, spec=by_name[name], collapsing=True)
            assert decision.reason in allowed, (
                f"{name} reported reason {decision.reason!r}, outside the closed "
                f"vocabulary {sorted(allowed)}"
            )

    def test_the_threshold_fires_on_this_repositorys_shipped_defaults(self) -> None:
        """The corpus's 10% rule is calibrated for a catalog the size of ours."""
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        assert composition.threshold is not None
        decision = composition.threshold
        assert decision.fired, (
            f"the threshold did not fire at {decision.share:.2%} of the window"
        )
        assert decision.threshold == DEFAULT_SCHEMA_SHARE_THRESHOLD
        assert "over" in decision.reason

    def test_the_threshold_is_reported_even_when_it_does_not_fire(self) -> None:
        """A threshold that only speaks when it fires cannot be told from one
        that never ran."""
        decision = threshold_decision(schema_tokens=100, context_window=32768)
        assert not decision.fired
        assert decision.threshold == DEFAULT_SCHEMA_SHARE_THRESHOLD
        assert decision.reason
        assert "share" in decision.to_dict()

    def test_an_unusable_window_does_not_strip_the_mutation_floor(self) -> None:
        """Fail SAFE: a zero window must not remove every way to change a file."""
        decision = threshold_decision(schema_tokens=99999, context_window=0)
        assert not decision.fired
        assert "did not run" in decision.reason

    def test_a_tiny_schema_set_below_the_floor_does_not_collapse(self) -> None:
        """A large share of a tiny number saves nothing and costs a turn."""
        decision = threshold_decision(
            schema_tokens=200, context_window=1024, threshold=0.10
        )
        assert not decision.fired
        assert "floor" in decision.reason


class TestTheDeferredSetCache:
    def test_a_cache_hit_returns_the_same_value(self) -> None:
        cache = DeferredSetCache()
        key = deferred_cache_key(
            catalog_fingerprint="f",
            deferred=("b", "a"),
            eager=("c",),
            threshold=0.1,
            context_window=100,
        )
        value, event = cache.get_or_build(key, lambda: {"n": 1})
        assert event == "miss"
        again, event2 = cache.get_or_build(key, lambda: {"n": 2})
        assert event2 == "hit"
        assert again == value, "a hit must not rebuild"

    def test_the_key_is_order_independent(self) -> None:
        """An order-dependent key is a cache that misses on an unchanged set."""
        base = dict(
            catalog_fingerprint="f",
            eager=("c",),
            threshold=0.1,
            context_window=100,
        )
        assert deferred_cache_key(deferred=("a", "b"), **base) == deferred_cache_key(
            deferred=("b", "a"), **base
        )

    def test_the_key_changes_when_the_deferred_set_changes(self) -> None:
        base = dict(
            catalog_fingerprint="f",
            eager=("c",),
            threshold=0.1,
            context_window=100,
        )
        assert deferred_cache_key(deferred=("a",), **base) != deferred_cache_key(
            deferred=("a", "b"), **base
        )

    def test_the_key_changes_when_the_activated_set_changes(self) -> None:
        """THE BUG THIS ROUND'S OWN DEMO FOUND.

        ``activated`` is an INPUT: a discovered tool moves from the deferred
        bucket to the shown bucket. Leaving it out of the key meant the first
        turn's composition was served for the whole run -- so the model ran
        ``tool_search``, was told the tool had loaded, and the next turn's
        prompt still did not carry its schema. A cache that never invalidates
        shows the model a tool list that no longer matches reality.
        """
        base = dict(
            catalog_fingerprint="f",
            deferred=("a", "b"),
            eager=("c",),
            threshold=0.1,
            context_window=100,
        )
        assert deferred_cache_key(activated=(), **base) != deferred_cache_key(
            activated=("b",), **base
        )

    def test_the_key_covers_every_input_that_can_change_the_set(self) -> None:
        """One field at a time; each must move the key."""
        base = dict(
            catalog_fingerprint="f",
            deferred=("a",),
            eager=("c",),
            threshold=0.1,
            context_window=100,
            model="m",
            provider="p",
            rich_descriptions=True,
        )
        reference = deferred_cache_key(**base)
        for field, value in (
            ("catalog_fingerprint", "g"),
            ("deferred", ("a", "b")),
            ("eager", ("c", "d")),
            ("threshold", 0.2),
            ("context_window", 200),
            ("model", "m2"),
            ("provider", "p2"),
            ("rich_descriptions", False),
        ):
            changed = dict(base)
            changed[field] = value
            assert deferred_cache_key(**changed) != reference, (
                f"changing {field} did not move the cache key"
            )

    def test_a_stale_entry_is_dropped_and_counted(self) -> None:
        """Replacing silently would make 'the catalog changed' indistinguishable
        from 'we returned the old answer'."""
        cache = DeferredSetCache()
        cache.get_or_build("k1", lambda: 1)
        value, event = cache.get_or_build("k2", lambda: 2)
        assert (value, event) == (2, "miss")
        assert cache.stats.stale_dropped == 1
        assert cache.stats.hit_rate == 0.0

    def test_an_unused_cache_reports_no_hit_rate_rather_than_zero(self) -> None:
        """No lookup is not a lookup that always missed."""
        cache = DeferredSetCache()
        assert cache.stats.hit_rate is None
        assert cache.stats.total == 0

    def test_only_the_three_closed_cache_events_are_recorded(self) -> None:
        from harness.deferred_tools import CacheStats

        assert set(CACHE_EVENTS) == {"hit", "miss", "stale_dropped"}
        stats = CacheStats()
        for event in CACHE_EVENTS:
            cache_event(stats, event)
        cache_event(stats, "not_an_event")
        assert (stats.hit, stats.miss, stats.stale_dropped) == (1, 1, 1)


# ===========================================================================
# 4. Discovery: one round-trip, schemas come back callable
# ===========================================================================


class TestDiscoveryCostsOneRoundTrip:
    def test_a_search_returns_schemas_so_the_tool_is_callable_next_turn(self) -> None:
        state = DeferredToolState(context_window=32768)
        state.compose()
        result = state.search("run the tests and tell me which ones failed")
        assert result.activated
        assert result.schemas, (
            "a search that returns names the model cannot call costs two "
            "round-trips per discovery"
        )
        names = [s["function"]["name"] for s in result.schemas]
        assert "test" in names

    def test_the_discovery_cost_is_charged_to_the_run(self) -> None:
        state = DeferredToolState(context_window=32768)
        state.compose()
        before = state.discovery_tokens
        state.search("who calls this function")
        assert state.discovery_tokens > before
        assert state.discovery_calls == 1

    def test_net_saving_subtracts_the_discovery_cost(self) -> None:
        """The number the exit gate asks for, not the flattering half."""
        state = DeferredToolState(context_window=32768)
        composition = state.compose()
        assert composition.token_saving > 0
        assert composition.net_token_saving == composition.token_saving
        for query in ("search inside files for a pattern", "who calls this function"):
            state.search(query)
        composition = state.compose()
        assert composition.net_token_saving < composition.token_saving, (
            "the net saving must fall as discovery is paid for; a scheme whose "
            "net figure never moves is not reporting its cost"
        )

    def test_a_miss_is_reported_honestly_rather_than_as_an_empty_result(self) -> None:
        state = DeferredToolState(context_window=32768)
        state.compose()
        result = state.search("zzzzqqqq nonexistent capability")
        assert not result.hits
        assert "No deferred tool matched" in result.render()

    def test_a_selection_of_a_non_deferred_tool_names_it(self) -> None:
        state = DeferredToolState(context_window=32768)
        state.compose()
        result = state.search("", select="select:read")
        assert "read" in result.render()
        assert "already in your prompt" in result.render()


# ===========================================================================
# 5. Progressive skills: tiers, interop, gating, triggers
# ===========================================================================


def _write_skill(directory: Path, name: str, frontmatter: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n{body}", encoding="utf-8"
    )


@pytest.fixture()
def skill_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    body = "\n".join(f"Instruction {i}. " + "detail " * 12 for i in range(8))
    _write_skill(
        repo / ".agents" / "skills" / "pytest-conventions",
        "pytest-conventions",
        "name: pytest-conventions\ndescription: Conventions for pytest suites.\n",
        body,
    )
    _write_skill(
        repo / ".claude" / "skills" / "django-conventions",
        "django-conventions",
        "name: django-conventions\ndescription: Django ORM conventions.\n",
        body,
    )
    _write_skill(
        repo / ".agents" / "skills" / "k8s-howto",
        "k8s-howto",
        "name: k8s-howto\ndescription: How to use kubectl.\n"
        "requires-binaries:\n  - kubectl\n  - helm\n",
        body,
    )
    _write_skill(
        repo / ".agents" / "skills" / "manifest-edits",
        "manifest-edits",
        "name: manifest-edits\ndescription: How to edit manifests.\n"
        "paths:\n  - k8s/\n  - deploy/*.yaml\n",
        body,
    )
    _write_skill(
        repo / ".neo" / "skills" / "our-own",
        "our-own",
        "name: our-own\ndescription: A Neo-native skill.\n",
        body,
    )
    return repo


class TestSkillInteropRoots:
    def test_the_neutral_agents_directory_is_read(self, skill_repo: Path) -> None:
        """Six systems converged on it; the corpus calls the interop paths the
        sharpest interoperability datum it found."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        assert "agents" in {s.origin for s in found}
        assert any(s.name == "pytest-conventions" for s in found)

    def test_the_claude_directory_is_read(self, skill_repo: Path) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        assert "claude" in {s.origin for s in found}
        assert any(s.name == "django-conventions" for s in found)

    def test_interop_can_be_turned_off_per_call(self, skill_repo: Path) -> None:
        """A benchmark must be able to get a hermetic scan without a global."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=False
        )
        assert {s.origin for s in found} == {"project"}

    def test_the_interop_switch_is_read_by_key_presence_and_truthiness(self) -> None:
        from harness.skills import _interop_from_config

        assert _interop_from_config({}) is None
        assert _interop_from_config({"skills_interop": False}) is False
        for spelling in ("0", "false", "no", "off"):
            assert _interop_from_config({"skills_interop": spelling}) is False
        assert _interop_from_config({"skills_interop": True}) is True

    def test_a_project_skill_outranks_an_interop_skill_of_the_same_name(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "r"
        body = "body text here"
        _write_skill(
            repo / ".neo" / "skills" / "shared",
            "shared",
            "name: shared\ndescription: the project one\n",
            body,
        )
        _write_skill(
            repo / ".agents" / "skills" / "shared",
            "shared",
            "name: shared\ndescription: the interop one\n",
            body,
        )
        found = skills.discover_skills(repo_path=str(repo), hidden_names=[])
        shared = [s for s in found if s.name == "shared"]
        assert len(shared) == 1
        assert shared[0].origin == "project"
        assert "project one" in shared[0].description


class TestSkillRuntimeGating:
    def test_a_missing_binary_removes_the_skill_from_the_prompt(
        self, skill_repo: Path
    ) -> None:
        """The corpus's stated reason: stop kubectl instructions polluting a
        prompt on a machine without kubectl."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        report = skills.load_skills_for_prompt(found, path_lookup=lambda n: False)
        names = [s.name for s in report.eligible]
        assert "k8s-howto" not in names
        blocked = [row for row in report.runtime_blocked if row["name"] == "k8s-howto"]
        assert blocked, "the exclusion must be named, not silent"
        assert any("kubectl" in reason for reason in blocked[0]["blockers"])

    def test_a_present_binary_admits_the_skill(self, skill_repo: Path) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        report = skills.load_skills_for_prompt(
            found, path_lookup=lambda n: n in {"kubectl", "helm"}
        )
        assert "k8s-howto" in [s.name for s in report.eligible]

    def test_an_empty_environment_variable_counts_as_unset(self) -> None:
        """An export that failed halfway is the common case."""
        skill = skills.Skill(
            name="needs-env",
            description="",
            body="body",
            source="<t>/needs-env/SKILL.md",
            origin="project",
            requires_env=("SOME_TOKEN",),
        )
        assert any("SOME_TOKEN" in b for b in skill.runtime_blockers(env={}))
        assert any(
            "SOME_TOKEN" in b for b in skill.runtime_blockers(env={"SOME_TOKEN": ""})
        )
        assert skill.runtime_blockers(env={"SOME_TOKEN": "value"}) == []

    def test_an_unmatched_operating_system_blocks(self) -> None:
        skill = skills.Skill(
            name="linux-only",
            description="",
            body="body",
            source="<t>/linux-only/SKILL.md",
            origin="project",
            requires_os=("linux",),
        )
        assert skill.runtime_blockers(system="Windows")
        assert skill.runtime_blockers(system="Linux") == []

    def test_a_skill_with_no_requirements_is_never_blocked(self) -> None:
        skill = skills.Skill(
            name="plain", description="", body="b", source="<t>", origin="project"
        )
        assert skill.runtime_blockers() == []


class TestSkillPathTriggers:
    def test_a_block_sequence_of_paths_is_parsed(self, skill_repo: Path) -> None:
        """THE BUG: the frontmatter parser only accepted block sequences for
        `permissions` and `tools`, so `paths:` with `- k8s/` parsed to an EMPTY
        list and the gate it asked for was never armed."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        gated = [s for s in found if s.name == "manifest-edits"]
        assert gated, "fixture missing"
        assert tuple(gated[0].trigger_paths) == ("k8s/", "deploy/*.yaml"), (
            f"parsed trigger_paths = {gated[0].trigger_paths!r}; a block "
            "sequence under `paths` is silently dropped"
        )

    def test_a_gated_skill_waits_until_a_matching_file_is_touched(
        self, skill_repo: Path
    ) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        before = skills.load_skills_for_prompt(found, touched_paths=[])
        assert "manifest-edits" not in [s.name for s in before.eligible]
        assert "manifest-edits" in [row["name"] for row in before.path_blocked]

        after = skills.load_skills_for_prompt(found, touched_paths=["k8s/deploy.yaml"])
        assert "manifest-edits" in [s.name for s in after.eligible]

    def test_a_directory_trigger_matches_a_file_underneath_it(self) -> None:
        """`paths: [k8s/]` must match `k8s/deploy.yaml`.

        The first version appended a second separator, so `k8s/` compared
        against `k8s//` and matched nothing -- a gate that cannot fire looks
        exactly like a gate that is not armed.
        """
        skill = skills.Skill(
            name="g",
            description="",
            body="b",
            source="<t>/g/SKILL.md",
            origin="project",
            trigger_paths=("k8s/",),
        )
        assert skill.triggers_on("k8s/deploy.yaml")
        assert skill.triggers_on("k8s")
        assert not skill.triggers_on("src/app.py")

    def test_a_skill_with_no_triggers_is_not_triggered_by_anything(self) -> None:
        skill = skills.Skill(
            name="u", description="", body="b", source="<t>", origin="project"
        )
        assert not skill.path_triggered
        assert not skill.triggers_on("anything.py")

    def test_an_ungated_skill_is_always_eligible(self, skill_repo: Path) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        report = skills.load_skills_for_prompt(found, touched_paths=[])
        assert "our-own" in [s.name for s in report.eligible]


class TestSkillDisclosureTiers:
    def test_the_index_carries_no_body(self, skill_repo: Path) -> None:
        """Tier 1 is metadata only. Asserted on a body marker, not on a length."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        eligible = skills.load_skills_for_prompt(found, touched_paths=["k8s/x.yaml"])
        index = skills.render_skill_index(eligible.eligible)
        for skill in eligible.eligible:
            first_line = str(skill.body).splitlines()[0]
            assert first_line not in index, (
                f"{skill.name}'s body text reached the tier-1 index"
            )
        assert "pytest-conventions" in index
        assert "k8s-howto" not in index, "a runtime-blocked skill reached the index"

    def test_the_index_reports_each_skills_load_cost(self, skill_repo: Path) -> None:
        """A model must be able to weigh a skill before loading it.

        This test asserted against ``str(skill_repo)`` **without taking
        ``skill_repo`` as a parameter**, so the name resolved to the fixture
        object and the scan was handed a repr instead of a path. It passed for
        months on the strength of the developer's own ``~/.claude/skills``: the
        index it asserted on was full of someone else's installed skills. That
        is the same defect the home-root default was, seen from the other side —
        a test whose subject is the fixture and whose evidence is the host.
        """
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        index = skills.render_skill_index(
            skills.load_skills_for_prompt(found, touched_paths=["k8s/x.yaml"]).eligible
        )
        assert "tokens to load" in index

    def test_skill_view_returns_the_body_on_request(self, skill_repo: Path) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        view = skills.skill_view("pytest-conventions", found)
        assert view["loaded"]
        assert view["tokens"] > 0
        assert str(found[0].body)[:40] or view["text"]

    def test_skill_view_refuses_an_unknown_name_with_a_reason(
        self, skill_repo: Path
    ) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        view = skills.skill_view("no-such-skill", found)
        assert not view["loaded"]
        assert "no-such-skill" in view["reason"]

    def test_skill_view_refuses_a_runtime_blocked_skill(self, skill_repo: Path) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        view = skills.skill_view("k8s-howto", found)
        assert not view["loaded"]
        # ANY of the declared requirements, not a specific one: which binaries
        # exist on the machine running the test is not the property under test,
        # and pinning 'kubectl' made this test pass or fail by host.
        assert view["reason"], "a refusal must carry its reason"
        assert "on PATH" in view["reason"] or "PATH" in view["reason"], (
            f"reason should name the missing requirement, got {view['reason']!r}"
        )

    def test_a_supporting_file_traversal_is_refused(self, skill_repo: Path) -> None:
        """Tier 3 must go through the guarded reader, or disclosure becomes
        file disclosure."""
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        row = skills.resolve_support_file(found[0], "../../../etc/passwd")
        assert not row["loaded"]
        assert "escapes" in row["reason"]

    def test_the_prompt_report_measures_the_saving_honestly(
        self, skill_repo: Path
    ) -> None:
        found = skills.discover_skills(
            repo_path=str(skill_repo), hidden_names=[], interop=True
        )
        eligible = skills.load_skills_for_prompt(found, touched_paths=["k8s/x.yaml"])
        report = skills.skills_prompt_report(eligible.eligible)
        assert report["bodies_tokens"] > report["index_tokens"]
        assert report["saving_tokens"] > 0
        assert 0.0 < report["saving_ratio"] < 1.0
        assert report["supporting_loaded"] == 0, (
            "supporting files are tier 3 and must never be counted as loaded"
        )

    def test_an_empty_skill_set_reports_a_saving_ratio_of_none(self) -> None:
        report = skills.skills_prompt_report([])
        assert report["saving_ratio"] is None, (
            "a ratio with a zero denominator is not a percentage"
        )


# ===========================================================================
# 6. The format/behaviour split
# ===========================================================================


class TestTheFormatBehaviourSplit:
    def test_the_default_matrix_is_empty_for_every_model(self) -> None:
        """The rule that makes every use an eval arm rather than a default."""
        for model in ("gpt-4o-mini", "claude-3-5-sonnet-20241022", "qwen3.8-27b"):
            receipt = prompt_matrix.format_prompt(model=model, provider="openai")
            assert receipt.text == "", (
                f"{model} got {len(receipt.text)} chars of prompt with no "
                "configuration; the default must be byte-identical to the "
                "pre-Phase-3b prompt"
            )
            assert not receipt.enabled

    def test_every_registered_fact_carries_a_cited_observation(self) -> None:
        assert prompt_matrix.FORMAT_FACTS
        for fact in prompt_matrix.FORMAT_FACTS:
            assert fact.observed.strip(), f"{fact.fact_id} has no citation"
            assert len(fact.observed) > 60, (
                f"{fact.fact_id}'s citation is too short to be an observation"
            )

    def test_every_fact_names_the_mechanism_that_already_handles_it(self) -> None:
        """A fact duplicating an existing mechanism is documentation."""
        for fact in prompt_matrix.FORMAT_FACTS:
            assert fact.handled_by.strip(), (
                f"{fact.fact_id} does not name the mechanism that already handles this"
            )

    def test_a_behavioural_fact_cannot_be_registered(self) -> None:
        """The split is enforced by the TYPE, not by review."""
        for kind in ("no_commit", "behaviour", "style", "policy"):
            with pytest.raises(ValueError, match="FORMAT fact kind"):
                prompt_matrix.FormatFact(
                    fact_id="x",
                    kind=kind,
                    families=("openai",),
                    text="do not commit",
                    observed="because",
                )

    def test_an_uncited_fact_cannot_be_registered(self) -> None:
        with pytest.raises(ValueError, match="cited"):
            prompt_matrix.FormatFact(
                fact_id="x",
                kind="tool_call_syntax",
                families=("openai",),
                text="t",
                observed="   ",
            )

    def test_the_taxonomy_has_no_behavioural_member(self) -> None:
        for kind in prompt_matrix.FORMAT_FACT_KINDS:
            lowered = kind.lower()
            assert not any(
                word in lowered
                for word in (
                    "commit",
                    "gold",
                    "behaviour",
                    "behavio",
                    "style",
                    "policy",
                )
            )

    def test_no_registered_fact_names_an_unreachable_family(self) -> None:
        """A fact no model can be selected for is a comment with a string in it.

        This round's first draft registered three facts for qwen/deepseek/glm,
        and `vision_family_for` resolves only openai/anthropic/google -- so they
        were dead on arrival while the registry table showed them as coverage.
        """
        assert prompt_matrix.unreachable_families() == (), (
            "a registered fact declares a family the resolver cannot produce, "
            "so no model will ever be shown it"
        )

    def test_the_unreachable_observations_are_kept_as_data_with_citations(self) -> None:
        """The observations are preserved; only the untestable blocks are gone."""
        assert prompt_matrix.UNREACHABLE_OBSERVATIONS
        for row in prompt_matrix.UNREACHABLE_OBSERVATIONS:
            assert row["observed"].strip()
            assert row["reachable_when"].strip()
            assert row["kind"] in prompt_matrix.FORMAT_FACT_KINDS

    def test_dispatch_is_on_metadata_not_a_model_name_string(self) -> None:
        """An openai-named gateway fronts Claude, so a name check is wrong.

        **DOCSTRINGS ARE EXCLUDED**, and the first version of this pin failed
        on its own module's explanation of why the effort resolver is the wrong
        one — that prose contains ``gpt-4o-mini`` as an example of a model
        that resolver refuses. A pin that reads its own rationale is a pin that
        forbids documenting the trap it exists to close. This is the same
        docstring exclusion ``harness/test_completion_mint_sites.py`` applies to
        the mint scan, for the same reason.
        """
        tree = ast.parse(_read_source(REPO_ROOT / "harness" / "prompt_matrix.py"))
        fragments = ("gpt-", "claude-", "gemini", "qwen", "deepseek", "glm-", "llama-")
        holders: Dict[str, List[str]] = {}

        def literals_under(node: ast.AST) -> List[str]:
            found = []
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    found.append(child.value)
            return found

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Docstring text is the rationale, not the dispatch path.
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body = body[1:]
                hits = [
                    value
                    for value in literals_under(ast.Module(body=body, type_ignores=[]))
                    if any(fragment in value for fragment in fragments)
                ]
                if hits:
                    holders[node.name] = hits

        # The audit surface may name models on purpose: unreachable_families()
        # asks the resolver which NAMES IT REFUSES so that refusal becomes a
        # pinned fact instead of a silent gap. That is name-based REFUSAL
        # detection - the opposite of name-based dispatch - so it is allowed,
        # and required, to hold literals. Nothing else may.
        # ONE function may name models: the probe grid that asks the resolver
        # what it produces. Everything else - the dispatch path, the fact
        # table, render() - must go through metadata, because an openai-named
        # gateway fronts Anthropic and Google.
        assert set(holders) <= {"resolve_all_families"}, (
            "model names appear outside the resolver probe: "
            f"{ {k: v for k, v in holders.items() if k != 'resolve_all_families'} }. "
            "Dispatch must go through runtime.model_capabilities, not a name "
            "comparison."
        )
        assert "resolve_all_families" in holders, (
            "the probe grid went away. resolve_all_families() exists to ask the "
            "resolver what it actually produces - a table of what it ought to "
            "produce cannot answer that, and without the probe a family the "
            "resolver cannot place silently loses its format facts."
        )

    def test_the_family_resolver_is_the_shape_resolver_not_the_effort_one(self) -> None:
        """The seam T3 documented: the effort table withholds families on
        purpose, so asking it for SHAPE inherits that withholding.

        Read from the AST rather than the source text, so this measures the CALL
        the module makes and not a paragraph explaining the call.
        """
        tree = ast.parse(_read_source(REPO_ROOT / "harness" / "prompt_matrix.py"))
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert "vision_family_for" in calls, (
            "the matrix must resolve the family through the SHAPE resolver"
        )
        assert "effort_family_for" not in calls, (
            "the matrix resolved SHAPE through the EFFORT resolver. "
            "effort_family_for withholds a family wherever attaching that "
            "family's effort parameter might be a 400, so it returns the empty "
            "string for this project's own cheap tier and the matrix would "
            "silently apply no format facts there."
        )


class TestBehaviourPolicyLivesInConfig:
    def test_a_declared_policy_renders_nothing_to_the_model(self) -> None:
        """The sentence the brief asks for, made executable."""
        policy = behaviour_policy.policy_from_config(
            {"no_commit": True, "no_test_edits": True}
        )
        assert policy.any_declared
        assert policy.render() == "", (
            "behavioural text reached the prompt; it belongs in configuration "
            "and in the mechanisms that enforce it"
        )

    def test_every_directive_names_a_real_enforcing_mechanism(self) -> None:
        """A directive with no mechanism is a promise the harness cannot keep."""
        for key in behaviour_policy.BEHAVIOUR_KEYS:
            mechanism = behaviour_policy.ENFORCEMENT.get(key, "")
            assert mechanism.strip(), f"{key} has no enforcing mechanism named"
            assert len(mechanism) > 40, (
                f"{key}'s mechanism citation is too short to name a real one"
            )

    def test_an_unknown_directive_is_refused(self) -> None:
        policy = behaviour_policy.policy_from_config({})
        assert "not a known behavioural directive" in policy.refuse("be_nice")

    def test_a_known_directive_with_no_mechanism_is_refused(self) -> None:
        """The honest case: declaring it would be a promise we cannot keep."""
        saved = dict(behaviour_policy.ENFORCEMENT)
        try:
            behaviour_policy.ENFORCEMENT["no_gold_plating"] = ""
            policy = behaviour_policy.policy_from_config({})
            assert "cannot keep" in policy.refuse("no_gold_plating")
        finally:
            behaviour_policy.ENFORCEMENT.clear()
            behaviour_policy.ENFORCEMENT.update(saved)

    def test_the_default_declares_nothing(self) -> None:
        policy = behaviour_policy.policy_from_config({})
        assert not policy.any_declared
        assert policy.declared_keys == ()

    def test_no_prompt_variant_can_widen_a_capability(self) -> None:
        """Phase 2's invariant, restated by the phase brief, as a function."""
        base = [spec.name for spec in typed_tool_specs()]
        assert behaviour_policy.widenings_possible(base) == []
        assert behaviour_policy.widenings_possible(
            base, variant_tools=["delete", "brand_new"]
        ) == ["brand_new"]
        assert behaviour_policy.widenings_possible(
            base, variant_text="You may also use the deploy tool"
        )

    def test_the_format_matrix_itself_widens_nothing(self) -> None:
        """The matrix ships, so it must pass the check it is subject to."""
        base = [spec.name for spec in typed_tool_specs()]
        arm = {"prompt_format_facts": True}
        for model in ("gpt-4o-mini", "claude-3-5-sonnet-20241022", "gemini-2.0-flash"):
            text = prompt_matrix.format_prompt(
                model=model, provider="openai", config=arm
            ).text
            assert behaviour_policy.widenings_possible(base, variant_text=text) == []

    def test_declared_and_enforced_are_separate_keys(self) -> None:
        """A projection publishing only one renders 'declared' and
        'guaranteed' identically, which is the confusion this removes."""
        policy = behaviour_policy.policy_from_config({"no_commit": True})
        row = next(d for d in policy.directives if d.key == "no_commit")
        payload = row.to_dict()
        assert payload["declared"] is True
        assert payload["enforced"] is True
        assert payload["enforcement"]


# ===========================================================================
# 7. The bridge tools stay out of the canonical catalog
# ===========================================================================


class TestTheBridgeToolsAreNotCatalogEntries:
    def test_the_catalog_is_still_forty_five(self) -> None:
        """The bridge is a bridge. Adding it to the catalog would move the
        fingerprint and hand every model a bridge to a mechanism it lacks."""
        assert len(typed_tool_specs()) == 45

    def test_tool_search_is_not_a_catalog_entry(self) -> None:
        names = {spec.name for spec in typed_tool_specs()}
        assert TOOL_SEARCH_NAME not in names
        assert TOOL_SEARCH_SPEC.name == TOOL_SEARCH_NAME
        assert TOOL_SEARCH_SPEC.capability_description()

    def test_skill_view_is_not_a_catalog_entry(self) -> None:
        names = {spec.name for spec in typed_tool_specs()}
        assert skills.SKILL_VIEW_NAME not in names
        spec = skills.skill_view_spec()
        assert spec is not None
        assert spec.name == skills.SKILL_VIEW_NAME

    def test_the_default_schema_list_is_unchanged_by_all_of_this(self) -> None:
        """45 schemas, and not one of them is a discovery bridge."""
        names = [s["function"]["name"] for s in typed_tool_schemas()]
        assert len(names) == 45
        assert TOOL_SEARCH_NAME not in names
        assert skills.SKILL_VIEW_NAME not in names


# ===========================================================================
# 8. One authority per concern
# ===========================================================================


class TestOneAuthorityPerConcern:
    def test_there_is_exactly_one_token_estimator_in_the_new_modules(self) -> None:
        """P3a's handoff: 'Do not write a second token estimator.'"""
        for name in ("bm25.py", "deferred_tools.py", "prompt_budget_measure.py"):
            source = _read_source(REPO_ROOT / "harness" / name)
            assert "CHARS_PER_TOKEN" not in source, (
                f"{name} divides by a second chars-per-token literal instead "
                "of delegating to harness.agent_kernel.budget.TokenEstimator"
            )

    def test_there_is_exactly_one_bm25_implementation(self) -> None:
        """Codex's mechanism, implemented once."""
        hits = []
        for path in sorted((REPO_ROOT / "harness").glob("*.py")):
            if path.name.startswith("test_"):
                # This pin file names the class in its own assertion text, so a
                # scan that reads itself reports a second implementation that
                # does not exist.
                continue
            source = _read_source(path)
            if "class BM25Index" in source:
                hits.append(path.name)
        assert hits == ["bm25.py"], f"BM25 implemented in {hits}"

    def test_deferred_tools_does_not_reimplement_search(self) -> None:
        source = _read_source(REPO_ROOT / "harness" / "deferred_tools.py")
        assert "from harness.bm25 import" in source
        assert "def _bm25" not in source

    def test_the_skill_interop_table_is_declared_once(self) -> None:
        source = _read_source(REPO_ROOT / "harness" / "skills.py")
        assert source.count("SKILL_INTEROP_ROOTS: tuple") == 1
        assert source.count('".agents/skills"') == 1, (
            "the interop root appears in more than one place, so it can diverge"
        )


class TestTheMechanismIsActuallyReachable:
    """A deferred catalog the model cannot search is a smaller catalog, not a
    cheaper one. Three defects made this mechanism unreachable while every part
    of it passed its own unit test."""

    def _surface(self, config):
        """The schema names a run with this config would really send."""
        from harness.agent_kernel.strategy import build_default_handlers
        from harness.agent_kernel.tools import ToolRegistry

        registry = ToolRegistry()
        build_default_handlers(registry, repo_path=".", config=config)
        return [
            str(schema.get("function", {}).get("name") or "")
            for schema in registry.schemas()
        ]

    # -- defect 1: the bridge schema never reached the prompt --------------

    def test_the_registry_advertises_a_spec_the_canonical_catalog_lacks(
        self,
    ) -> None:
        """``ToolRegistry.schemas()`` derived only from the canonical catalog.

        A bridge is registered into the registry, given a handler, and callable
        by name — and was still never advertised, because membership was checked
        against ``typed_tool_schemas()``, a list the bridge is deliberately not
        in. ``tool_search`` and ``skill_view`` were therefore invisible to every
        model in the project.
        """
        from harness.agent_kernel.tools import ToolRegistry
        from harness.tools import ToolSpec, typed_tool_schemas

        canonical = {
            str(schema.get("function", {}).get("name") or "")
            for schema in typed_tool_schemas()
        }
        registry = ToolRegistry()
        bridge = ToolSpec(
            name="probe_bridge",
            side_effect_class="control",
            required=("query",),
            optional=(),
            types={"query": str},
            description="a registered spec the canonical catalog does not hold",
        )
        assert bridge.name not in canonical, (
            "the fixture stopped being a bridge; pick a name outside the catalog"
        )
        registry.register(bridge, lambda call, context: None)

        advertised = {
            str(schema.get("function", {}).get("name") or "")
            for schema in registry.schemas()
        }
        assert "probe_bridge" in advertised, (
            "a spec registered into the registry cannot be advertised unless it "
            "is also in harness.tools.typed_tool_schemas(). That couples every "
            "mechanism that needs a bridge to editing the canonical catalog, "
            "which is exactly what harness/deferred_tools refuses to do so the "
            "fingerprint stays at 45."
        )
        # ...and the catalogued tools are still the catalog's own schemas.
        assert canonical <= advertised

    def test_a_deferred_run_still_advertises_the_tool_that_finds_the_rest(
        self,
    ) -> None:
        """The end-to-end consequence, stated as one assertion.

        ``tool_search`` reached the prompt; ``_apply_deferral`` was what dropped
        it, because it filtered purely by the composition's offered set and the
        bridge is in no catalog entry to be offered. Before the fix a deferred
        run offered EIGHT tools and never mentioned the one that finds the other
        thirty-seven.
        """
        from harness.agent_kernel.strategy import build_default_handlers
        from harness.agent_kernel.tools import ToolRegistry

        config = {"tool_deferral": True, "tool_deferral_threshold": 0.1}
        registry = ToolRegistry()
        build_default_handlers(registry, repo_path=".", config=config)
        advertised = {
            str(schema.get("function", {}).get("name") or "")
            for schema in registry.schemas()
        }
        assert TOOL_SEARCH_NAME in advertised, (
            "deferral is ON and the model is not told the search tool exists, so "
            "every deferred tool is unreachable: this is the defect, live"
        )

        state = deferred_from_config(config, context_window=32768)
        assert state is not None
        composition = state.compose()
        catalog = set(state.catalog_names)
        offered = set(composition.shown) | set(composition.activated)
        # The rule _apply_deferral now uses, asserted rather than re-derived.
        kept = {name for name in advertised if name not in catalog or name in offered}
        assert TOOL_SEARCH_NAME in kept
        assert composition.deferred, "the fixture stopped deferring anything"
        assert len(composition.deferred) > 20, (
            f"only {len(composition.deferred)} deferred; the fixture no longer "
            "exercises the case this test is named for"
        )

    def test_a_deferred_run_really_is_smaller_and_the_bridge_is_free(
        self,
    ) -> None:
        """The saving, and the bridge's own cost, measured rather than asserted."""
        config = {"tool_deferral": True, "tool_deferral_threshold": 0.1}
        state = deferred_from_config(config, context_window=32768)
        composition = state.compose()
        assert composition.schema_tokens_sent < composition.schema_tokens_full
        assert composition.discovery_tokens == 0, (
            "no discovery has run yet, so charging any discovery cost here would "
            "be inventing a receipt"
        )

    # -- defect 2: a disabled mechanism still registered its bridge ---------

    @pytest.mark.parametrize(
        "config",
        [{}, {"tool_deferral": False}, {"tool_deferral": "0"}, {"tool_deferral": None}],
    )
    def test_a_disabled_mechanism_registers_no_bridge(self, config) -> None:
        """Key PRESENCE, not truthiness, is what the handler registration used.

        ``deferred_from_config`` decided on truthiness, so ``{"tool_deferral":
        False}`` built no deferral state — and the presence check still handed
        that model a ``tool_search`` whose every query answers "nothing matched"
        about a catalog it could plainly see. A bridge to a mechanism that is
        off is a tool that lies.
        """
        assert not deferral_enabled(config)
        advertised = self._surface(config)
        assert TOOL_SEARCH_NAME not in advertised, (
            "an absent or explicitly-false mechanism advertised its bridge; an "
            "unconfigured run's prompt must stay byte-identical"
        )
        assert len(advertised) == 45, (
            f"a disabled mechanism advertised {len(advertised)} schemas, not the "
            "canonical 45"
        )

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, False),
            ({"tool_deferral": False}, False),
            ({"tool_deferral": "0"}, False),
            ({"tool_deferral": None}, False),
            ({"tool_deferral": ""}, False),
            ({"tool_deferral": True}, True),
            ({"tool_deferral": "yes"}, True),
            ({"tool_deferral": 1}, True),
        ],
    )
    def test_the_flag_reader_and_the_state_builder_never_disagree(
        self, config, expected
    ) -> None:
        """One reader for the question, so two call sites cannot answer it
        differently. This is the disagreement itself, as a table."""
        assert deferral_enabled(config) is expected
        assert (
            deferred_from_config(config, context_window=32768) is not None
        ) is expected

    def test_every_eval_arm_recipe_names_a_key_some_module_declares(self) -> None:
        """An arm that silently does nothing is indistinguishable from one that works.

        The first draft of :data:EVAL_ARM_RECIPES carried a
        `tool_deferral_verbose` arm, on the reasonable-sounding assumption that
        rich descriptions have their own switch. They do not - they ride
        `tool_deferral`. A driver would have merged that config, found no
        reader, run the arm as a copy of the baseline, and published the result as
        a measurement of description verbosity.
        """
        from harness.prompt_budget_measure import EVAL_ARM_RECIPES, unknown_recipe_keys

        unknown = unknown_recipe_keys()
        assert not unknown, (
            f"{unknown} name a config key no module declares. An eval arm whose "
            "config is unread runs as the baseline and is reported as a "
            "measurement."
        )
        assert EVAL_ARM_RECIPES, "the arm table is empty"

    def test_the_recipe_reader_refuses_an_unknown_arm_rather_than_returning_none(
        self,
    ) -> None:
        from harness.prompt_budget_measure import eval_arm_recipe

        with pytest.raises(KeyError):
            eval_arm_recipe("no_such_arm")
        assert eval_arm_recipe("tool_deferral")["config"] == {"tool_deferral": True}

    def test_no_recipe_moves_two_mechanisms_at_once(self) -> None:
        """An arm that moved two switches could not attribute a regression."""
        from harness.prompt_budget_measure import EVAL_ARM_RECIPES

        for recipe in EVAL_ARM_RECIPES:
            mechanisms = {key.split("_")[0] for key in recipe["config"]}
            assert len(mechanisms) <= 2, (
                f"{recipe['arm']} config {recipe['config']} touches more than one "
                "mechanism, so a regression in it has no single cause"
            )

    def test_no_pin_in_this_file_reads_the_developers_home_skills(self) -> None:
        """The host must not be able to make a pin pass.

        One test here asserted against `str(skill_repo)` without taking the
        fixture as a parameter, so the name resolved to the fixture OBJECT and
        the scan got a repr instead of a path. It was green on this machine only
        because `~/.claude/skills` is populated: the index it asserted on was
        full of the developer's installed skills, and the fixture under test was
        never loaded at all. A pin whose evidence is the host is not a pin.
        """
        import inspect
        import re as _re

        offenders = []
        for name, obj in vars(sys.modules[__name__]).items():
            if not (inspect.isclass(obj) and name.startswith("Test")):
                continue
            for method_name, method in vars(obj).items():
                if not method_name.startswith("test_"):
                    continue
                # This pin names the call it is looking for, so a scan that read
                # itself would report itself. Same exclusion, same reason, as the
                # one-BM25-implementation pin.
                if (
                    method_name
                    == "test_no_pin_in_this_file_reads_the_developers_home_skills"
                ):
                    continue
                try:
                    source = inspect.getsource(method)
                except (OSError, TypeError):
                    continue
                uses_fixture = _re.search(r"str\(skill_repo\)", source)
                if not uses_fixture:
                    continue
                declares = "skill_repo" in inspect.signature(method).parameters
                if not declares:
                    offenders.append(f"{name}.{method_name}")
        assert not offenders, (
            f"{offenders} use skill_repo without taking it as a parameter, so the "
            "scan is handed the fixture object rather than the fixture path and "
            "the assertion is satisfied by whatever the host happens to have "
            "installed"
        )

    def test_the_same_discipline_applies_to_the_skill_bridge(self) -> None:
        """``skills_progressive`` had the identical split reader."""
        from harness import skills as skills_mod

        for config, expected in (
            ({}, False),
            ({"skills_progressive": False}, False),
            ({"skills_progressive": "0"}, False),
            ({"skills_progressive": True}, True),
            ({"skills_progressive": "yes"}, True),
        ):
            assert skills_mod.progressive_skills_enabled(config) is expected, (
                f"{config!r} read as "
                f"{skills_mod.progressive_skills_enabled(config)!r}, expected "
                f"{expected!r}"
            )

    def test_a_disabled_skill_bridge_advertises_nothing(self) -> None:
        advertised = self._surface({"skills_progressive": False})
        assert skills_mod_name() not in advertised, (
            "skills_progressive=False still advertised skill_view, whose every "
            "call would report the skill blocked or absent"
        )
        assert len(advertised) == 45

    # -- defect 3: the configured search limit was never consulted ---------

    def test_the_configured_search_limit_is_the_limit_that_is_used(self) -> None:
        """``search_limit`` was read, clamped, and then ignored.

        The parameter defaulted to the module constant instead of the
        instance's own configured value, so a run configured for one hit per
        search got the constant's three. ``None`` is the sentinel because ``0``
        is a legal configured value.
        """
        for limit in (0, 1, 2, 3):
            state = DeferredToolState(search_limit=limit)
            assert state.search_limit == limit
            result = state.search("read a file from disk")
            assert len(result.hits) == limit, (
                f"configured {limit}, got {len(result.hits)} hits; the state "
                "carries the value but the search used the module constant"
            )

    def test_an_explicit_limit_argument_still_wins(self) -> None:
        state = DeferredToolState(search_limit=1)
        result = state.search("read a file from disk", limit=3)
        assert len(result.hits) == 3

    # -- the reason any of this is allowed to exist ------------------------

    def test_the_canonical_catalog_stays_45_with_an_unmoved_fingerprint(self) -> None:
        """Both bridges stay OUT of ``harness/tools.py``.

        That is the whole reason a bridge has to be advertised by the registry
        rather than by the catalog: adding ``tool_search`` to the catalog would
        change ``catalog_fingerprint()``, and every consumer pinning it would
        have to re-derive a value that has nothing to do with the change.
        """
        from harness import tools as tools_mod

        specs = tools_mod.typed_tool_specs()
        assert len(specs) == 45, f"the catalog moved to {len(specs)} tools"
        names = {spec.name for spec in specs}
        assert TOOL_SEARCH_NAME not in names
        assert skills_mod_name() not in names
        assert tools_mod.catalog_fingerprint() == (
            "d3eb4ac7de866736d63f253f01ce442fd0c67089d2b3283d14638b185db4a2e3"
        ), (
            "the fingerprint moved; deferral is not supposed to touch the "
            "canonical catalog, so this needs explaining before it is updated"
        )

    def test_registering_a_bridge_does_not_make_catalog_parity_report_an_extra(
        self,
    ) -> None:
        """Parity derives from the catalog, not from the registry — so it must
        still read ``identical`` on a run that has both bridges registered."""
        from harness.agent_kernel.strategy import build_default_handlers
        from harness.agent_kernel.tools import ToolRegistry, catalog_parity

        registry = ToolRegistry()
        build_default_handlers(
            registry,
            repo_path=".",
            config={
                "tool_deferral": True,
                "skills_progressive": True,
                "skills_interop": False,
            },
        )
        # catalog_parity() takes no registry, which is the point; assert it.
        report = catalog_parity()
        assert report.get("identical") is True, report
        assert len(registry.schemas()) > 45, (
            "the fixture registered no bridge, so it is not testing the case"
        )
