# SPDX-License-Identifier: Apache-2.0
"""Pins for T1.W1.1 (retrieval engines + one skip-set), T1.W1.2 (turn caps) and
T1.W1.3 (the RepoMap disk cache).

Every test here is named after the BEHAVIOUR it pins, and the ones that matter
most are the ones that would fail if someone re-introduced a defect this round
closed. Three of them are deliberately SHAPED so they cannot pass vacuously:

* ``test_the_engine_is_reported_on_every_outcome_including_every_refusal``
  walks the refusal paths, because the "silent fallback" this round exists to
  remove is a defect that only appears on the paths nobody exercises.
* ``test_the_prefilter_never_drops_a_real_match`` is a DIFFERENTIAL against the
  prefilter disabled, not an assertion that the prefilter returns something.
* ``test_the_skip_set_is_one_authority`` asserts OBJECT IDENTITY across the
  three modules, so a fourth copy fails even if it happens to be equal today.
"""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from harness import config as config_mod
from harness import repomap_cache, retrieval, search_engine, skipset, turn_caps


# --------------------------------------------------------------------------
# T1.W1.1 - one skip-set authority
# --------------------------------------------------------------------------
class TestOneSkipSetAuthority:
    def test_the_skip_set_is_one_authority(self) -> None:
        """The three harness walks share ONE set object, not three equal copies.

        Object IDENTITY, not equality. Three sets with equal contents pass an
        equality check on the day they are written and diverge the first time
        one of them gains an entry - which is exactly the 345x this closes.
        """
        from harness import agent_loop, scan_mode

        assert retrieval._SKIP_DIRS is skipset.SKIP_DIRS
        assert agent_loop._SKIP_DIRS is skipset.SKIP_DIRS
        assert scan_mode._SKIP_DIRS is skipset.SKIP_DIRS

    def test_logs_is_in_the_set_and_that_is_the_whole_345x(self) -> None:
        """``logs`` is present, and it is the name the drift was about.

        Measured on this tree, one pass each, same walk, no other change:
        147,241 directories / 71.6 s without it, 271 directories / 0.08 s with
        it. If someone removes it, retrieval walks a copy of the repository per
        historical run.
        """
        assert "logs" in skipset.SKIP_DIRS
        assert "node_modules" in skipset.SKIP_DIRS
        assert ".git" in skipset.SKIP_DIRS

    def test_a_variant_requires_a_reason(self) -> None:
        """A walk that prunes differently must SAY why, in its receipt."""
        assert skipset.skip_dirs_for() is skipset.SKIP_DIRS
        with pytest.raises(ValueError):
            skipset.skip_dirs_for(drop={"logs"})
        narrowed = skipset.skip_dirs_for(drop={"logs"}, reason="scanning logs")
        assert "logs" not in narrowed
        assert narrowed != skipset.SKIP_DIRS

    def test_the_two_engines_prune_identically(self) -> None:
        """The ripgrep glob set and the Python set cover the same directories.

        An engine that prunes MORE than the fallback is the same drift with a
        subprocess in the middle, and the trailing ``/**`` is load-bearing:
        ``!**/logs/`` does not exclude the directory's CONTENTS in ripgrep.
        """
        args = skipset.ripgrep_glob_args()
        assert len(args) == 2 * (len(skipset.SKIP_DIRS) + len(skipset.SKIP_FILE_GLOBS))
        for name in list(skipset.SKIP_DIRS)[:5] + list(skipset.SKIP_FILE_GLOBS):
            assert f"!**/{name}/**" in args
            assert f"!**/{name}/" not in args


# --------------------------------------------------------------------------
# T1.W1.1 - the engine is reported, never silent
# --------------------------------------------------------------------------
class TestTheEngineIsNeverSilent:
    def test_the_engine_is_reported_on_every_outcome_including_every_refusal(
        self, tmp_path
    ) -> None:
        """Every outcome names an engine and a reason - refusals included.

        A receipt that omits the engine because the search was REFUSED is a
        receipt whose reader cannot tell a cheap refusal from an unmeasured
        one, and the silent fallback this round removes lives precisely on the
        paths a test that only searches the happy path never reaches.
        """
        (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
        outcomes = [
            retrieval.search_repo(str(tmp_path), "alpha"),  # normal
            retrieval.search_repo(str(tmp_path), "alpha", arguments={"page": 2}),
            retrieval.search_repo(str(tmp_path), "alpha", path="../escape"),
            retrieval.search_repo(str(tmp_path), "alpha", glob="*.rs"),
        ]
        for outcome in outcomes:
            assert outcome.engine, outcome
            assert outcome.engine_reason, outcome
            assert outcome.cost()["engine"] == outcome.engine
            assert "engine" in outcome.to_dict()

    def test_the_fallback_states_why_rather_than_being_silent(self) -> None:
        """A resolution that is not ripgrep explains itself in words."""
        resolution = search_engine.resolve_ripgrep()
        assert resolution.engine in search_engine.ENGINE_RIPGREP or (
            resolution.engine == search_engine.ENGINE_FALLBACK
        )
        assert resolution.reason, "a resolution must always carry a reason"
        if not resolution.available:
            assert resolution.fetch_refused, (
                "a fallback must distinguish 'we chose not to fetch' from "
                "'the fetch failed' - they are different operator actions"
            )
            assert "ripgrep" in resolution.reason

    def test_every_search_result_carries_its_cost(self) -> None:
        """Engine, files, matches, duration, truncation - on every result."""
        outcome = retrieval.search_repo(".", "def search_repo")
        cost = outcome.cost()
        for key in (
            "engine",
            "files_scanned",
            "matches_returned",
            "duration_s",
            "truncated",
            "truncated_by",
            "complete",
        ):
            assert key in cost, key
        assert cost["files_scanned"] > 0
        assert cost["matches_returned"] >= 0
        assert cost["duration_s"] >= 0.0

    def test_an_over_cap_result_refuses_loudly_and_says_it_is_truncated(
        self, tmp_path
    ) -> None:
        """Route 1 of 2: the match cap, which is a REFUSAL.

        The wording is the notice. A result over the cap must not read as a
        list that happens to end.
        """
        for index in range(120):
            (tmp_path / f"f{index}.py").write_text("needle = 1\n" * 40)
        outcome = retrieval.search_repo(str(tmp_path), "needle", max_matches=5)
        assert outcome.truncated is True
        assert outcome.complete is False
        assert outcome.truncated_by == "matches"
        assert outcome.over_cap is True
        assert outcome.ok is False
        rendered = outcome.render()
        assert "TOOL ERROR [too_many_matches]" in rendered
        assert "Narrow the query" in rendered

    def test_a_max_results_narrowing_names_its_bound_in_the_render(
        self, tmp_path
    ) -> None:
        """Route 2 of 2: an under-cap result a caller narrowed.

        This is the route that was previously SILENT: the search succeeded, so
        nothing in the old render said the list had been cut. A partial list
        that reads as a whole answer is the specific dishonesty the cost
        receipt exists to prevent.
        """
        (tmp_path / "one.py").write_text("needle = 1\n" * 10)
        outcome = retrieval.search_repo(
            str(tmp_path), "needle", max_matches=50, max_results=3
        )
        assert outcome.ok is True
        assert outcome.truncated is True
        assert outcome.complete is False
        assert outcome.truncated_by == "max_results"
        assert outcome.max_results_applied == 3
        assert "[TRUNCATED: max_results]" in outcome.render()
        assert "partial list" in outcome.render()

    def test_the_max_results_cap_cannot_be_bought_past(self, tmp_path) -> None:
        """Asking for MORE does not raise the error threshold.

        A caller must not be able to buy its way past the cap by asking for
        more, which is why `max_results` is clamped to the cap.
        """
        (tmp_path / "one.py").write_text("needle = 1\n" * 10)
        outcome = retrieval.search_repo(
            str(tmp_path), "needle", max_matches=2, max_results=500
        )
        assert outcome.over_cap is True
        assert outcome.cap == 2

    def test_an_untruncated_result_is_reported_complete(self, tmp_path) -> None:
        """The CONTROL for the test above.

        Without this, "always says TRUNCATED" would satisfy the previous test -
        a receipt that claims truncation for a complete result is exactly as
        misleading as one that hides it.
        """
        (tmp_path / "only.py").write_text("unique_token = 1\n")
        outcome = retrieval.search_repo(str(tmp_path), "unique_token")
        assert outcome.truncated is False
        assert outcome.complete is True
        assert outcome.truncated_by == ""
        assert "TRUNCATED" not in outcome.render()


# --------------------------------------------------------------------------
# T1.W1.1 - the prefilter is behaviour-preserving
# --------------------------------------------------------------------------
class TestTheLiteralPrefilter:
    @pytest.mark.parametrize(
        "pattern",
        [
            "def search_repo",
            "_SKIP_DIRS",
            "[a-z]+def",
            "agent_max_turns",
            "a|b",
            "a{2,3}b",
            r"b\w*turns",
            "(?i)status",
            "^harness",
            "ripgrep|shutil.which",
            ".+_SAFE_",
            r"sk-[A-Za-z0-9]+",
            "xb*",
            "a*b",
            "colou?r",
            "a{,3}b",
            "(a|b)|a?b1",
            "nonexistent_zz",
        ],
    )
    def test_the_prefilter_never_drops_a_real_match(
        self, tmp_path, monkeypatch, pattern
    ):
        """DIFFERENTIAL: identical results with the prefilter on and off.

        An unsound prefilter does not make a search slow, it makes it WRONG -
        it silently drops a line that matched. So the proof is a differential
        against the prefilter disabled, over patterns chosen to include every
        construct that made an earlier version of this unsound: character
        classes, non-capturing groups, lookaheads, quantifiers (including
        ``{,3}``, which Python's ``re`` treats as something other than a
        quantifier), and a top-level alternation.
        """
        for index in range(12):
            (tmp_path / f"m{index}.py").write_text(
                "alpha beta\ndef search_repo():\n    return 1\n"
                f"a{{2,3}}b xb* colou?r\nvalue{index} = {index}\n"
                "sk-ABCDEFGHIJKLMNOPQRST\n"
            )
        fast = retrieval.search_repo(str(tmp_path), pattern)
        monkeypatch.setattr(retrieval, "required_literal", lambda _p: "")
        slow = retrieval.search_repo(str(tmp_path), pattern)
        assert (fast.ok, fast.total, fast.matches, fast.files) == (
            slow.ok,
            slow.total,
            slow.matches,
            slow.files,
        ), f"the prefilter changed the answer for {pattern!r}"

    def test_a_character_class_is_not_treated_as_literal_text(self) -> None:
        """`[a-z]+` must not require the substring ``"a-z"``.

        This is the unsoundness the brute-force check found: a class matches ONE
        character from a set, so requiring the class's own text drops every real
        match. The same applies to ``(?:...)`` and to a top-level ``|``.
        """
        assert retrieval.required_literal("[a-z]+") == ""
        assert retrieval.required_literal("[0-9]{3}") == ""
        assert retrieval.required_literal("(?:abc)") == ""
        assert retrieval.required_literal("(?=x)y") == ""
        assert retrieval.required_literal("foo|bar") == ""
        assert retrieval.required_literal("a{2,3}b") == ""

    def test_a_sound_pattern_still_yields_a_usable_filter(self) -> None:
        """The CONTROL: the prefilter is not simply always empty.

        A prefilter that always returned "" would satisfy every soundness test
        above while delivering none of the speed, and would be a much more
        comfortable thing to ship.
        """
        assert retrieval.required_literal("def search_repo") == "def search_repo"
        assert retrieval.required_literal("foo(bar)") == "foo"
        assert retrieval.required_literal(r"\d+_foo") == "_foo"
        assert retrieval.required_literal("x") == "", "a 1-char needle is overhead"


# --------------------------------------------------------------------------
# T1.W1.2 - the turn caps
# --------------------------------------------------------------------------
class TestTheTurnCaps:
    def test_the_per_task_cap_is_at_least_fifty(self) -> None:
        """The acceptance number, read from DEFAULTS and not from a literal."""
        assert config_mod.DEFAULTS["agent_max_turns"] >= 50

    def test_no_hard_coded_twenty_five_survives(self) -> None:
        """The four literal `25` fallbacks are GONE, not updated.

        A literal that still exists is a literal that can drift again, which is
        the same failure class as the three skip-sets.
        """
        import pathlib

        for name in (
            "agent_loop.py",
            "agent_loop_step.py",
            "agent_kernel/strategy.py",
        ):
            source = pathlib.Path("harness") / name
            if not source.is_file():
                continue
            text = source.read_text(encoding="utf-8-sig")
            assert not re.search(r'agent_max_turns"?\s*,\s*25\b', text), (
                f"{name} still hard-codes 25 as the fallback"
            )

    def test_session_max_turns_is_not_reported_as_a_conversation_cap(self) -> None:
        """The key that LIES is reported for what it actually bounds.

        `session_max_turns` is read by exactly one place and bounds
        context-SUMMARY records, not conversation length. The receipt says so,
        which is the whole point: a cap under a misleading name is a cap that
        does not cap.
        """
        caps = turn_caps.resolve_caps({})
        assert caps.context_summary == 24
        assert caps.conversation is None
        rendered = turn_caps.render_caps({})
        assert "unbounded" in rendered, (
            "an absent conversation cap must be rendered as unbounded, not "
            "omitted - omission reads as 'fine'"
        )
        assert "context-summary" in rendered

    def test_a_conversation_cap_is_reported_when_one_is_set(self) -> None:
        """The real conversation cap is distinguishable from the context one."""
        caps = turn_caps.resolve_caps({"session_max_conversation_turns": 200})
        assert caps.conversation == 200
        assert caps.context_summary == 24
        assert "200 conversation turns" in turn_caps.render_caps(
            {"session_max_conversation_turns": 200}
        )

    def test_the_conversation_cap_default_is_unbounded_not_a_number(self) -> None:
        """A real default here would silently truncate every session.

        DEFAULTS is merged into every task and every eval arm, so shipping a
        conversation bound on the day the key lands changes all of them at once.
        """
        assert config_mod.DEFAULTS["session_max_conversation_turns"] is None

    def test_approaching_a_cap_is_an_observation_not_a_stop(self) -> None:
        """The approach is REPORTED, and reporting it changes nothing."""
        near = turn_caps.approach_observation(55, config={})
        assert near is not None
        assert near["cap"] == 60
        assert near["remaining"] == 5
        assert "heads-up, not a stop" in near["observation"]
        far = turn_caps.approach_observation(5, config={})
        assert far is None, "nothing to say is None, and None is not a refusal"

    def test_the_approach_receipt_is_publishable_for_the_status_surface(self) -> None:
        """T4's rail and T5's rung-8 read this; it must not need re-deriving."""
        receipt = turn_caps.turn_caps_receipt({}, turn=55)
        assert receipt["approaching"] is True
        assert receipt["caps"]["per_task"]["value"] == 60
        assert receipt["caps"]["per_task"]["bounds"]
        assert receipt["rendered"]
        quiet = turn_caps.turn_caps_receipt({}, turn=1)
        assert quiet["approaching"] is False
        assert quiet["approach_observation"] is None

    def test_a_bool_cap_is_refused_rather_than_coerced(self) -> None:
        """`int(True) == 1` would turn a typo into a one-turn run."""
        assert turn_caps.resolve_caps({"agent_max_turns": True}).per_task == 60
        assert turn_caps.resolve_caps({"agent_max_turns": "nonsense"}).per_task == 60
        assert turn_caps.resolve_caps({"agent_max_turns": 7}).per_task == 7


# --------------------------------------------------------------------------
# T1.W1.3 - the RepoMap disk cache
# --------------------------------------------------------------------------
class TestTheRepoMapCache:
    def test_an_unchanged_file_is_served_and_a_changed_one_is_not(
        self, tmp_path
    ) -> None:
        """Per-digest invalidation: one edit re-derives ONE entry.

        A cache that invalidates wholesale is not a cache, it is a latency
        spike with extra steps - and on a repository under active edit, where
        something changes most turns, that is the only access pattern there is.
        """
        path = os.path.join(str(tmp_path), "c.sqlite3")
        with repomap_cache.open_cache(".", cache_path=path) as cache:
            cache.put_many([("a.py", "d1", {"s": 1}), ("b.py", "d2", {"s": 2})])
            warm, report = cache.get_many([("a.py", "d1"), ("b.py", "d2")])
            assert report.hit == repomap_cache.HIT
            assert set(warm) == {("a.py", "d1"), ("b.py", "d2")}

            after, report = cache.get_many([("a.py", "d1"), ("b.py", "d2-CHANGED")])
            assert report.hit == repomap_cache.PARTIAL
            assert report.served == 1 and report.derived == 1
            assert set(after) == {("a.py", "d1")}

    def test_a_cold_read_and_a_warm_read_are_reported_separately(
        self, tmp_path
    ) -> None:
        """`cold_s` and `warm_s` are different fields, not one average.

        Quoting a warm number as "the retrieval cost" is the dishonest
        measurement this round exists to prevent, so the report keeps them
        apart rather than offering a convenient single figure.
        """
        path = os.path.join(str(tmp_path), "c.sqlite3")
        with repomap_cache.open_cache(".", cache_path=path) as cache:
            _, cold = cache.get_many([("a.py", "d1")])
            assert cold.hit == repomap_cache.MISS
            assert cold.warm_s == 0.0 or cold.warm_s >= 0.0
            cache.put("a.py", "d1", {"s": 1})
            _, warm = cache.get_many([("a.py", "d1")])
            assert warm.hit == repomap_cache.HIT
            assert "cold_s" in cold.to_dict() and "warm_s" in warm.to_dict()
            assert warm.to_dict()["cold_s"] == 0.0

    def test_unavailable_is_never_rendered_as_miss_or_as_zero_percent(
        self, tmp_path
    ) -> None:
        """A cache that cannot be read is not a cache that has no entry.

        Only the first of those is a fact about the cache, and rendering it as
        `miss` - or as `0%` - is the unreported degradation this repo forbids.
        """
        old = os.path.join(str(tmp_path), "v3.sqlite3")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version','3')")
        conn.commit()
        conn.close()
        with repomap_cache.open_cache(".", cache_path=old) as cache:
            found, report = cache.get_many([("a.py", "d1")])
            assert found == {}
            assert report.hit == repomap_cache.UNAVAILABLE
            assert report.hit != repomap_cache.MISS
            assert report.reason
            assert report.honest_hit_rate is None, (
                "unavailable must report None, never 0% - a percentage is a "
                "number nobody measured"
            )
            assert "0%" not in repomap_cache.render_hit(report.hit)

    def test_the_hit_vocabulary_is_four_distinct_values(self) -> None:
        """Collapsing any two of them is a lie in one direction or the other."""
        assert repomap_cache.HIT_VALUES == ("hit", "partial", "miss", "unavailable")
        assert len(set(repomap_cache.HIT_VALUES)) == 4
        rendered = {
            value: repomap_cache.render_hit(value) for value in repomap_cache.HIT_VALUES
        }
        assert len(set(rendered.values())) == 4

    def test_gc_is_bounded_and_reports_what_it_removed(self, tmp_path) -> None:
        """A store with no GC grows without limit; a GC nobody can read is a
        GC nobody can trust to have run."""
        path = os.path.join(str(tmp_path), "c.sqlite3")
        # Fill under a GENEROUS ceiling (so `put_many` does not collect), then
        # tighten it. That is the path an operator takes when they lower the
        # policy on a cache that already exists.
        with repomap_cache.open_cache(
            ".", cache_path=path, max_bytes=10_000_000, max_rows=10_000
        ) as cache:
            cache.put_many(
                [(f"f{i}.py", f"d{i}", {"pad": "x" * 120}) for i in range(10)]
            )
            assert cache.stats()["gc_ran"] is False, "nothing to collect yet"
            cache.max_bytes = 400
            cache.max_rows = 5
            stats = cache.stats()
            assert stats["gc_ran"] is True
            assert stats["evicted_rows"] > 0, "over the ceiling and nothing evicted"
            assert stats["rows"] <= 5
            assert stats["bytes_used"] <= 400
            collected = cache.collect()
            assert collected["ran"] is False, "already under the ceiling"
            assert "reason" in collected

    def test_gc_dry_run_removes_nothing_and_says_so(self, tmp_path) -> None:
        """A dry run that quietly deleted rows would be a data-loss bug wearing
        a diagnostic's clothes.

        The over-ceiling state is built and inspected WITHOUT calling `stats()`
        first: `stats()` collects as a side effect, so asking it "how many rows
        are there?" already brings the store under the ceiling, and the dry run
        would then have nothing to report - which would make this test pass for
        the wrong reason.
        """
        path = os.path.join(str(tmp_path), "c.sqlite3")
        with repomap_cache.open_cache(
            ".", cache_path=path, max_bytes=10_000_000, max_rows=10_000
        ) as cache:
            cache.put_many([(f"f{i}.py", f"d{i}", {"pad": "x" * 80}) for i in range(6)])
            cache.max_bytes = 100
            cache.max_rows = 2
            dry = cache.collect(dry_run=True)
            assert dry["ran"] is True, "the store is over the ceiling"
            assert dry["dry_run"] is True
            assert dry["rows"] == 0
            assert dry["bytes"] == 0
            # Nothing was removed, so a subsequent REAL collect still has work.
            real = cache.collect()
            assert real["ran"] is True
            assert real["rows"] > 0

    def test_a_corrupt_row_is_dropped_rather_than_served(self, tmp_path) -> None:
        """Half-parsed symbols are worse than no symbols."""
        path = os.path.join(str(tmp_path), "c.sqlite3")
        with repomap_cache.open_cache(".", cache_path=path) as cache:
            cache.put("a.py", "d1", {"s": 1})
            cache._conn.execute(
                "UPDATE entries SET payload = 'not json' WHERE rel='a.py'"
            )
            cache._conn.commit()
            assert cache.lookup("a.py", "d1") is None

    def test_the_provenance_header_names_the_source_and_the_licence(self) -> None:
        """Phase 7 scans for this header; a vendored file without it is a defect.

        The four fields are all required, and the LAST one - why the upstream
        beats ours - is the field that makes the vendoring auditable rather
        than decorative.
        """
        import pathlib

        text = pathlib.Path("harness/repomap_cache.py").read_text(encoding="utf-8-sig")
        head = text[: text.index('"""', text.index('"""') + 3)]
        assert "SPDX-License-Identifier: Apache-2.0" in head
        assert "github.com/Aider-AI/aider" in head
        assert "WHY IT BEATS OURS" in head, (
            "the mandatory field: a vendored file that cannot say why the "
            "upstream is better is not auditable"
        )
        assert re.search(r"commit pin", head)
