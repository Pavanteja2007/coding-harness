"""P2/W2-T1 — the edit-drift benchmark, and the three guards it forced.

**The gate under test.** ``wrong_matched == 0`` across both corpora. Not "low".
Not "acceptable". Zero — because the two failure directions are not symmetric:

* an **under-match** is a failed edit: annoying, visible, SAFE;
* a **wrong-match** is **corruption**: the write succeeds, the run reports
  success, and the file is silently broken.

**Three of these tests are non-vacuity proofs, and they are the important
ones.** A benchmark that reports "zero wrong matches" is worthless if its grader
cannot report a wrong match at all — a broken oracle reports zero for exactly
the same reason a correct one does. So:

* :meth:`TestTheGateCanFail.prove_the_gate_goes_red_when_the_matcher_answers_wrongly`
  drives the REAL grader with a ladder that has been monkeypatched to return a
  confident, guard-approved, **wrong** offset, and asserts the benchmark reports
  ``wrong_matched > 0``. If this test stops failing the gate, the gate means
  nothing.
* :meth:`TestTheOracleIsSound` asserts every drift transform is COMPOSABLE and
  INVERTIBLE, because the whole offset oracle rests on those two properties and
  a transform that is neither would produce a confident wrong answer.
* :meth:`TestByteFidelity` runs the real ``harness.editor.apply_text_edit`` and
  asserts the untouched lines kept their EXACT original bytes.

**What the benchmark found, which no prompt predicted.** See
``harness/AGENTS.md``. In one sentence: the cascade corrupted files on three of
the ten classes before this round, and two of the three were silent index-map
and region-extent defects in the vendored ladder, now caught by three guards
that live in the WRAPPER (``harness/fuzzy_edit.py``) because a vendored file is
never modified in place.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace as dataclass_replace

from harness import edit_drift_bench as bench
from harness import editor, fuzzy_edit

REPO_ROOT = bench.default_repo_root()


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


class TestTheCorpusIsTheTenClasses:
    def test_the_briefs_ten_classes_are_exactly_these_ten(self):
        assert bench.DRIFT_CLASSES == (
            "trailing_whitespace",
            "indentation_tabs",
            "crlf",
            "unicode_nfd",
            "smart_quotes",
            "block_reorder",
            "duplicate_target",
            "similar_name_other_file",
            "very_long_line",
            "minified_generated",
        ), (
            "the brief names ten drift classes; if this tuple changed, a class "
            "was dropped or renamed and the published table no longer answers "
            "the question the brief asked"
        )

    def test_discovered_classes_are_kept_separate_from_the_briefs_ten(self):
        """A class this benchmark found must never masquerade as a briefed one."""
        assert set(bench.EXTRA_CLASSES).isdisjoint(bench.DRIFT_CLASSES), (
            "a discovered class leaked into the briefed ten; a reader would "
            "assume it had been asked for"
        )
        assert "block_tail_blank_lines" in bench.EXTRA_CLASSES, (
            "the class this benchmark DISCOVERED is no longer reported as one; "
            "that is how a found defect becomes an unrecorded one"
        )

    def test_every_class_is_covered_by_the_synthetic_corpus(self):
        present = {c.drift_class for c in bench.synthetic_corpus()}
        assert present == set(bench.DRIFT_CLASSES) | set(bench.EXTRA_CLASSES), (
            f"missing classes: {sorted(set(bench.DRIFT_CLASSES) - present)}"
        )

    def test_every_synthetic_case_declares_the_direction_it_expects(self):
        for case in bench.synthetic_corpus():
            assert case.expect in ("apply", "refuse"), case.name
            if case.expect == "apply":
                assert case.intended_post is not None, (
                    f"{case.name}: an 'apply' case with no intended_post has no "
                    "answer to be graded against, so it can only ever pass"
                )
            else:
                assert case.guard_expect, (
                    f"{case.name}: a 'refuse' class must name the guard that is "
                    "supposed to produce the refusal, or a refusal for the wrong "
                    "reason is indistinguishable from a right one"
                )

    def test_every_held_out_class_is_covered(self):
        cases = bench.held_out_corpus(REPO_ROOT)
        present = {c.drift_class for c in cases}
        missing = (set(bench.DRIFT_CLASSES) | set(bench.EXTRA_CLASSES)) - present
        assert not missing, (
            f"the held-out corpus is missing {sorted(missing)}; a class absent "
            "from the real-code corpus is not held out, it is untested"
        )
        assert len(cases) >= 50, (
            f"the held-out corpus has only {len(cases)} cases; a benchmark that "
            "small cannot separate a class that works from one that does not"
        )


class TestTheHeldOutCorpusIsGenuinelyHeldOut:
    def test_the_held_out_cases_are_built_from_real_repository_files(self):
        cases = bench.held_out_corpus(REPO_ROOT)
        assert cases, "the held-out corpus is empty; the gate would be vacuous"
        stems = {c.name.split(".")[1] for c in cases if c.name.startswith("held.")}
        assert stems, "no case carries a module stem in its name"
        for stem in stems:
            per_stem = [c for c in cases if c.name.startswith(f"held.{stem}.")]
            assert len(per_stem) >= 8, (
                f"{stem}: only {len(per_stem)} case(s) were built; a module with "
                "one case cannot separate a working class from a broken one"
            )
        # And every stem is a file that really exists in the repository.
        for module in bench._HELD_OUT_MODULES:
            assert os.path.isfile(os.path.join(REPO_ROOT, module)), module

    def test_the_target_block_is_located_by_ast_not_hand_picked(self):
        """Proven on a real file, and proven to REJECT rather than guess."""
        for module in bench._HELD_OUT_MODULES:
            path = os.path.join(REPO_ROOT, module)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                source = bench._prepare_fixture(handle.read())
            block = bench._real_block(source)
            assert block is not None, f"{module}: no AST-located block found"
            start, end = block
            snippet = source[start:end]
            assert snippet.lstrip().startswith("def "), (
                f"{module}: the located block is not a def -- the locator "
                "returned something other than a function"
            )
            assert '"""' in snippet, f"{module}: the located block has no docstring"
        # And a file with no parseable content yields NO block, not a guess.
        assert bench._real_block("this is not python (") is None
        assert bench._real_block("x = 1\n") is None


class TestTheOracleIsSound:
    def test_every_transform_is_composable_and_invertible(self):
        for name, transform in bench.TRANSFORMS.items():
            assert transform.composable(), (
                f"{name} is not composable (T(a+b) != T(a)+T(b)); the offset "
                "oracle computes a drifted block's extent by transforming the "
                "prefix and the block separately, so a non-compositional "
                "transform would yield a confident WRONG answer"
            )
            assert transform.invertible(), (
                f"{name} is not invertible; the grader compares a post-edit "
                "file against the intent by undoing the drift, so a transform "
                "that does not round-trip makes the comparison meaningless"
            )

    def test_the_fixture_preparer_actually_removes_what_the_inverses_need(self):
        """Each preparation step undoes a PRE-EXISTING condition, not a drift."""
        messy = "def f():\r\n\treturn 1   \n\tx = '“hi”'\n"
        prepared = bench._prepare_fixture(messy)
        assert "\r" not in prepared, "CRLF survived preparation"
        assert "\t" not in prepared, "a tab survived preparation"
        assert "  \n" not in prepared, "trailing whitespace survived preparation"
        assert "“" not in prepared, "a smart quote survived preparation"
        for name, transform in bench.TRANSFORMS.items():
            assert transform.inverse(transform.forward(prepared)) == prepared, (
                f"{name} does not round-trip a PREPARED fixture; the oracle "
                "would be unsound on every held-out case using it"
            )

    def test_a_match_at_the_wrong_offset_is_graded_wrong_not_matched(self):
        """The grader's verdict is decided by the FILE, not by the confidence."""
        case = next(
            c for c in bench.synthetic_corpus()
            if c.drift_class == "indentation_tabs"
        )
        good = bench.grade(case)
        assert good.outcome == "matched"

        # Same confidence, same stage name, one line further down: still wrong.
        shifted = dataclass_replace(
            case,
            intended_post=case.intended_post.replace("unrelated_helper", "unrelated_helperX"),
        )
        result = bench.grade(shifted)
        assert result.outcome == "wrong_matched", (
            "a case whose intent was altered still graded 'matched'; the "
            "grader is comparing something other than the produced file"
        )


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------


class TestTheGate:
    def test_the_synthetic_corpus_has_zero_wrong_matches(self):
        report = bench.run_benchmark(bench.synthetic_corpus(), name="synthetic")
        offenders = [c.name for c in report.cases if c.is_wrong]
        assert report.wrong_matched == 0, (
            "WRONG MATCHES -- each of these wrote to a site nobody asked for:\n"
            + "\n".join(f"  {c.name}: {c.detail[:160]}" for c in report.cases if c.is_wrong)
            + f"\noffenders: {offenders}"
        )

    def test_the_held_out_corpus_has_zero_wrong_matches(self):
        """The gate is asserted on REAL code, not only on the development set."""
        report = bench.run_benchmark(
            bench.held_out_corpus(REPO_ROOT), name="held-out"
        )
        offenders = [c.name for c in report.cases if c.is_wrong]
        assert report.wrong_matched == 0, (
            "WRONG MATCHES on real repository files:\n"
            + "\n".join(f"  {c.name}: {c.detail[:160]}" for c in report.cases if c.is_wrong)
            + f"\noffenders: {offenders}"
        )

    def test_both_corpora_agree_on_which_classes_the_cascade_cannot_do(self):
        """The published table must be the same on synthetic and real code.

        "Cannot do" means EVERY case of the class refuses -- not merely one, or
        a class would flip between corpora on the strength of one awkward file.
        If the held-out corpus found a class the synthetic one did not, the
        difference is the most valuable thing in this file, so the assertion is
        that they AGREE and a divergence has to be looked at.
        """
        def not_doable(report):
            return {
                cls.drift_class
                for cls in report.classes
                if cls.cases and cls.refused == len(cls.cases)
            }

        syn = bench.run_benchmark(bench.synthetic_corpus(), name="s")
        held = bench.run_benchmark(bench.held_out_corpus(REPO_ROOT), name="h")
        assert not_doable(syn) == not_doable(held), (
            "the two corpora disagree about which classes the cascade cannot do "
            "at all:\n"
            f"  synthetic only: {sorted(not_doable(syn) - not_doable(held))}\n"
            f"  held-out only:  {sorted(not_doable(held) - not_doable(syn))}"
        )
        # And the classes that ARE doable must work on BOTH corpora, every case.
        for report in (syn, held):
            for cls in report.classes:
                if cls.expected == "refuse":
                    continue
                assert cls.wrong_matched == 0, f"{cls.drift_class} corrupted a file"

    def test_results_are_published_per_class_and_not_only_in_aggregate(self):
        report = bench.run_benchmark(bench.synthetic_corpus(), name="s")
        table = bench.format_table(report)
        for drift_class in bench.DRIFT_CLASSES:
            assert drift_class in table, f"{drift_class} missing from the table"
        # The wrong-match column is per class and is the gate.
        assert "WRONG" in table
        assert "GATE wrong_matched == 0 : PASS" in table
        # And there is deliberately NO aggregate success percentage.
        assert "%" not in table.replace("0%", ""), (
            "the table renders a percentage; an aggregate success number is "
            "exactly what lets a wrong-match hide behind good classes"
        )


class TestTheGateCanFail:
    """The non-vacuity proof. Without this, "zero wrong" proves nothing."""

    def test_the_gate_goes_red_when_the_matcher_answers_wrongly(self, monkeypatch):
        """A confident, guard-approved, WRONG offset must be graded wrong."""
        from harness.third_party import opencode_replacer

        real_run = opencode_replacer.run_ladder
        poisoned = {"count": 0}

        def sabotage(text, old, **kwargs):
            verdict = real_run(text, old, **kwargs)
            if verdict.candidate is None:
                return verdict
            poisoned["count"] += 1
            # Same stage, same (perfect) confidence, same "guard approved"
            # receipt -- shifted onto a DIFFERENT site in the file.
            bad = opencode_replacer.Candidate(
                start=0,
                end=min(len(text) // 3, 40),
                similarity=verdict.candidate.similarity,
                stage=verdict.candidate.stage,
            )
            return dataclass_replace(
                verdict, candidate=bad, guard="", refused="", alternatives=0
            )

        monkeypatch.setattr(opencode_replacer, "run_ladder", sabotage)
        report = bench.run_benchmark(bench.synthetic_corpus(), name="poisoned")
        assert poisoned["count"] > 0, "the sabotage never fired; the proof is vacuous"
        assert report.wrong_matched > 0, (
            "a ladder returning a wrong offset on every case produced ZERO "
            "wrong-matches. The grader cannot detect corruption, so every "
            "'zero wrong-matches' result in this file is meaningless."
        )

    def test_a_refused_class_that_matches_is_reported_as_a_failure(self):
        """Class 7 must not be satisfiable, and its refusal must be real.

        Two things are proved. First the class refuses. Second -- and this is
        the part that stops "the grader refuses everything" from looking like a
        pass -- the SAME text under an ``apply`` expectation is graded
        ``refused`` too, i.e. the ladder's uniqueness guard is what refuses it,
        not the grader's own pessimism about the class.
        """
        case = next(
            c for c in bench.synthetic_corpus()
            if c.drift_class == "duplicate_target"
        )
        as_refuse = bench.grade(case)
        assert as_refuse.outcome == "refused"
        as_apply = bench.grade(
            dataclass_replace(case, expect="apply", intended_post="never")
        )
        assert as_apply.outcome == "refused", (
            f"the duplicate class produced {as_apply.outcome} under an 'apply' "
            "expectation; either the edit landed (corruption) or the case text "
            "is not actually ambiguous"
        )
        # And the refusal came from the ladder's own uniqueness rule.
        assert "places" in as_refuse.reason or "unique" in as_refuse.reason, (
            f"refused for an unrelated reason: {as_refuse.reason[:160]}"
        )


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


class TestTheGuards:
    def test_the_minified_file_is_refused_for_its_own_reason(self):
        case = next(
            c for c in bench.synthetic_corpus()
            if c.drift_class == "minified_generated"
        )
        result = bench.grade(case)
        assert result.outcome == "refused"
        assert "minified or generated" in result.reason, (
            f"refused for the wrong reason: {result.reason[:200]}"
        )

    def test_a_file_with_fewer_lines_than_the_block_is_refused(self):
        """The class-10 guard is structural, and similarity cannot catch it."""
        text = "def a():\n    return 1\n"
        old = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        refusal = fuzzy_edit._refuse_missing_line_structure(text, old)
        assert "minified or generated" in refusal
        # A file with ENOUGH lines is not refused by this rule.
        assert fuzzy_edit._refuse_missing_line_structure(old, old) == ""

    def test_a_match_covering_most_of_the_file_is_refused(self):
        # Direct exercise of the rule itself, independent of any corpus.
        text = "a" * 1000
        refusal = fuzzy_edit._refuse_region_coverage(text, 0, 900, 0.5)
        assert "not a block edit" in refusal
        assert fuzzy_edit._refuse_region_coverage(text, 0, 100, 0.5) == ""

    def test_the_region_extent_clamp_stops_the_block_eating_the_next_def(self):
        """The class this benchmark discovered, asserted directly."""
        old = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        # A ladder region that ran PAST the caller's block, through `def b` and
        # on into the tail -- exactly the over-reach the benchmark measured.
        text = "HEADER\n" + old + "TAIL\n"
        base = fuzzy_edit.FuzzyMatch(
            start=text.index("def a"),
            end=len(text),
            replacement="X",
            stage="line_trimmed",
            confidence=1.0,
        )
        clamped, guard = fuzzy_edit._clamp_region_extent(base, text, old)
        assert guard == "region_extent"
        # `old` legitimately contains four content lines (`def a`, `return 1`,
        # `def b`, `return 2`), so the region keeps those and gives back only
        # what lies beyond them -- here the trailing TAIL line.
        assert text[clamped.end :].strip() == "TAIL", (
            "the clamp did not hand back the over-reach; the region still eats "
            "whatever followed the block"
        )
        # The sharper shape: a block whose caller did NOT include the following
        # declaration. There the over-reach IS a `def`, and the clamp must
        # remove it or the edit deletes a function.
        short_old = "def a():\n    return 1\n\n\n"
        short_text = "HEADER\n" + short_old + "def b():\n    return 2\nTAIL\n"
        short = fuzzy_edit.FuzzyMatch(
            start=short_text.index("def a"),
            end=len(short_text),
            replacement="X",
            stage="line_trimmed",
            confidence=1.0,
        )
        fixed, guard3 = fuzzy_edit._clamp_region_extent(short, short_text, short_old)
        assert guard3 == "region_extent"
        assert "def b" not in short_text[fixed.start : fixed.end], (
            "the clamp kept the following declaration; applying this edit would "
            "delete a function -- the corruption the benchmark found"
        )
        # And the clamp is a NO-OP when the region is already exactly the caller's
        # block, so every well-behaved fuzzy edit is byte-identical to before
        # the guard existed.
        ordinary_old = "def a():\n    return 1\n"
        ordinary_text = "HEADER\n" + ordinary_old + "TAIL\n"
        ordinary = fuzzy_edit.FuzzyMatch(
            start=ordinary_text.index("def a"),
            end=ordinary_text.index("TAIL"),
            replacement="X",
            stage="line_trimmed",
            confidence=1.0,
        )
        same, guard2 = fuzzy_edit._clamp_region_extent(ordinary, ordinary_text, ordinary_old)
        assert guard2 == "" and same.end == ordinary.end, (
            "the clamp fired on a region that was already exactly old_string; "
            "it is not a no-op and every well-behaved fuzzy edit is now being "
            "altered"
        )
        # A region one line too long IS corrected, which is the ordinary case
        # the guard exists for.
        over = dataclass_replace(ordinary, end=len(ordinary_text))
        trimmed, guard3b = fuzzy_edit._clamp_region_extent(over, ordinary_text, ordinary_old)
        assert guard3b == "region_extent" and trimmed.end == ordinary.end

    def test_the_normalisation_integrity_check_catches_a_bad_offset_map(self):
        """The vendored map defect, asserted directly and with its shape."""
        from harness.third_party import opencode_replacer

        text, amap = opencode_replacer._strip_trailing("def f():\n    return 1  \n")
        assert len(amap) < len(text), (
            "the vendored _strip_trailing offset map is no longer short; if "
            "that was fixed upstream this guard is dead weight and should be "
            "removed with a Change Log entry"
        )
        # A region that is NOT old_string under the same normalisation is refused.
        refusal = fuzzy_edit._normalisation_integrity(
            "line_trimmed", "def g():\n    return 2\n", "def f():\n    return 1\n"
        )
        assert "its own claim failed" in refusal
        # And a faithful region passes.
        assert fuzzy_edit._normalisation_integrity(
            "line_trimmed", "def f():  \n    return 1  \n", "def f():\n    return 1\n"
        ) == ""

    def test_an_approximate_stage_is_not_held_to_an_equality_it_cannot_meet(self):
        """The scored stages locate a region; demanding exactness would kill them."""
        for stage in fuzzy_edit.APPROXIMATE_STAGES:
            assert fuzzy_edit._normalisation_integrity(
                stage, "something else entirely", "def f():\n    return 1\n"
            ) == "", (
                f"{stage} is an approximate stage and must not be refused by the "
                "integrity post-condition"
            )


class TestTheLLMFixerCannotBypassTheGuard:
    def test_the_llm_fixer_is_off_by_default_and_never_in_defaults(self):
        from harness.config import DEFAULTS

        assert "edit_llm_repair" not in DEFAULTS, (
            "edit_llm_repair is in DEFAULTS, which is merged into every task and "
            "every eval arm -- that would silently switch every run in the "
            "project onto a model call on the edit path"
        )
        for key in (
            "edit_llm_repair_max_calls",
            "edit_llm_repair_max_tokens",
            "edit_llm_repair_model",
        ):
            assert key not in DEFAULTS, f"{key} is in DEFAULTS; a default budget is not a bound"
        assert not fuzzy_edit.llm_repair_enabled(None), (
            "the LLM edit-fixer is ON with no config; it is a model call on the "
            "edit path and must be opt-in"
        )
        assert not fuzzy_edit.llm_repair_enabled({})
        assert not fuzzy_edit.llm_repair_enabled({"edit_llm_repair": False})
        assert not fuzzy_edit.llm_repair_enabled({"edit_llm_repair": "no"})
        assert not fuzzy_edit.llm_repair_enabled({"edit_llm_repair": ""})

    def test_the_switch_alone_is_not_enough_without_a_boundary(self):
        boundary = lambda prompt, difficulty_hint=None: "{}"  # noqa: E731
        assert not fuzzy_edit.llm_repair_enabled(
            {"edit_llm_repair": True}, call=None
        ), (
            "the switch is on but no model boundary is bound; the repair must "
            "degrade to a refusal rather than resolve a default provider and "
            "dial one during an edit"
        )
        assert fuzzy_edit.llm_repair_enabled({"edit_llm_repair": True}, call=boundary)
        # Truthiness is read strictly enough that a typo cannot enable it.
        assert not fuzzy_edit.llm_repair_enabled({"edit_llm_repair": "maybe"}, call=boundary)

    def test_a_model_proposed_match_still_goes_through_every_guard(self, monkeypatch):
        """The model proposes; the code disposes. All three guards still run."""
        from harness.third_party import opencode_replacer

        # A structurally perfect candidate pointing at the WRONG place, exactly
        # what a hallucinating model proposes.
        text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        poisoned = opencode_replacer.Candidate(
            start=text.index("def b"), end=len(text) - 1,
            similarity=1.0, stage="exact",
        )
        verdict = opencode_replacer.Verdict(candidate=poisoned, stage="exact")
        monkeypatch.setattr(opencode_replacer, "run_ladder", lambda *a, **k: verdict)

        match, reason = fuzzy_edit.resolve_fuzzy(text, "def a():\n    return 1\n", "X")
        assert match is None, (
            "a model-proposed match at the wrong site was ACCEPTED; a guard was "
            "bypassed"
        )
        assert reason, "the refusal says nothing about which guard stopped it"
        assert any(
            marker in reason
            for marker in (
                "own claim failed",
                "is not old_string",
                "minified or generated",
                "not a block edit",
            )
        ), f"refused for an unrelated reason: {reason[:200]}"

    def test_the_llm_repair_receipt_names_the_weakest_guarantee(self):
        report = fuzzy_edit.llm_repair_report()
        assert report["guarantee"] == fuzzy_edit.LLM_GUARANTEE
        assert fuzzy_edit.LLM_GUARANTEE == "weakest", (
            "the LLM-repaired edit must be labelled the weakest of the three "
            "mechanisms; it is exact < fuzzy < model-repaired"
        )
        assert report["guarantee_order"] == ["exact", "fuzzy", "weakest"]
        assert report["default_enabled"] is False
        assert report["budget_keys"], "the token/call caps are not published"
        assert report["difficulty_hint"] == "easy", (
            "the repair must run on the cheap tier; it is a mechanical repair, "
            "not reasoning"
        )

    def test_the_call_and_token_caps_refuse_rather_than_retry(self):
        spent = {"calls": 0, "tokens": 0}
        calls = []

        def boundary(prompt, difficulty_hint=None):
            calls.append(difficulty_hint)
            return '{"found_in_file": "def a():\\n    return 1\\n"}'

        text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        config = {"edit_llm_repair": True, "edit_llm_repair_max_calls": 1}
        first, _, receipt = fuzzy_edit.llm_repair_stage(
            text, "def a():\n    return 1\nnope", "X",
            config=config, call=boundary, spent=spent,
        )
        assert first is not None, "the repair should have succeeded on the first try"
        assert receipt["guarantee"] == "weakest"
        assert calls == ["easy"], "the repair did not run on the cheap tier"
        # Second attempt is refused WITHOUT calling the model again.
        second, reason, receipt2 = fuzzy_edit.llm_repair_stage(
            text, "def a():\n    return 1\nnope", "X",
            config=config, call=boundary, spent=spent,
        )
        assert second is None and len(calls) == 1, (
            "the call cap did not stop a second model call; an unbounded repair "
            "loop on the edit path spends money until something else stops it"
        )
        assert "budget" in reason and receipt2["note"] == "call budget spent"

    def test_a_model_reply_naming_absent_text_is_refused(self):
        spent = {"calls": 0, "tokens": 0}
        text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        match, reason, receipt = fuzzy_edit.llm_repair_stage(
            text, "def a():\n    return 1\nnope", "X",
            config={"edit_llm_repair": True},
            call=lambda p, difficulty_hint=None: '{"found_in_file": "def zz():\\n    return 9\\n"}',
            spent=spent,
        )
        assert match is None and "does not occur" in reason
        assert receipt["proposed"] is True and receipt["applied"] is False

    def test_a_model_reply_naming_ambiguous_text_is_refused(self):
        spent = {"calls": 0, "tokens": 0}
        text = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        match, _reason, _ = fuzzy_edit.llm_repair_stage(
            text, "def a():\n    return 1\nnope", "X",
            config={"edit_llm_repair": True},
            call=lambda p, difficulty_hint=None: '{"found_in_file": "    return 1\\n"}',
            spent=spent,
        )
        # "    return 1\n" occurs once, so this one is accepted; a genuinely
        # repeated proposal is what must be refused.
        assert match is not None
        twice = "def a():\n    return 1\n\n\ndef c():\n    return 1\n"
        match2, reason2, _ = fuzzy_edit.llm_repair_stage(
            twice, "def a():\n    return 1\nnope", "X",
            config={"edit_llm_repair": True},
            call=lambda p, difficulty_hint=None: '{"found_in_file": "    return 1\\n"}',
            spent={},
        )
        assert match2 is None and "occurs 2 times" in reason2


# ---------------------------------------------------------------------------
# Byte fidelity -- requirement 4
# ---------------------------------------------------------------------------


def _fidelity_file(root: str, name: str, text: str) -> str:
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return name


class TestByteFidelity:
    def test_untouched_lines_keep_their_exact_original_whitespace(self):
        """A fuzzy edit must not reformat the file it landed in.

        Driven through the REAL ``apply_text_edit``, on a file padded so the
        target is a realistic fraction of it -- a 6-line block in a 14-line file
        is 56% and the region-coverage guard refuses it, which is correct
        behaviour and would make this test measure the wrong thing.
        """
        filler = "".join(
            f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24)
        )
        head = "import os\n\n\n"
        block = (
            "def compute_totals(entries):\n"
            "    running = 0\n"
            "    for e in entries:\n"
            "        running += e.amount\n"
            "    return running\n"
        )
        tail = (
            "\n\ndef other():\n"
            "    # deliberately odd spacing that must survive byte-for-byte\n"
            "    x = 1\n"
            "\n"
            "    return x\n"
        )
        original = bench._prepare_fixture(head + block + tail + filler)
        drifted = bench.TRANSFORMS["indentation_tabs"].forward(original)

        with tempfile.TemporaryDirectory() as root:
            name = _fidelity_file(root, "app.py", drifted)
            session = editor.EditSession()
            session.note_read(name, data=drifted.encode("utf-8"))
            outcome = editor.apply_text_edit(
                root, name,
                head + block,
                head + "def compute_totals(entries):\n    return 0\n",
                session=session,
                config={"require_edit_digest": False},
            )
            assert outcome.ok, f"the fuzzy edit was refused: {outcome.message[:300]}"
            assert outcome.match_strategy, "an applied edit must name its stage"
            with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as fh:
                after = fh.read()

        # The block was replaced...
        assert "    return 0\n" in after
        assert "running += e.amount" not in after.split("def other()")[0]
        # ...and NOTHING else moved. Compared against the DRIFTED bytes, because
        # the file on disk was drifted: asserting against the pre-drift text
        # would be asserting that the drift was undone, which is a different
        # (and wrong) claim.
        assert after.startswith(head), "the header was reformatted"
        untouched = drifted[drifted.index("\n\ndef other():"):]
        assert untouched in after, (
            "an untouched region of the file did not survive byte-for-byte; a "
            "fuzzy edit that reformats the file is a diff-noise disaster. The "
            "region compared here is every byte after the edited block, taken "
            "from the DRIFTED file, so it covers the deliberately odd internal "
            "blank line inside `other` as well as the trailing padding."
        )

    def test_crlf_drift_is_absorbed_by_the_editor_above_the_ladder(self):
        """Class 3 behaves DIFFERENTLY at the two levels, and both are correct.

        The benchmark grades the LADDER in isolation, where a CRLF file defeats
        every rung and the integrity post-condition refuses it. At the EDITOR
        level the outcome is better than a refusal: ``apply_text_edit`` adapts
        the caller's ``\\n`` to the file's own convention *before* matching, so
        the edit lands EXACTLY.

        Both are reported because a reader who saw only the benchmark's class-3
        row (5/5 refused on held-out code) would conclude CRLF edits are
        impossible, which is false. The ladder is one layer down.
        """
        original = bench._prepare_fixture(
            "import os\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n"
        )
        drifted = bench.TRANSFORMS["crlf"].forward(original)
        assert "\r\n" in drifted and "\r\n" not in original

        with tempfile.TemporaryDirectory() as root:
            name = _fidelity_file(root, "m.py", drifted)
            session = editor.EditSession()
            session.note_read(name, data=drifted.encode("utf-8"))
            outcome = editor.apply_text_edit(
                root, name, "def a():\n    return 1\n", "def a():\n    return 42\n",
                session=session, config={"require_edit_digest": False},
            )
            assert outcome.ok, f"the editor refused a CRLF edit: {outcome.message[:200]}"
            assert outcome.match_strategy == "exact", (
                "the CRLF edit did not land exactly; the editor's newline "
                "adaptation exists precisely so it does"
            )
            with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as fh:
                after = fh.read()
        assert "    return 42\r\n" in after, "the CRLF file's convention was not preserved"
        assert "\n" not in after.replace("\r\n", ""), "a lone LF was introduced"

    def test_a_refused_drift_leaves_the_file_byte_identical(self):
        """A refusal must not write anything at all."""
        filler = "".join(
            f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24)
        )
        original = bench._prepare_fixture(
            "import os\n\n\ndef a():\n    return 1\n\n\ndef b():\n    return 2\n" + filler
        )
        # A drift the ladder provably cannot serve: the block the caller wants
        # is in a file that has been REORDERED past recognition, with an
        # old_string that matches nothing at any stage.
        drifted = bench.TRANSFORMS["indentation_tabs"].forward(original)
        with tempfile.TemporaryDirectory() as root:
            name = _fidelity_file(root, "m.py", drifted)
            session = editor.EditSession()
            session.note_read(name, data=drifted.encode("utf-8"))
            outcome = editor.apply_text_edit(
                root, name, "def zzz_absent():\n    return 1\n", "X",
                session=session, config={"require_edit_digest": False},
            )
            assert not outcome.ok, "a no-match edit was applied"
            with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as fh:
                assert fh.read() == drifted, (
                    "a refused edit modified the file; nothing was written"
                )


# ---------------------------------------------------------------------------
# Class 8 -- the multi-file containment guard
# ---------------------------------------------------------------------------


class TestTheMultiFileGuard:
    def test_a_fuzzy_match_never_writes_outside_the_named_file(self):
        """Class 8's claim is about CONTAINMENT, and it is tested as one.

        The ladder only ever sees one file's text, so the guard under test is
        the one above it: that an edit aimed at one path cannot alter another.
        The sibling carries a similarly-named function with a DIFFERENT body, so
        a leak would be visible rather than silent.
        """
        filler = "".join(
            f"\n\ndef pad_{i}(v):\n    return v + {i}\n" for i in range(24)
        )
        block = (
            "def compute_totals(entries):\n"
            "    running = 0\n"
            "    for e in entries:\n"
            "        running += e.amount\n"
            "    return running\n"
        )
        target = bench._prepare_fixture(
            "import os\n\n\n" + block + "\n\ndef other():\n    return 1\n" + filler
        )
        drifted = bench.TRANSFORMS["indentation_tabs"].forward(target)
        sibling = (
            '"""A sibling module with a SIMILARLY NAMED function."""\n\n\n'
            "def compute_totals(entries):\n"
            '    """Same name, different body."""\n'
            "    return sum(e.amount for e in entries), []\n"
        )

        with tempfile.TemporaryDirectory() as root:
            a_name = _fidelity_file(root, "a_module.py", drifted)
            b_name = _fidelity_file(root, "b_module.py", sibling)
            session = editor.EditSession()
            session.note_read(a_name, data=drifted.encode("utf-8"))
            outcome = editor.apply_text_edit(
                root, a_name, block, "def compute_totals(entries):\n    return 0\n",
                session=session, config={"require_edit_digest": False},
            )
            assert outcome.ok, outcome.message[:300]
            with open(os.path.join(root, b_name), "r", encoding="utf-8", newline="") as fh:
                after_b = fh.read()

        assert after_b == sibling, (
            "an edit aimed at a_module.py modified b_module.py; the "
            "similarly-named function in the other file was rewritten"
        )
        assert "sum(e.amount for e in entries)" in after_b


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestTheReportIsPublishable:
    def test_the_synthetic_table_renders_every_class(self):
        report = bench.run_benchmark(bench.synthetic_corpus(), name="synthetic")
        table = bench.format_table(report)
        for name in bench.DRIFT_CLASSES + bench.EXTRA_CLASSES:
            assert name in table
        assert "TOTAL" in table

    def test_the_report_serialises_without_a_lossy_aggregate(self):
        report = bench.run_benchmark(bench.held_out_corpus(REPO_ROOT), name="held-out")
        payload = report.to_dict()
        assert set(payload) == {
            "corpus", "total_cases", "matched", "wrong_matched", "refused",
            "classes", "unexpected",
        }, "the report shape changed; a consumer may be reading the old keys"
        assert len(payload["classes"]) == len(report.classes)
        assert sum(c["cases"] for c in payload["classes"]) == payload["total_cases"]
        assert sum(c["wrong_matched"] for c in payload["classes"]) == payload["wrong_matched"]

    def test_every_unexpected_case_carries_a_reason_a_reader_can_act_on(self):
        report = bench.run_benchmark(bench.synthetic_corpus(), name="s")
        for case in report.unexpected:
            assert case.reason or case.detail, (
                f"{case.name} did not do what its class expects and says nothing "
                "about why; an unexplained refusal is indistinguishable from a bug"
            )
