"""Pins for the MULTI-SESSION evaluation, and the evaluation's own honesty gates.

**The three tests in this file that matter most are the ones that could make the
evaluation look better than it is:**

* the **plant** -- a marker that exists ONLY in session 1's conversation and is
  asserted absent from session 3's brief. That is what makes "session boundaries
  are real" a measurement instead of an assertion, and it is the difference
  between this evaluation measuring session SURVIVAL and measuring compaction;
* the **control** -- one session with the same turn budget must fail to finish
  the task, or the whole evaluation reports ``task_fits_one_session`` rather
  than a result;
* the **discrimination** receipt -- a metric that is identical under every arm has
  not measured the arms, and the report says which those are.

Host-only: no Docker, no provider, no network, no credential. The feature
verification runs real ``python -m pytest`` on the host; the nightly lane is
where the Docker-backed repetitions live.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from evals import multi_session as MS
from harness import long_horizon as LH
from memory import feature_ledger_store as FL

PROJECT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# The task must genuinely require multiple sessions
# ---------------------------------------------------------------------------


class TestTheTaskRequiresMultipleSessions:
    def test_one_session_with_the_same_budget_cannot_finish_the_task(self, tmp_path: Path):
        """THE CONTROL. If this fails, the evaluation measures nothing.

        A single session is given the SAME per-session turn budget as a session
        inside the multi-session run, and must leave real work undone. This is
        asserted by driving the real gate, not by reading the session count.
        """
        repo = MS.build_task_repo(tmp_path / "repo")
        features = MS.task_features(repo)
        assert len(features) == MS.DEFAULT_SESSIONS >= 3, "at least three features, so three sessions"

        # Exactly one feature's implementation exists.
        (Path(repo["repo"]) / "shop" / f"{features[0].id}.py").write_text(
            f"def {features[0].id}_value(items):\n    return sum(items)\n", encoding="utf-8", newline="\n"
        )
        assert MS._host_pytest_gate(repo, f"tests/test_{features[0].id}.py") is True
        for feature in features[1:]:
            assert MS._host_pytest_gate(repo, f"tests/test_{feature.id}.py") is False, (
                "an unimplemented feature must FAIL its own check, or the control arm "
                "would be measuring a fixture that starts out finished"
            )

    def test_the_control_verdict_is_published_rather_than_swallowed(self, tmp_path: Path):
        """A task that FITS one session makes every multi-session number meaningless.

        Driven by building a task with FEWER features than the session count, so
        the control cannot refuse, and asserting the refusal is reported rather
        than quietly turned into a number.
        """
        captured: dict = {}

        def gate(task, target):
            captured["called"] = True
            return True  # every feature "already passes": the task fits one session

        report = MS._one_effort(
            task=None,
            arm_config=dict(MS.MULTI_SESSION_ARMS["initializer"]),
            sessions=1,
            session_turns=MS.DEFAULT_SESSION_TURNS,
            gate=gate,
            out_root=tmp_path,
            repetition=0,
        )[0]
        assert report.verdict == "task_fits_one_session"
        assert captured.get("called") is True, "the control must actually RUN, not be assumed"
        for name in MS.METRIC_NAMES:
            metric = report.metrics[name]
            assert metric.measured is False
            assert "fits one session" in metric.reason


# ---------------------------------------------------------------------------
# Session boundaries are real -- the plant
# ---------------------------------------------------------------------------


class TestTheSessionBoundaryIsReal:
    def test_a_marker_that_exists_only_in_session_1_conversation_never_reaches_session_3(
        self, tmp_path: Path
    ):
        """The plant. This is the pin the whole evaluation stands on.

        ``SESSION_ONE_ONLY`` is the kind of content a long context WOULD carry:
        a fact the model discovered while working. It is written into a
        conversation file and never into any durable artefact, and session 3's
        brief must not contain it.

        A long-context design would fail this, which is the point -- it measures
        session SURVIVAL, not compaction.
        """
        repo = MS.build_task_repo(tmp_path / "repo")
        root = Path(repo["repo"])
        marker = "SESSION_ONE_ONLY_a4f9c1_secret_discovered_in_turn_2"
        conversation = root / "conversation.jsonl"
        conversation.write_text(
            json.dumps({"turn": 2, "role": "assistant", "content": marker}) + "\n", encoding="utf-8"
        )

        brief = MS.build_session_brief(repo, root, session_index=3, claimed_feature="pricing")
        rendered = brief.render()
        assert marker not in rendered, (
            "session 3's brief carried content from session 1's CONVERSATION. That makes "
            "this a compaction measurement, not a session-survival measurement."
        )
        # And the brief carries the DURABLE artefacts instead.
        assert str(repo["request"]) in rendered
        assert brief.to_dict()["carries_prior_conversation"] is False

    def test_the_brief_size_does_not_grow_with_a_huge_prior_conversation(self, tmp_path: Path):
        """The same claim as a MEASUREMENT: brief bytes are independent of history.

        A brief built after a 2 MB conversation must be byte-identical to one
        built after no conversation at all. A compaction-based design fails this by
        an enormous margin, and the margin is the finding.
        """
        repo = MS.build_task_repo(tmp_path / "repo")
        root = Path(repo["repo"])
        before = MS.build_session_brief(repo, root, session_index=3, claimed_feature="pricing").render()

        (root / "conversation.jsonl").write_text(
            "".join(json.dumps({"turn": i, "content": "x" * 400}) + "\n" for i in range(5000)),
            encoding="utf-8",
        )
        after = MS.build_session_brief(repo, root, session_index=3, claimed_feature="pricing").render()
        assert len(after) == len(before), "brief size must not depend on prior conversation size"
        assert after == before

    def test_the_brief_signature_takes_no_prior_session(self):
        """Asserted structurally: there is no argument through which history arrives."""
        import inspect

        parameters = set(inspect.signature(MS.build_session_brief).parameters)
        assert parameters == {
            "task",
            "root",
            "session_index",
            "claimed_feature",
            "constraints",
            "initializer",
        }
        for forbidden in ("history", "messages", "conversation", "summary", "previous", "last"):
            assert not any(forbidden in name for name in parameters), (
                f"a {forbidden!r} parameter would let a prior session into the brief"
            )

    def test_the_initializer_arm_brief_carries_the_rules_and_the_baseline_does_not(self, tmp_path: Path):
        """A constraint that was never stated cannot be retained -- and says so."""
        repo = MS.build_task_repo(tmp_path / "repo")
        root = Path(repo["repo"])
        LH.initialize_project(
            str(root),
            LH.ProjectProfile(name="shop", language="python", test_command="python -m pytest -q"),
            MS.task_features(repo),
            request=str(repo["request"]),
            project_id="shop",
            session="s0",
            git_runner=None,
        )
        with_init = MS.build_session_brief(repo, root, session_index=3, claimed_feature="pricing")
        assert with_init.constraints, "the initializer arm's brief must state the durable rules"
        assert "tests/*" in with_init.render()

        without = MS.build_session_brief(
            repo, root, session_index=3, claimed_feature="pricing", initializer=False
        )
        assert without.constraints == ()
        assert "tests/*" not in without.render()


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------


class TestTheArms:
    def test_there_are_exactly_four_arms_and_each_turns_on_one_thing(self):
        assert sorted(MS.MULTI_SESSION_ARMS) == [
            "baseline",
            "initializer",
            "read_before_edit",
            "subagent_isolation",
        ]
        assert MS.MULTI_SESSION_ARMS["baseline"] == {}, (
            "the baseline must set NOTHING: an arm that sets a key is not the OFF arm"
        )
        # Each non-baseline arm is the initializer plus exactly one distinguishing key.
        base = set(MS.MULTI_SESSION_ARMS["initializer"])
        for arm in ("read_before_edit", "subagent_isolation"):
            extra = set(MS.MULTI_SESSION_ARMS[arm]) - base
            assert len(extra) == 1, f"{arm} adds {extra}; an arm must isolate ONE thing"

    def test_no_arm_key_is_published_in_the_harness_defaults(self):
        """A real value in ``DEFAULTS`` would switch every task and every eval arm.

        That is why these arms live in their own table, following
        ``evals/memory_ablation.py::MEMORY_ARMS``. If a Phase-4 owner publishes one
        of these keys with a value, this test fails and the separation is restored
        deliberately rather than by accident.
        """
        assert MS.opt_in_keys_not_in_defaults() == ()
        assert "read_before_edit" in MS.OPT_IN_KEYS, (
            "the read_before_edit arm must read the key the product already has, not an alias"
        )

    def test_an_unknown_arm_is_refused_by_name(self):
        """An unknown arm returning ``{}`` would publish the BASELINE as that arm."""
        with pytest.raises(KeyError, match="unknown multi-session arm"):
            MS.arm_config("initializer_but_faster")

    def test_a_configured_but_inert_mechanism_is_reported_not_measured(self, tmp_path: Path):
        """The arm table's central hazard: an arm that runs but measures the baseline.

        Driven by asking for the initializer over a root where nothing was
        initialized, which is exactly the state a broken wiring produces.
        """
        repo = MS.build_task_repo(tmp_path / "repo")
        reason = MS._arm_mechanism_wired(
            MS.MULTI_SESSION_ARMS["initializer"], repo, Path(repo["repo"])
        )
        assert reason is not None
        assert "initializer" in reason and "baseline" in reason
        assert MS._arm_mechanism_wired(
            MS.MULTI_SESSION_ARMS["baseline"], repo, Path(repo["repo"])
        ) is None


# ---------------------------------------------------------------------------
# The six metrics, and the null results
# ---------------------------------------------------------------------------


class TestTheSixMetrics:
    def test_the_metric_set_is_the_one_the_brief_asks_for(self):
        assert MS.METRIC_NAMES == (
            "completion",
            "rework_rate",
            "constraint_retention",
            "parent_context_tokens",
            "tokens_per_session",
            "cost_usd",
        )

    def test_a_null_metric_says_why_and_is_not_zero(self):
        metric = MS.Metric("cost_usd", None, reason="the scripted boundary has no price")
        assert metric.measured is False
        assert metric.value is None, "a null is None; 0.0 would put a number nobody took in a table"
        assert "NOT MEASURED" in metric.render()
        assert metric.to_dict()["measured"] is False

    def test_cost_is_null_under_the_scripted_boundary_and_states_why(self):
        outcome = MS.SessionOutcome(
            index=1, claimed_feature="receipts", status="completed", finished_feature="receipts"
        )
        metric = MS.metric_cost_usd([outcome])
        assert metric.measured is False
        assert "$0.00" in metric.reason, "the reason must say why a zero would be a fabrication"

    def test_rework_rate_counts_files_an_earlier_session_already_touched(self):
        sessions = [
            MS.SessionOutcome(index=1, claimed_feature="a", status="completed", changed_files=("shop/a.py",)),
            MS.SessionOutcome(
                index=2, claimed_feature="b", status="completed", changed_files=("shop/a.py", "shop/b.py")
            ),
            MS.SessionOutcome(
                index=3, claimed_feature="c", status="completed", changed_files=("shop/c.py", "PROGRESS.md")
            ),
        ]
        metric = MS.metric_rework_rate(sessions)
        assert metric.measured is True
        assert metric.value == pytest.approx(0.25), "session 2 redid 1 of 2; session 3 redid 0 of 2; the MEAN is 0.25"
        assert metric.per_session == (0.5, 0.0)

    def test_rework_needs_two_sessions_and_says_so(self):
        metric = MS.metric_rework_rate([MS.SessionOutcome(index=1, claimed_feature="a", status="completed")])
        assert metric.measured is False
        assert "two sessions" in metric.reason

    def test_constraint_retention_is_null_when_no_rules_were_stated(self):
        sessions = [
            MS.SessionOutcome(
                index=index,
                claimed_feature="a",
                status="completed",
                changed_files=("shop/a.py",),
                brief=MS.SessionBrief(session_index=index, request="r", init_script="", progress_text="", feature_view=(), claimed_feature="a"),
            )
            for index in (1, 2)
        ]
        metric = MS.metric_constraint_retention(sessions)
        assert metric.measured is False
        assert "no constraint set was stated" in metric.reason

    def test_constraint_retention_scores_a_violated_constraint(self):
        brief = MS.SessionBrief(
            session_index=2, request="r", init_script="", progress_text="", feature_view=(), claimed_feature="a",
            constraints=MS.CONSTRAINTS,
        )
        sessions = [
            MS.SessionOutcome(
                index=1, claimed_feature="a", status="completed", changed_files=("shop/a.py",), brief=brief
            ),
            MS.SessionOutcome(
                index=2,
                claimed_feature="b",
                status="completed",
                changed_files=("tests/test_a.py",),
                brief=brief,
            ),
        ]
        metric = MS.metric_constraint_retention(sessions)
        assert metric.measured is True
        assert metric.value == round(2 / 3, 4), "2 of 3 constraints honoured, rounded to the published precision"
        assert "no_test_edits" in metric.reason

    def test_a_metric_that_does_not_vary_is_reported_as_not_discriminating(self):
        """The honesty receipt: identical values under every arm measured nothing."""
        from dataclasses import dataclass

        @dataclass
        class _Fixed(MS.Metric):
            pass

        arms = (
            MS.ArmResult(
                arm="baseline",
                config={},
                verdict="measured",
                metrics={"completion": MS.Metric("completion", 1.0)},
            ),
            MS.ArmResult(
                arm="initializer",
                config={},
                verdict="measured",
                metrics={"completion": MS.Metric("completion", 1.0)},
            ),
        )
        report = MS.MultiSessionReport(arms=arms, sessions=3, repetitions=1, session_turns=6)
        rows = report.discrimination()["per_metric"]
        assert rows["completion"]["varies"] is False
        assert report.discrimination()["metrics_that_vary"] == []
        assert "has not measured the arms" in report.discrimination()["note"]


# ---------------------------------------------------------------------------
# Repetitions, and the end-to-end shape
# ---------------------------------------------------------------------------


class TestTheRunShape:
    def test_repetitions_are_the_default_and_each_is_a_separate_repository(self, tmp_path: Path):
        """One run is noise, and the repetitions must not share state.

        **P4/W2.3: a repetition is now one pass over EVERY task**, so the
        per-repetition record shape changed and this was rewritten rather than
        deleted. What the original line asserted -- ``record["repetitions"] is
        not None or True`` -- was vacuous on arrival, and a vacuous line in a
        test about not sharing state is the wrong kind of tautology.
        """
        report = MS.run_arm("baseline", repetitions=3, gate=lambda task, target: False, out_root=tmp_path)
        assert MS.DEFAULT_REPETITIONS >= 3
        assert len(report.repetitions) == 3
        assert [item["repetition"] for item in report.repetitions] == [0, 1, 2]
        # Every repetition covered every task and published its own sessions.
        for record in report.repetitions:
            assert record["tasks"] == sorted(MS.TASK_SLUGS)
            assert record["sessions"], "a repetition published no session records at all"
            # The non-vacuity half: the sessions cover BOTH tasks, one feature per
            # session each. A repetition that reused the previous one's state
            # would fail here; so would one that silently dropped a task.
            by_task: dict = {}
            for item in record["sessions"]:
                by_task.setdefault(str(item.get("task")), []).append(
                    str(item.get("claimed_feature"))
                )
            assert sorted(by_task) == sorted(MS.TASK_SLUGS), (
                f"repetition {record['repetition']} covered {sorted(by_task)}; one repetition "
                f"is one pass over EVERY task, so it should cover {sorted(MS.TASK_SLUGS)}"
            )
            for slug, features in by_task.items():
                assert features == list(MS.TASK_FEATURE_NAMES[slug]), (
                    f"repetition {record['repetition']} worked on {features} for task {slug}; "
                    f"it should be one session per feature, {list(MS.TASK_FEATURE_NAMES[slug])}"
                )
        # And they really are separate: no two repetitions share a session index set
        # by reusing a repository. Each repetition's repos are cleaned up, so the
        # observable proof is that each repetition produced a full set rather than
        # inheriting the previous one's.
        assert len({len(record["sessions"]) for record in report.repetitions}) == 1, (
            "the repetitions produced different numbers of sessions; one is running with "
            "state another left behind"
        )

    def test_the_report_declares_that_it_is_not_a_model_quality_measurement(self, tmp_path: Path):
        report = MS.run_multi_session(["baseline"], repetitions=1, gate=lambda task, target: False, out_root=tmp_path)
        assert report.to_dict()["model_quality_measured"] is False
        assert "heuristic" in report.to_dict()["token_estimator"]
        assert "NOT MEASURED" in report.render()

    def test_the_report_publishes_its_own_coverage_limitation(self, tmp_path: Path):
        """**Rewritten in P4/W2.3, and the rewrite made it STRONGER.**

        The original asserted the honest note said "one task". P4/W2.3 closed
        that gap by adding a second task, so the sentence it pinned had become
        false and the pin fired -- which is exactly what it was written to do.

        A test that only said "some limitation is published" would have passed on
        any limitation at all. So this now demands the two things a reader
        actually needs: the note states the task COUNT that ran, and the
        uncovered shapes are enumerated rather than gestured at.
        """
        report = MS.run_multi_session(["baseline"], repetitions=1, gate=lambda task, target: False, out_root=tmp_path)
        payload = report.to_dict()
        assert f"{len(MS.TASK_SLUGS)} task(s)" in payload["honest_note"], (
            "the honest note must state how many tasks actually ran, so a reader cannot "
            "assume the whole table was"
        )
        assert payload["task_slugs"] == list(MS.TASK_SLUGS)
        assert payload["task_shapes_not_covered"], (
            "the report stopped enumerating the shapes its task set does NOT reach; two "
            "tasks read as coverage, and coverage without a boundary is the overclaim this "
            "phase exists to avoid"
        )
        assert sorted(payload["arms_available"]) == sorted(MS.MULTI_SESSION_ARMS)

    def test_a_session_publishes_its_verification_through_the_store(self, tmp_path: Path):
        """The ledger transition really goes through the STORE's rules.

        Driven with a gate that passes and an evidence record written the way the
        store requires; a store that refused would leave the feature ``failing``
        and the audit would say so.
        """
        repo = MS.build_task_repo(tmp_path / "repo")
        root = Path(repo["repo"])
        LH.initialize_project(
            str(root),
            LH.ProjectProfile(name="shop", language="python", test_command="python -m pytest -q"),
            MS.task_features(repo),
            request=str(repo["request"]),
            project_id="shop",
            session="s0",
            git_runner=None,
        )
        store, refusal = FL.LedgerStore.open(root)
        assert store is not None and refusal is None
        store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")

        outcome = MS.SessionOutcome(
            index=1, claimed_feature="receipts", status="completed", finished_feature="receipts"
        )
        MS._publish(store, outcome)

        document = store.document()
        assert document is not None
        feature = next(item for item in document["features"] if item["id"] == "receipts")
        assert feature["status"] == LH.PASSING_FEATURE_STATUS
        assert feature["evidence"], "the published row must CITE the stored evidence by address"
        assert store.guard().ok
        assert store.audit().overclaims is False

    def test_a_failing_session_does_not_claim_the_feature(self, tmp_path: Path):
        repo = MS.build_task_repo(tmp_path / "repo")
        root = Path(repo["repo"])
        LH.initialize_project(
            str(root),
            LH.ProjectProfile(name="shop", language="python", test_command="python -m pytest -q"),
            MS.task_features(repo),
            request=str(repo["request"]),
            project_id="shop",
            session="s0",
            git_runner=None,
        )
        store, _ = FL.LedgerStore.open(root)
        assert store is not None
        store.ensure_sealed(sealed_at="2026-10-06T00:00:00Z")
        MS._publish(
            store,
            MS.SessionOutcome(index=1, claimed_feature="receipts", status="verify_failed"),
        )
        document = store.document()
        assert document is not None
        feature = next(item for item in document["features"] if item["id"] == "receipts")
        assert feature["status"] != LH.PASSING_FEATURE_STATUS
        assert store.guard().ok, "and the ledger is still internally consistent"

class TestTheSuiteIsRegisteredAndLaned:
    """The suite has to be reachable from the single entry point, or it is a module.

    `evals.run` is the one evaluation entry point this repository has. A suite
    reachable only as `python -m evals.multi_session` is reachable by whoever
    remembers the name, which is not the same as reachable by CI.
    """

    def test_it_is_a_named_suite_of_the_shared_runner(self):
        from evals import run

        source = inspect.getsource(run.main)
        assert '"multi-session"' in source, (
            "multi-session must appear in evals.run --suite's choices; the module's own "
            "__main__ entry point is not a registration"
        )

    def test_the_suite_dispatch_rejects_another_suites_flags(self):
        """A suite-specific flag under the wrong suite is a usage error, not silence."""
        from evals import run

        for argv in (
            ["--suite", "multi-session", "--tasks", "x"],
            ["--suite", "multi-session", "--daily-cases", "x"],
            ["--suite", "multi-session", "--manual-evidence", "x"],
            ["--suite", "multi-session", "--no-docker"],
        ):
            with pytest.raises(SystemExit) as caught:
                run.main(argv)
            assert caught.value.code == 2, f"{argv} should be a usage error"

    def test_a_non_positive_repetition_count_is_refused(self):
        """Repetitions are the cost knob; zero would silently measure nothing."""
        from evals import run

        for repetitions in ("0", "-1"):
            with pytest.raises(SystemExit) as caught:
                run.main(["--suite", "multi-session", "--repetitions", repetitions])
            assert "at least 1" in str(caught.value)

    def test_an_unknown_arm_names_itself_instead_of_running_nothing(self):
        """A typo'd arm that quietly runs no arms would report an empty 'clean'."""
        from evals import run

        with pytest.raises(SystemExit) as caught:
            run.main(["--suite", "multi-session", "--arms", "typo-arm"])
        assert "typo-arm" in str(caught.value)

    def test_it_runs_in_the_nightly_lane_and_never_in_a_blocking_one(self):
        """Real sessions cost real runs, so a required PR check cannot afford them."""
        from evals import ci_lanes

        nightly = next(lane for lane in ci_lanes.LANES if lane.name == "nightly")
        assert any("evals.multi_session" in item for item in nightly.contains), (
            "the multi-session suite is absent from the nightly lane, which is the only "
            "lane whose budget admits several sequential sessions"
        )
        assert nightly.blocking is False

        for lane in ci_lanes.LANES:
            if lane.blocking:
                assert not any("evals.multi_session" in item for item in lane.contains), (
                    f"{lane.name} is a BLOCKING lane and must not carry the multi-session suite"
                )

    def test_the_store_pins_are_in_a_lane_and_not_left_on_the_floor(self):
        """A pin in no lane is a comment with a filename."""
        from evals import ci_lanes

        joined = " ".join(
            item for lane in ci_lanes.LANES for item in lane.contains
        )
        for name in (
            "tests/test_feature_ledger_store.py",
            "tests/test_ledger_store_structural_pins.py",
            "tests/test_p4_long_horizon_gaps.py",
        ):
            assert name in joined, f"{name} is in no lane at all"

    def test_the_middleware_source_pin_exists_and_is_promoted_to_a_lane(self):
        """Phase 4 asked for this pin; T3's module landed, so it can finally ship.

        It was withheld while no middleware existed, because pinning the absence
        of a file is not a pin. Now that ``runtime/turn_middleware.py`` is here
        the pin is real, and it belongs in a lane that runs on merges -- which is
        what makes it the net T3 needs to keep their own module honest.
        """
        project = PROJECT
        assert (project / "runtime" / "turn_middleware.py").exists(), (
            "the middleware module is gone again; the pin must be re-pointed, not left "
            "asserting an absence"
        )
        pin_file = project / "tests" / "test_ledger_store_structural_pins.py"
        pin_source = pin_file.read_text(encoding="utf-8")
        assert "TestAddingAMiddlewareDoesNotTouchTheLoop" in pin_source
        assert "turn_middleware" in pin_source

        from evals import ci_lanes

        joined = " ".join(item for lane in ci_lanes.LANES for item in lane.contains)
        assert "tests/test_ledger_store_structural_pins.py" in joined, (
            "the middleware source pin is written but in no lane, which makes it a comment"
        )

    def test_the_smoke_lane_withheld_the_promotions_for_a_stated_reason(self):
        """The decision to NOT promote is pinned too, so it cannot drift silently."""
        from evals import ci_lanes

        smoke = next(lane for lane in ci_lanes.LANES if lane.name == "smoke")
        joined = " ".join(smoke.contains)
        assert "test_ledger_store_structural_pins" not in joined
        assert "test_agent_loop_matrix" not in joined
        assert "already over" in smoke.note or "budget" in smoke.note.lower(), (
            "smoke must carry the reason these pins are not in it"
        )


class TestTheModuleDoesNotClaimMoreThanItDoes:
    """The claims in the docstrings are themselves under test.

    This module's one real dishonesty risk is not a wrong number -- it is a
    docstring that says "a session here is a fresh ``run_agent`` call with a real
    turn budget" while the function underneath writes one file. A reader skimming
    a docstring is exactly the reader a gate exists to protect, and the first
    version of this module made that claim.

    **P4/W2.3: four of the five tests here were REWRITTEN, and they fired on
    arrival -- which is the mechanism working, not a regression.** P4/W2 changed
    the session body from a direct ``path.write_text`` to real
    ``ToolRegistry.execute`` calls with an ENFORCED turn ceiling, so the claims
    changed and the pins demanding the old claims went red. Deleting them would
    have removed the only guard against the next version overclaiming; rewriting
    them points the same guard at the current boundary.

    Both directions still matter. A one-way check is satisfiable by deleting the
    docstring, so every test below asserts BOTH what the module may not claim and
    what it may not quietly drop.
    """

    @staticmethod
    def _module_doc() -> str:
        return (PROJECT / "evals" / "multi_session.py").read_text(encoding="utf-8")

    def test_it_does_not_advertise_a_run_agent_session_it_does_not_make(self):
        """The rule is unchanged; the boundary moved.

        The runner is a scripted, deterministic policy over the REAL tool path.
        It is NOT ``harness.agent_loop.run_agent``, and the docstring may not imply
        otherwise. Silences are accepted -- the rule is about what it ASSERTS --
        but an assertion of a real agent loop is refused.
        """
        doc = self._module_doc()
        advertisements = (
            "is a fresh ``harness.agent_loop.run_agent`` call",
            "is a fresh ``run_agent`` call",
            "a real `run_agent` session",
        )
        for phrase in advertisements:
            assert phrase not in doc, (
                f"the docstring advertises {phrase!r} while no code calls run_agent. If the "
                "runner now drives the real loop, say so here and update this pin; if it "
                "does not, the prose must not imply that it does."
            )

    def test_the_advertised_limit_is_the_documented_one(self):
        """The docstring must name the specific limit, not gesture at one.

        "Caveats may apply" is how a reader comes away believing the measurement
        was real. **The concrete, checkable sentence is the one that saves them**,
        and P4/W2.3 changed which sentence that is: the limit is now the scripted
        POLICY over the real tool path, and the module must say so in those words.
        """
        doc = self._module_doc()
        named = (
            "scripted, deterministic policy",
            "NOT** ``harness.agent_loop.run_agent``",
            "every model call is a scripted double",
            "scripted POLICY over a real tool path",
        )
        assert any(phrase.lower() in doc.lower() for phrase in named), (
            "the docstring no longer names the specific limit of the session body. The "
            "limit that must be named is that the POLICY is scripted while every attempt "
            "goes through the real product path -- and that this is not model quality."
        )

    def test_the_turn_budget_is_now_ENFORCED_and_the_docstring_says_so(self):
        """**Inverted in W2.3, deliberately, and the reason is the whole point.**

        The old pin required the docstring to say the turn budget was *recorded
        rather than enforced*, because it was not enforced. It now is -- a session
        that reaches its budget ends -- so requiring the old sentence would have
        demanded a false statement about the code.

        So this test asserts BOTH halves: the code enforces the ceiling, AND the
        docstring says it does. A module that enforces the budget while still
        claiming it does not would be understating a real guarantee, which is the
        same failure in the other direction.
        """
        import inspect

        from evals import multi_session as MS

        source = inspect.getsource(MS._bounded_session)
        assert 'state["turns"] >= budget' in source, (
            "the session body no longer bounds its turns. If the ceiling was removed, "
            "update this pin rather than deleting it: an unbounded session is the defect "
            "the one-feature-per-session discipline exists to prevent."
        )
        doc = self._module_doc().lower()
        assert "enforced" in doc, (
            "the turn ceiling is enforced in code but the docstring does not say so. "
            "Understating a real guarantee is the same failure as overstating one."
        )
        assert "not** enforced" not in doc and "is **not** enforced" not in doc, (
            "the docstring still says the turn budget is NOT enforced, which is now FALSE. "
            "A stale limit sentence is worse than no limit sentence."
        )

    def test_the_scripted_body_still_declares_itself_scripted(self):
        """The policy is scripted. That must never quietly stop being said.

        The counterweight to the pin above: a bounded session over a real tool
        path is *more* real than the one it replaced, and "more real" is exactly
        when a docstring drifts toward claiming more than it makes. So the
        scripted nature of the POLICY is pinned from the other side, in the code
        and in the report both.
        """
        doc = self._module_doc().lower()
        assert "scripted" in doc, (
            "the module stopped describing its session body as scripted. The POLICY is a "
            "fixture and the receipt says MODEL_QUALITY_MEASURED = False; a reader must "
            "not be able to read this module as a model-quality measurement."
        )
        assert not hasattr(__import__("evals.multi_session", fromlist=["x"]), "_scripted_session"), (
            "the old direct-write `_scripted_session` is still exported. It writes files "
            "itself, so the read-before-edit arm cannot reach it -- keep ONE session body "
            "or every number becomes ambiguous about which one produced it."
        )

    def test_the_discrimination_receipt_reports_the_consequence(self):
        """The receipt must still name which metrics separated and why.

        P4/W2.3 moved the arms from void to separating, so the split is no longer
        "none of them". What must not change is the receipt's JOB: it exists to
        stop a reader treating identical numbers as a comparison, and it does that
        by publishing both the varying metrics and whether the gap exceeds the
        arm's own noise.
        """
        from evals import multi_session as MS

        payload = MS.run_multi_session(
            ["baseline"], repetitions=1, gate=lambda task, target: False, out_root=""
        ).to_dict()
        discrimination = payload["discrimination"]
        assert "metrics_that_vary" in discrimination, (
            "the discrimination receipt stopped naming the varying metrics; without it a "
            "reader sees a column of numbers and infers a comparison that was not made"
        )
        assert "metrics_whose_gap_exceeds_their_own_noise" in discrimination, (
            "the receipt must compare the between-arm gap against the arm's own spread, or "
            "a one-token difference reads as a result"
        )
        assert "spread" in payload, (
            "the report stopped publishing variance. `Never report a single run per arm` "
            "is enforced by publishing the spread, not by repeating the number."
        )

    def test_the_spread_receipt_reports_real_observations_not_an_empty_list(self):
        """**The variance receipt shipped for a whole wave reporting `n: 0`.**

        `spread()` read its values off the repetition records, which carry their
        numbers under `per_task_metrics` rather than at the top level -- so every
        `record.get(name)` was `None` and every arm published
        `n: 0, values: [], no_variance: True`. An empty list has `distinct == 0`
        and `min`/`max` of `None`, so `widest_within_arm_spread` collapsed to
        `0.0` by `max(..., default=0.0)` and `exceeds_own_noise` was `True` for
        every metric that moved -- a separation measured against nothing.

        The three prior assertions only checked the KEYS existed. This one runs
        three repetitions of a real effort and checks that a repetition
        actually produced the numbers the receipt claims to summarise.
        """
        from evals import multi_session as MS

        repetitions = 3
        report = MS.run_multi_session(
            ["baseline"],
            repetitions=repetitions,
            gate=lambda task, target: False,
            out_root="",
        )
        payload = report.to_dict()
        arm = payload["arms"][0]
        observed = {
            name: row
            for name, rows in payload["spread"].items()
            for row in [rows[arm["arm"]]]
            if row["n"]
        }
        assert observed, (
            "every row of the spread receipt is n=0 across "
            f"{repetitions} repetitions, so the receipt is publishing statistics over an "
            "empty sample. 'no_variance' on zero observations is not evidence of stability."
        )
        assert any(row["n"] >= repetitions for row in observed.values()), (
            f"the spread receipt summarises fewer observations than the run performed "
            f"({repetitions}): { {k: v['n'] for k, v in observed.items()} }"
        )

    def test_an_unmeasured_noise_floor_cannot_be_exceeded(self):
        """Zero observations is not a noise floor of zero.

        `exceeds_own_noise` compared `across > widest`, and `widest` defaulted to
        `0.0` whenever no arm had two observations. That makes the verdict true
        for any metric with a non-zero between-arm gap -- a claim bounded by
        nothing. A single-repetition run is the smallest case that must fail
        closed rather than pass.
        """
        from evals import multi_session as MS

        report = MS.run_multi_session(
            ["baseline", "initializer"],
            repetitions=1,
            gate=lambda task, target: False,
            out_root="",
        )
        per_metric = report.discrimination()["per_metric"]
        assert per_metric, "the discrimination receipt is empty; there is nothing to check"
        for name, row in per_metric.items():
            if not row.get("noise_measured", False):
                assert row["exceeds_own_noise"] is False, (
                    f"{name}: exceeds_own_noise is True with noise_measured=False "
                    f"(widest={row['widest_within_arm_spread']!r}, gap={row['across_arm_gap']!r}). "
                    "A gap cannot exceed a floor that was never measured."
                )

    def test_the_isolation_null_names_its_own_workload_limit(self):
        """**The W2.3 null must not become a claim by being copied.**

        `subagent_isolation` compressed nothing because the child transcript was
        0.31% of its cap. A later reader who sees `isolation: exercised` and four
        identical metric values could easily write "isolation saves nothing". The
        receipt has to keep naming the workload as the reason, and this test is
        what stops that sentence from being deleted.
        """
        from evals import multi_session as MS

        payload = MS.run_multi_session(
            ["subagent_isolation"],
            repetitions=1,
            gate=lambda task, target: False,
            out_root="",
        ).to_dict()
        isolation = payload["isolation"]
        if isolation.get("isolation_exercised"):
            assert isolation.get("reason"), (
                "the isolation arm ran and compressed nothing, and the receipt no longer "
                "says why. An exercised-but-unstressed mechanism with no stated reason is "
                "indistinguishable from a mechanism that does not work."
            )
            assert isolation.get("headroom_used") is not None, (
                "the receipt must publish how much of the compression cap was actually used; "
                "that is the number separating 'no effect' from 'not stressed'"
            )

    def test_a_repetition_is_a_pass_over_every_task_not_over_one(self):
        """The meaning of `repetitions` must be published, not assumed.

        A repetition is one pass over EVERY task. A reader dividing repetitions by
        tasks divides by the wrong thing, and `spread()` -- the variance receipt
        that makes repetition worth anything -- reads the wrong rows if the two
        are confused.
        """
        from evals import multi_session as MS

        payload = MS.run_multi_session(
            ["baseline"], repetitions=1, gate=lambda task, target: False, out_root=""
        ).to_dict()
        assert "repetition_meaning" in payload, (
            "the report no longer states what one repetition is"
        )
        for arm in payload["arms"]:
            for record in arm["repetitions"]:
                assert record.get("tasks") == sorted(MS.TASK_SLUGS), (
                    f"a repetition covered {record.get('tasks')!r}, not every task; the "
                    "per-repetition metrics are an aggregate over tasks and a reader has to "
                    "know that"
                )
                assert "per_task_metrics" in record, (
                    "per-task values must ride beside the aggregate: a mean across two "
                    "tasks is a different quantity from either task's number"
                )
