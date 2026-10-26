"""P2/W2 T5.W2.2 - the policy language's self-validation is real.

A policy language whose rules carry their own examples is only worth anything
if the examples are EXECUTED and a broken rule is REFUSED. This file proves
both, and it proves the layering is a security property rather than a
formatting preference.

Three tasks, three classes:

* **Task A - it fails closed.** Three deliberately broken rules, each of which
  must refuse to load with an error naming the rule and the failing example.
  A fourth pins the floor's deny-only invariant.
* **Task B - the examples are a real suite.** Every shipped rule's examples run
  as a suite and the totals are reported. A rule shipping with ZERO examples is
  a finding: a rule with no proof is a rule nobody checked.
* **Task C - layering is order-dependent.** The SAME three rules in four
  different layer orders produce four different answers, and the floor beats
  all of them in every order.

Transcripts: ``python Temp/opencode/w2_2_transcripts.py``.
"""

from __future__ import annotations

import ast

import pytest

from shared import policy_lang as pl
from shared.policy_lang import (
    LAYERS,
    PolicyProgram,
    PolicyRule,
    PolicyValidationError,
    validate_program,
)

# ---------------------------------------------------------------------------
# TASK A -- three broken rules, all three must refuse to load
# ---------------------------------------------------------------------------


def _load(rules) -> PolicyProgram:
    return validate_program(PolicyProgram(rules=tuple(rules)))


class TestThePolicyLanguageFailsClosed:
    def test_a_rule_whose_match_example_does_NOT_match_is_refused(self) -> None:
        """Broken rule 1: the declared `match` example does not match.

        The example PARSES (`command=git` is a well-formed subject) and the
        rule's own pattern does not reach it, so the failure is semantic. An
        earlier draft of this transcript used `command=git status`, which was
        refused by the example GRAMMER for a bare token - a refusal for the
        wrong reason, pointing the reader at the wrong defect.
        """
        rule = PolicyRule(
            name="w2_broken_match",
            decision="deny",
            command_prefix="rm -rf /tmp",
            match=("command=git",),
        )
        with pytest.raises(PolicyValidationError) as excinfo:
            _load([rule])
        exc = excinfo.value
        assert exc.rule == "w2_broken_match"
        assert exc.example == "command=git"
        assert exc.layer == "defaults"
        assert "does not behave as declared" in str(exc)
        assert "deciding rule" in str(exc), (
            "the message must say the rule did not DECIDE, not merely that the "
            "answer differed - otherwise a reader cannot tell a broken example "
            "from a rule that was shadowed."
        )

    def test_a_rule_whose_not_match_example_DOES_match_is_refused(self) -> None:
        """Broken rule 2: the declared `not_match` example matches.

        `not_match` means "THIS RULE DOES NOT APPLY to this subject". It is
        deliberately not "the answer differs" - two different rules can
        legitimately reach the same decision, and a `not_match` demanding a
        different ANSWER would fail on a perfectly consistent policy.
        """
        rule = PolicyRule(
            name="w2_broken_not_match",
            decision="deny",
            tool="shell",
            match=("tool=shell",),
            not_match=("tool=shell",),
        )
        with pytest.raises(PolicyValidationError) as excinfo:
            _load([rule])
        exc = excinfo.value
        assert exc.rule == "w2_broken_not_match"
        assert exc.example == "tool=shell"
        assert "MATCHES this subject" in str(exc)

    def test_a_malformed_decision_is_refused_at_construction(self) -> None:
        """Broken rule 3: `decision='forbiddenish'`.

        Refused rather than coerced. A rule whose decision silently became
        `ask` would turn an author's "forbid this" into a question; one that
        became `allow` would be worse than no rule at all.
        """
        with pytest.raises(PolicyValidationError) as excinfo:
            PolicyRule(name="w2_bad_decision", decision="forbiddenish")
        exc = excinfo.value
        assert exc.rule == "w2_bad_decision"
        assert "unsupported policy decision" in str(exc)
        assert "forbiddenish" in str(exc)
        # The message must LIST the accepted vocabulary, so the fix is
        # readable without reading the source.
        for word in ("allow", "ask", "deny"):
            assert word in str(exc)

    def test_the_floor_refuses_to_carry_anything_but_a_denial(self) -> None:
        """A floor `allow` is a contradiction; a floor `ask` can be talked past."""
        for decision in ("allow", "ask"):
            with pytest.raises(PolicyValidationError) as excinfo:
                PolicyRule(name="w2_floor", decision=decision, layer=pl.FLOOR_LAYER)
            assert "only carries denials" in str(excinfo.value)

    def test_a_rule_with_NO_examples_is_refused(self) -> None:
        """The Codex model is "executable test cases inside the policy file"."""
        rule = PolicyRule(name="w2_no_examples", decision="deny", tool="shell")
        assert rule.has_examples is False
        with pytest.raises(PolicyValidationError) as excinfo:
            _load([rule])
        assert "declares no examples" in str(excinfo.value)
        assert excinfo.value.rule == "w2_no_examples"

    def test_an_unknown_layer_is_refused_and_names_the_known_layers(self) -> None:
        with pytest.raises(PolicyValidationError) as excinfo:
            PolicyRule(
                name="w2_bad_layer", decision="deny", layer="yesterday", match=("*",)
            )
        message = str(excinfo.value)
        assert "unknown layer" in message
        for layer in LAYERS:
            assert layer in message

    def test_a_duplicate_rule_name_is_refused(self) -> None:
        """A refusal names ONE rule; two rules with one name make that a guess."""
        one = PolicyRule(name="w2_dup", decision="deny", tool="shell", match=("tool=shell",))
        with pytest.raises(PolicyValidationError) as excinfo:
            PolicyProgram(rules=(one, one))
        assert "duplicate rule name" in str(excinfo.value)

    def test_the_validation_error_carries_a_machine_readable_record(self) -> None:
        """A receipt a gate can publish, not only a sentence a human reads."""
        rule = PolicyRule(
            name="w2_recorded", decision="deny", tool="shell", match=("tool=read",)
        )
        with pytest.raises(PolicyValidationError) as excinfo:
            _load([rule])
        record = excinfo.value.as_dict()
        assert set(record) == {"message", "rule", "layer", "example"}
        assert record["rule"] == "w2_recorded"
        assert record["example"] == "tool=read"


class TestTheShippedPolicyLoadsAndItsExamplesAreASuite:
    def test_the_shipped_policy_validates(self) -> None:
        program = pl.shipped_policy()
        assert program.validated is True
        assert program.rules, "the shipped policy has no rules at all"

    def test_every_shipped_rule_ships_at_least_one_example(self) -> None:
        """A rule with ZERO examples is a FINDING and is reported here."""
        report = pl.shipped_policy().report()
        zero = [row["name"] for row in report["rules"] if row["examples"] == 0]
        assert not zero, (
            f"rules shipping with NO examples: {zero}. A rule nobody has "
            f"demonstrated is a rule nobody has checked, and this mechanism "
            f"exists so that is impossible rather than merely discouraged."
        )

    def test_every_shipped_rules_example_passes(self) -> None:
        report = pl.shipped_policy().report()
        failing = [row["name"] for row in report["rules"] if not row["ok"]]
        assert not failing, f"shipped rules whose own examples fail: {failing}"

    def test_the_suite_has_a_reportable_total(self) -> None:
        """Task B's deliverable: real numbers, not 'it validated'."""
        report = pl.shipped_policy().report()
        total = sum(row["examples"] for row in report["rules"])
        assert total > 0
        assert report["rule_count"] == len(report["rules"])
        # Every rule contributes at least its `match` example.
        assert total >= report["rule_count"]

    def test_specificity_is_reported_but_never_used_for_resolution(self) -> None:
        """A specificity score would be a DIFFERENT policy, and this one
        deliberately does not implement it. An unused property is one edit away
        from being used, so the non-use is pinned."""
        broad = PolicyRule(
            name="w2_broad", decision="ask", layer="defaults", match=("tool=shell",)
        )
        narrow = PolicyRule(
            name="w2_narrow",
            decision="allow",
            layer="defaults",
            tool="shell",
            command_prefix="pytest",
            match=("tool=shell",),
        )
        assert narrow.specificity > broad.specificity
        program = PolicyProgram(rules=(narrow, broad))
        outcome = program.evaluate({"tool": "shell", "command": "pytest tests/"})
        # LAST match wins, so the BROADER rule declared later decides, even
        # though the narrower one has the higher specificity score.
        assert outcome.rule == "w2_broad"
        assert outcome.decision == "ask"

    def test_the_undecided_default_is_ask_not_allow(self) -> None:
        """An action no rule spoke about is a question, not a grant."""
        assert pl.DEFAULT_ACTION == "ask"
        program = PolicyProgram(rules=())
        assert program.evaluate({"tool": "unheard-of"}).decision == "ask"


# ---------------------------------------------------------------------------
# TASK C -- layering is a security property
# ---------------------------------------------------------------------------


def _three_layer_program(order) -> PolicyProgram:
    """The SAME three rules, evaluated under a caller-chosen layer order."""
    return PolicyProgram(
        rules=(
            PolicyRule(
                name="l_defaults", decision="ask", layer="defaults",
                tool="shell", match=("tool=shell",),
            ),
            PolicyRule(
                name="l_user", decision="allow", layer="user_config",
                tool="shell", match=("tool=shell",),
            ),
            PolicyRule(
                name="l_session", decision="deny", layer="per_session",
                tool="shell", match=("tool=shell",),
            ),
        ),
        layers=order,
    )


class TestLayerOrderIsASecurityProperty:
    def test_the_documented_order_is_what_the_module_declares(self) -> None:
        assert LAYERS == (
            "floor",
            "defaults",
            "builtin_agent",
            "user_config",
            "per_agent",
            "per_session",
        )

    def test_the_documented_order_lets_a_session_grant_beat_the_defaults(self) -> None:
        outcome = _three_layer_program(LAYERS).evaluate(
            {"tool": "shell", "command": "pytest tests/"}
        )
        assert (outcome.decision, outcome.rule) == ("deny", "l_session")

    def test_REORDERING_the_layers_changes_the_outcome(self) -> None:
        """The load-bearing proof.

        Four orders, the same three rules, the same three decisions. Only the
        ORDER differs and the answer differs. That is what makes layer order a
        security property and not a documentation convention: an
        implementation that ignored the order would still pass every other
        test in this file.
        """
        orders = {
            "as documented": LAYERS,
            "user above session": ("defaults", "user_config", "per_session"),
            "session first": ("per_session", "user_config", "defaults"),
            "session between": ("defaults", "per_session", "user_config"),
        }
        decisions = {
            name: _three_layer_program(order).evaluate({"tool": "shell"}).decision
            for name, order in orders.items()
        }
        assert len(set(decisions.values())) == 3, (
            f"expected three DISTINCT decisions across the four orders, got "
            f"{decisions}. If reordering changed nothing, layer order would not "
            f"be a security property."
        )
        assert set(decisions.values()) == {"allow", "ask", "deny"}
        # And the documented order is the one that lets the most specific,
        # most operator-controlled layer win.
        assert decisions["as documented"] == "deny"
        assert decisions["session first"] == "ask"
        assert decisions["session between"] == "allow"

    def test_last_match_wins_within_one_layer(self) -> None:
        program = PolicyProgram(
            rules=(
                PolicyRule(name="first", decision="allow", tool="shell",
                           match=("tool=shell",)),
                PolicyRule(name="second", decision="deny", tool="shell",
                           match=("tool=shell",)),
            )
        )
        outcome = program.evaluate({"tool": "shell"})
        assert outcome.rule == "second"
        assert outcome.decision == "deny"

        reversed_program = PolicyProgram(rules=tuple(reversed(program.rules)))
        assert reversed_program.evaluate({"tool": "shell"}).rule == "first"

    def test_a_floor_refusal_beats_every_layer_in_every_order(self) -> None:
        floor_rule = PolicyRule(
            name="l_floor",
            decision="deny",
            layer=pl.FLOOR_LAYER,
            screen=lambda command: command == "pytest tests/",
            match=("command=pytest tests/",),
        )
        allow_rule = PolicyRule(
            name="l_user", decision="allow", layer="per_session",
            tool="shell", match=("tool=shell",),
        )
        for order in (LAYERS, ("defaults", "per_session", "user_config"),
                      ("per_session", "user_config", "defaults")):
            program = PolicyProgram(rules=(floor_rule, allow_rule), layers=order)
            outcome = program.evaluate(
                {"tool": "shell", "command": "pytest tests/"}
            )
            assert outcome.decision == "deny", order
            assert outcome.floor is True, order
            assert outcome.rule == "l_floor", order

    def test_a_floor_that_does_not_match_does_not_short_circuit(self) -> None:
        """A dead floor rule must not silently answer for everything."""
        floor_rule = PolicyRule(
            name="l_floor",
            decision="deny",
            layer=pl.FLOOR_LAYER,
            screen=lambda command: command == "rm -rf /",
            match=("command=rm -rf /",),
        )
        allow_rule = PolicyRule(
            name="l_user", decision="allow", layer="user_config",
            tool="shell", match=("tool=shell",),
        )
        outcome = PolicyProgram(rules=(floor_rule, allow_rule)).evaluate(
            {"tool": "shell", "command": "pytest tests/"}
        )
        assert outcome.floor is False
        assert outcome.decision == "allow"

    def test_a_raising_screen_is_treated_as_a_match(self) -> None:
        """Fail closed. A broken screen is not a permissive screen.

        The floor's own receipt records which screens were exercised, so a
        screen that is broken rather than refusing is visible rather than
        silent.
        """
        def exploding(command: str) -> bool:
            raise RuntimeError("the screen is broken")

        rule = PolicyRule(
            name="l_boom", decision="deny", layer=pl.FLOOR_LAYER,
            screen=exploding, match=("command=anything",),
        )
        program = PolicyProgram(rules=(rule,))
        outcome = program.evaluate({"command": "anything"})
        assert outcome.decision == "deny"
        assert outcome.floor is True

    def test_for_layer_refuses_an_unknown_layer(self) -> None:
        program = pl.shipped_policy()
        with pytest.raises(PolicyValidationError):
            program.for_layer("not_a_layer")
        assert program.for_layer("defaults") is not program


class TestTheHardlineFoldDelegatesRatherThanDuplicates:
    def test_the_fold_reports_what_it_did_and_what_it_refused(self) -> None:
        """RECORDED against a moving target, so the assertion is about HONESTY.

        `hardline_floor_rules` folds a T3 pattern only when one of that
        pattern's own examples is refused AND the refusal names it. T3's table
        is being edited during this phase, so the fold count is a measurement,
        not a promise - but the REPORT must always be honest about which it
        is, and an empty fold must never read as 'covered'.
        """
        rules, report = pl.hardline_floor_rules()
        assert report.get("source"), "the fold must name what it folded from"
        assert "patterns_seen" in report
        if rules:
            assert report.get("available") is True
        else:
            assert report.get("available") is False, (
                "the fold produced no rules but claims to be available"
            )
            assert str(report.get("reason") or "").strip(), (
                "an unavailable fold must carry a reason"
            )
            for row in report.get("excluded") or ():
                assert str(row.get("reason") or "").strip(), (
                    f"excluded pattern {row.get('pattern_id')!r} has no reason"
                )

    def test_the_shared_module_never_restates_ts_patterns(self) -> None:
        """A second copy of T3's matcher is a second answer to one question.

        The check is STRUCTURAL (AST), and that is not a style preference - it
        is the only form that works here. Two earlier drafts of this test used
        a substring scan and both were wrong:

        * scanning for ``rm -rf`` failed immediately, because a policy rule's
          own ``match`` EXAMPLE may legitimately contain one;
        * scanning for ``HARDLINE_PATTERNS`` also failed, because the fold
          *references* T3's attribute by name with
          ``getattr(module, "HARDLINE_PATTERNS", ...)``.

        A bare substring cannot tell a DEFINITION from a REFERENCE. That is
        this repo's documented trap, and a gate that punishes correct
        behaviour is a gate people learn to route around. So what is forbidden
        is a module-level ASSIGNMENT of the pattern table, or a class
        DEFINITION of the refusal type - both of which are second
        implementations, and neither of which a reference can be.
        """
        import inspect

        import shared.policy_lang as module

        tree = ast.parse(inspect.getsource(module))

        assigned = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        for forbidden in ("HARDLINE_PATTERNS", "_PATTERNS", "HARDLINE_TABLE"):
            assert forbidden not in assigned, (
                f"shared/policy_lang.py ASSIGNS a module-level {forbidden!r}. "
                f"The floor is T3's; shared/ folds it by DELEGATION. An "
                f"assignment here is a second copy that will drift."
            )

        defined_classes = {
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        assert "HardlineRefused" not in defined_classes, (
            "shared/policy_lang.py defines its own HardlineRefused. The refusal "
            "TYPE must be T3's, or a caller catching one will not catch the "
            "other."
        )

        # The floor must be reached through a LAZY import inside the fold, not
        # at module scope. `shared/` is the bottom layer and may not import
        # `runtime/` eagerly.
        module_level_runtime_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and "runtime" in ast.dump(node)
        ]
        assert not module_level_runtime_imports, (
            "shared/policy_lang.py imports runtime at module scope. `shared/` is "
            "the bottom layer; the hardline fold must reach `runtime.hardline` "
            "lazily so the module stays importable with no runtime present."
        )

    def test_the_fold_actually_reaches_the_floor_when_it_is_available(self) -> None:
        """The control arm for the test above.

        "shared/ does not copy the table" is satisfiable by not folding at all,
        which is exactly what happened earlier in this phase. So the fold is
        also asserted to REACH T3's matcher when T3 exposes it - the two tests
        together say "delegates, and the delegation is live".
        """
        rules, report = pl.hardline_floor_rules()
        assert report.get("patterns_seen"), (
            "the fold saw no patterns at all; `shared/` may not be able to reach "
            "`runtime.hardline` any more"
        )
        if report.get("available"):
            assert rules, "an available fold must produce rules"
            for rule in rules:
                assert rule.layer == pl.FLOOR_LAYER
                assert rule.decision == "deny"
                assert rule.screen is not None, (
                    f"floor rule {rule.name!r} has no screen, so it matches "
                    f"nothing and is decoration"
                )

    def test_the_shipped_policy_reports_its_layer_order(self) -> None:
        assert pl.shipped_policy().as_dict()["layers"] == list(LAYERS)
