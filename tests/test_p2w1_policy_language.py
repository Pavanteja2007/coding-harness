"""The policy language: examples that prove the rules, and a layering that is pinned.

`shared/policy_lang.py` exists so a rule is not only a decision but a decision
**plus the examples that demonstrate it**, and the examples are EXECUTED at load
time. These tests are the evidence for that claim, and for the three properties
around it that a policy is only as good as:

* the resolution order, which is a security property and not a preference;
* the arity dictionary, which is DATA and can be corrected without a code
  change -- and which therefore needs its own self-check;
* the `deny` -> visibility contract, in BOTH directions.

Every "never does X" test here has a control arm, because the cheapest way to
satisfy it is to never do anything.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from shared import policy_lang as pl

# ---------------------------------------------------------------------------
# The rule model and the example grammar.
# ---------------------------------------------------------------------------


def test_an_unknown_decision_is_refused_not_defaulted():
    """A rule whose decision silently became `ask` would turn a forbid into a
    question, and one that became `allow` would be worse than no rule."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.PolicyRule(
            name="x",
            decision="probably-fine",
            match=['tool=read'],
        )
    assert "unsupported policy decision" in str(excinfo.value)


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("allow", "allow"),
        ("prompt", "ask"),
        ("ask", "ask"),
        ("deny", "deny"),
        ("forbidden", "deny"),
        ("block", "deny"),
    ],
)
def test_the_starlark_spellings_load_and_normalise(spelling, expected):
    """Codex's own vocabulary is accepted, and it lands on OUR vocabulary."""
    rule = pl.PolicyRule(
        name="x", decision=spelling, match=['tool=read path="a.py"']
    )
    assert rule.decision == expected


def test_every_rule_must_carry_at_least_one_example():
    """The headline property, on a rule that declares none."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.validate_program(
            pl.PolicyProgram(
                rules=(pl.PolicyRule(name="silent", decision="ask"),)
            )
        )
    assert "declares no examples" in str(excinfo.value)
    assert excinfo.value.rule == "silent"


def test_a_self_inconsistent_rule_refuses_to_load():
    """THE demonstration: a rule whose declared example does not behave as declared.

    Two rules in ONE layer contradict each other. Last-match-wins means the
    later one silently wins, so without parse-time validation the earlier rule's
    `match` example is a lie that ships.
    """
    program = pl.shipped_policy(include_per_session=True)
    allow_curl = pl.PolicyRule(
        name="liar:allows-curl",
        decision="allow",
        layer="per_session",
        command_pattern=r"^curl\b",
        match=['tool=bash command="curl http://evil.invalid"'],
    )
    deny_curl = pl.PolicyRule(
        name="liar:later-denies-curl",
        decision="deny",
        layer="per_session",
        command_pattern=r"^curl\b",
        match=['tool=bash command="curl http://evil.invalid"'],
    )
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.validate_program(program.with_rules(allow_curl, deny_curl))
    error = excinfo.value
    assert error.rule == "liar:allows-curl"
    assert error.layer == "per_session"
    assert "curl" in error.example
    assert "does not behave as declared" in str(error)
    assert error.as_dict()["rule"] == "liar:allows-curl"


def test_a_not_match_example_the_rule_DOES_match_refuses_to_load():
    """`not_match` means "this rule does not apply", and it is enforced exactly."""
    liar = pl.PolicyRule(
        name="liar:claims-not-to-match",
        decision="ask",
        layer="per_session",
        command_pattern=r"^npm\b",
        match=['tool=bash command="npm test"'],
        not_match=['tool=bash command="npm test"'],
    )
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.validate_program(pl.shipped_policy().with_rules(liar))
    assert "cannot be listed as one it does not apply to" in str(excinfo.value)


def test_an_unparseable_example_names_its_OWN_rule():
    """A broad rule declared first must not be able to steal the error.

    The parse pass runs before any evaluation precisely so that a defect in THIS
    rule produces an error naming THIS rule. A validation failure that points at
    the wrong rule is worse than none: it sends the reader to fix the wrong
    thing.
    """
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.validate_program(
            pl.PolicyProgram(
                rules=(
                    pl.PolicyRule(
                        name="first:very-broad",
                        decision="allow",
                        layer="per_session",
                        match=["*"],
                    ),
                    pl.PolicyRule(
                        name="second:broken-example",
                        decision="ask",
                        layer="per_session",
                        match=["just some bare words"],
                    ),
                )
            )
        )
    assert excinfo.value.rule == "second:broken-example"
    assert "bare token" in str(excinfo.value)


@pytest.mark.parametrize(
    "example",
    [
        "just some bare words",
        'nonsense=tool path="a.py"',
        'tool=read tool=write path="a.py"',
    ],
)
def test_the_example_grammar_fails_closed(example):
    """An example the grammar does not understand is a refusal, not a guess."""
    with pytest.raises(pl.PolicyValidationError):
        pl.parse_subject(example)


def test_a_value_containing_spaces_must_be_quoted_and_the_error_says_so():
    """The one ergonomic rule of the grammar, with a message that teaches it."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.parse_subject('command=git commit -m "fix"')
    assert "bare token" in str(excinfo.value)
    parsed = pl.parse_subject('command="git commit -m \'fix\'"')
    assert parsed.command == "git commit -m 'fix'"


def test_a_string_where_a_list_of_examples_was_expected_is_refused():
    """`match="git status"` almost always meant one example; treating the STRING
    as a sequence of characters would produce a rule whose proof is nonsense."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.PolicyRule(
            name="x", decision="ask", match='tool=read path="a.py"'
        )
    assert "wrap it in a list" in str(excinfo.value)


def test_a_floor_rule_that_is_not_a_denial_refuses_at_construction():
    """A floor `ask` is a floor that can be talked past."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.PolicyRule(
            name="floor:not-a-denial",
            decision="ask",
            layer="floor",
            command_pattern=r"^npm\b",
            match=['tool=bash command="npm test"'],
        )
    assert "only carries denials" in str(excinfo.value)


def test_an_uncompilable_command_pattern_refuses_rather_than_silently_matching_nothing():
    """A rule whose matcher is broken must not read as a rule that matches nothing."""
    bad = pl.PolicyRule(
        name="x",
        decision="ask",
        layer="per_session",
        command_pattern=r"(unclosed",
        match=['tool=bash command="npm test"'],
    )
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.validate_program(pl.shipped_policy().with_rules(bad))
    assert "uncompilable command_pattern" in str(excinfo.value)


def test_two_rules_may_not_share_a_name():
    """A refusal names ONE rule; two rules with one name make that a guess."""
    with pytest.raises(pl.PolicyValidationError) as excinfo:
        pl.PolicyProgram(
            rules=(
                pl.PolicyRule(name="same", decision="ask", match=["*"]),
                pl.PolicyRule(name="same", decision="deny", match=["*"]),
            )
        )
    assert "duplicate rule name" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The shipped policy.
# ---------------------------------------------------------------------------


def test_the_shipped_policy_loads_and_every_rule_proved_itself():
    """The product claim, as a single assertion over the real table."""
    program = pl.shipped_policy(
        include_user_config=True, include_per_session=True
    )
    assert program.validated is True
    report = program.report()
    assert report["validated"] is True
    assert report["rule_count"] >= 25
    assert report["example_count"] >= 60
    assert [row["name"] for row in report["rules"] if not row["examples"]] == []
    assert [row["name"] for row in report["rules"] if not row["ok"]] == []


def test_every_shipped_rule_carries_examples_and_a_reason():
    """A rule with no reason cannot be rendered in a refusal a human reads."""
    for rule in pl.shipped_policy().rules:
        assert rule.has_examples, rule.name
        assert rule.reason, rule.name


def test_the_hardline_floor_is_folded_from_T3_not_restated_here():
    """Coordination, proven: the floor is delegated to, not duplicated.

    Every folded rule names `runtime.hardline.screen_or_raise` and carries NO
    regex of its own, so there is exactly one implementation of "is this command
    refused" in the tree.
    """
    rules, status = pl.hardline_floor_rules()
    assert status["available"] is True, status["reason"]
    assert status["patterns_seen"] >= 16
    assert status["folded"] == status["patterns_seen"]
    assert status["excluded"] == []
    assert status["delegates_to"] == "runtime.hardline.screen_or_raise"
    for rule in rules:
        assert rule.layer == pl.FLOOR_LAYER
        assert rule.decision == "deny"
        assert rule.command_pattern == "", (
            f"{rule.name} carries its own regex; that is a second matcher"
        )
        assert rule.screen is not None
        assert rule.has_examples


def test_a_hardline_pattern_with_no_examples_is_EXCLUDED_and_named():
    """A floor rule nobody demonstrated cannot be admitted to a validated program.

    Driven through a STAND-IN module rather than by editing T3's table: the point
    is the fold's behaviour, and editing another terminal's in-flight file to
    test it would be a collision.
    """
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FakePattern:
        pattern_id: str
        summary: str
        regex: str
        examples: tuple = ()

    class FakeRefused(Exception):
        pass

    class FakeModule:
        HARDLINE_PATTERNS = (
            FakePattern("proved", "a proved one", r"^rm\b", ("rm -rf /tmp/x",)),
            FakePattern("unproved", "no examples at all", r"^npm\b"),
        )

        @staticmethod
        def screen_or_raise(command):
            text = str(command)
            if text.startswith("rm"):
                raise FakeRefused("refused by the floor (proved)")
            if text.startswith("npm"):
                raise FakeRefused("refused by the floor (unproved)")

    # The fold requires the screen AND the refusal type; both are named the way
    # T3 names them, because the fold looks them up by those names.
    FakeModule.HardlineRefused = FakeRefused  # type: ignore[attr-defined]

    rules, status = pl.hardline_floor_rules(runtime_module=FakeModule)
    assert [rule.name for rule in rules] == ["floor:hardline:proved"]
    assert status["folded"] == 1
    assert status["patterns_seen"] == 2
    assert [row["pattern_id"] for row in status["excluded"]] == ["unproved"]
    assert "EXCLUDED" in status["reason"]


def test_the_fold_refuses_when_runtime_is_absent_rather_than_producing_an_empty_floor():
    """A missing dependency is REPORTED, not papered over with zero rules.

    An empty floor and a floor nobody loaded are the same bytes and only one of
    them is true, so the status carries which happened.
    """
    rules, status = pl.hardline_floor_rules(runtime_module=object())
    assert rules == ()
    assert status["available"] is False
    assert status["reason"]


def test_the_program_refuses_to_report_itself_validated_when_it_is_not():
    """`validated` is a claim, and it is only ever set by `validate_program`."""
    program = pl.PolicyProgram(rules=pl.shipped_policy().rules, validated=False)
    assert program.validated is False
    assert program.report()["validated"] is False


# ---------------------------------------------------------------------------
# Layering. Layer order is a security property.
# ---------------------------------------------------------------------------


def test_the_layer_order_is_exactly_the_documented_one():
    """A reader asking "what can override what" reads one tuple."""
    assert pl.LAYERS == (
        "floor",
        "defaults",
        "builtin_agent",
        "user_config",
        "per_agent",
        "per_session",
    )


def test_the_floor_is_not_overridable_by_ANY_later_layer():
    """The reason the floor is a layer rather than a high-priority rule.

    A `per_session` allow for the exact command the floor refuses still loses.
    """
    program = pl.shipped_policy(include_per_session=True)
    override = pl.PolicyRule(
        name="per-session:allow-root-wipe",
        decision="allow",
        layer="per_session",
        command_pattern=r"^rm\s+-rf\s+/",
        match=['tool=bash command="rm -rf /"'],
    )
    relaxed = pl.validate_program(program.with_rules(override))
    outcome = relaxed.evaluate(pl.parse_subject('tool=bash command="rm -rf /"'))
    assert outcome.decision == "deny"
    assert outcome.floor is True
    assert outcome.layer == "floor"


def test_resolution_is_LAST_MATCH_WINS_within_a_layer():
    """The pinned property, and the reason a shipped default is overridable."""
    program = pl.shipped_policy(include_per_session=True)
    outcome = program.evaluate(
        pl.parse_subject('tool=bash command="git commit -m \\"fix\\""')
    )
    assert outcome.decision == "allow"
    assert outcome.rule == "session:grant-git-commit-with-message"


def test_a_later_layer_beats_an_earlier_one():
    """`user_config` relaxes `defaults`; `per_session` relaxes `user_config`."""
    program = pl.shipped_policy(include_user_config=True, include_per_session=True)
    trusted_host = program.evaluate(
        pl.parse_subject("tool=web_fetch network_domain=arxiv.org")
    )
    assert trusted_host.decision == "allow"
    assert trusted_host.rule == "user:allow-arxiv-lookups"
    untrusted_host = program.evaluate(
        pl.parse_subject("tool=web_fetch network_domain=evil.invalid")
    )
    assert untrusted_host.decision == "ask"
    assert untrusted_host.layer == ""


def test_an_undecided_action_is_a_question_not_a_grant():
    """The layer default is `ask`, because the ABSENCE of a rule is not consent."""
    program = pl.shipped_policy()
    assert program.default_action == "ask"
    outcome = program.evaluate(pl.parse_subject('tool=some-unknown-tool'))
    assert outcome.decision == "ask"
    assert outcome.rule == "layer_default"


def test_the_outcome_reports_every_rule_that_matched():
    """Last-match-wins is only auditable if the losers are visible."""
    outcome = pl.shipped_policy().evaluate(
        pl.parse_subject('tool=edit path="src/app.py" side_effect_class=workspace_write')
    )
    assert outcome.decision == "ask"
    assert outcome.considered, outcome


def test_a_read_only_command_is_allowed_without_a_per_command_approval():
    """The arity dictionary earns its place by CHANGING THE POLICY, not only
    the prompt text: a rule can say "the dictionary declares this read-only"."""
    program = pl.shipped_policy()
    allowed = program.evaluate(pl.parse_subject('tool=bash command="git status"'))
    assert allowed.decision == "allow"
    assert allowed.rule == "default:read-only-command-allows"
    not_allowed = program.evaluate(pl.parse_subject('tool=bash command="git push"'))
    assert not_allowed.decision == "ask"


def test_the_engine_bridge_drops_the_dimensions_the_kernel_cannot_express():
    """A rule whose behaviour depends on a regex or a screen must say so.

    `to_engine_value` drops `command_pattern` / `command_risk` / `screen`, so a
    consumer that hands the mapping to `harness.agent_kernel.policy` gets the
    DIMENSIONS it understands rather than a silently-wrong rule.
    """
    folded, _status = pl.hardline_floor_rules()
    value = folded[0].to_engine_value()
    assert "command_pattern" not in value
    assert "screen" not in value
    assert "command_risk" not in value
    assert value["action"] == "deny"
    assert value["name"].startswith("floor:hardline:")


# ---------------------------------------------------------------------------
# The arity dictionary: DATA, and its own self-check.
# ---------------------------------------------------------------------------


def test_the_arity_table_is_a_DATA_FILE_and_it_is_present():
    """A 167-row table corrected by editing code is a table nobody can correct."""
    assert pl.ARITY_DATA_PATH.name == "command_arity.json"
    assert pl.ARITY_DATA_PATH.exists(), pl.ARITY_DATA_PATH
    document = json.loads(pl.ARITY_DATA_PATH.read_text(encoding="utf-8"))
    assert document["schema"] == "neo/policy-arity/v1"
    assert len(document["commands"]) >= 130


def test_the_arity_table_loads_and_every_entry_resolves_to_itself():
    """A table that loaded but resolves nothing would render every prompt as a
    blanket approval, which is the thing this table exists to replace."""
    table = pl.command_arity_table()
    assert table.available is True, table.reason
    assert len(table) >= 130
    for command in table.commands():
        assert table.lookup(command).command == command, command


def test_a_missing_arity_file_reports_UNAVAILABLE_rather_than_an_empty_table():
    """"No command is permitted" and "no dictionary was loaded" are different."""
    table = pl.load_command_arity("does-not-exist.json")
    assert table.available is False
    assert table.reason
    assert len(table) == 0


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        '{"schema": "x"}',
        '{"commands": {}}',
        '{"commands": {"git status": {"min_args": "many"}}}',
        '{"commands": {"git status": {"min_args": 3, "max_args": 1}}}',
        '{"commands": {"git status": {"min_args": 0, "risk": "spicy"}}}',
        '{"commands": {"git status": "not an object"}}',
    ],
)
def test_a_malformed_arity_file_reports_a_reason_not_a_partial_table(tmp_path, body):
    """A half-parsed dictionary is worse than none: it constrains some commands
    and silently permits the rest."""
    target = tmp_path / "arity.json"
    target.write_text(body, encoding="utf-8")
    table = pl.load_command_arity(target)
    assert table.available is False
    assert table.reason
    assert len(table) == 0


def test_the_arity_lookup_prefers_the_longest_declared_prefix():
    """Falling back to `git` would answer about the wrong command shape."""
    table = pl.command_arity_table()
    assert table.lookup("git commit -m x").command == "git commit"
    assert table.lookup("git status").command == "git status"
    assert table.lookup("frobnicate --wat") is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit", "git commit [<pathspec>] ..."),
        ("git status", "git status [<pathspec>]"),
        ("python -m pytest", "python -m pytest [<target>] ..."),
    ],
)
def test_the_scope_label_names_the_SHAPE_not_the_command(command, expected):
    """This is what lets a prompt render `git commit -m <message>` instead of a
    blanket bash approval."""
    assert pl.render_arity_scope_label(command) == expected


def test_an_undeclared_command_renders_as_an_explicit_anything():
    """A guess at a shape nobody declared would be a false claim."""
    assert pl.arity_pattern("frobnicate --wat") == "frobnicate --wat *"
    assert pl.render_arity_scope_label("frobnicate --wat") == "frobnicate --wat *"


def test_the_prompt_pattern_carries_the_flags_and_the_scope_label_does_not():
    """A human asked to approve `git commit` is being asked to approve
    `git commit --amend --no-verify`; a REMEMBERED scope is a narrow thing."""
    pattern = pl.arity_pattern("git commit")
    assert "--amend" in pattern
    assert "--amend" not in pl.render_arity_scope_label("git commit")


# ---------------------------------------------------------------------------
# `deny` -> visibility. Both directions are failures.
# ---------------------------------------------------------------------------


CATALOG = ("read", "list", "edit", "write", "bash", "verify")
MUTATIONS = ("edit", "write")


def test_an_unclassified_tool_is_visible_and_executable():
    """THE default, and the one a tool hidden by default would violate.

    A tool the agent cannot use, and cannot see that it cannot use, produces a
    worse plan silently. That is why the default is visible/executable and why
    it is asserted rather than assumed.
    """
    surface = pl.resolve_tool_surface(CATALOG, mutation_tools=MUTATIONS)
    assert set(surface.visible) == set(CATALOG)
    assert surface.executable == CATALOG
    assert surface.hidden == ()
    assert surface.violations == ()


def test_a_denied_tool_leaves_the_models_view_and_is_not_executable():
    """`deny` is itself a declaration, so it also ends executability."""
    surface = pl.resolve_tool_surface(
        CATALOG, decisions={"bash": "deny"}, mutation_tools=MUTATIONS
    )
    assert "bash" not in surface.visible
    assert "bash" in surface.hidden
    assert "bash" not in surface.executable
    assert "bash" not in surface.schema_names()
    assert surface.violations == ()


def test_the_invisible_but_executable_asymmetry_is_DECLARED_not_accidental():
    """The legitimate case, and the reason it needs a reason and an owner."""
    declaration = pl.ToolVisibilityDeclaration(
        tool="verify",
        visible=False,
        executable=True,
        reason="the harness runs its own verifier; the model needs no schema",
        owner="T1",
    )
    surface = pl.resolve_tool_surface(
        CATALOG, declarations=[declaration], mutation_tools=MUTATIONS
    )
    assert declaration.declared is True
    assert surface.withheld_but_runnable() == ("verify",)
    assert surface.violations == (), "a DECLARED hidden capability is not a defect"


def test_the_SAME_shape_without_a_declaration_is_a_violation():
    """A reason alone is a comment and an owner alone is a queue; both or neither."""
    for incomplete in (
        pl.ToolVisibilityDeclaration(
            tool="verify", visible=False, executable=True, reason="why", owner=""
        ),
        pl.ToolVisibilityDeclaration(
            tool="verify", visible=False, executable=True, reason="", owner="T1"
        ),
        pl.ToolVisibilityDeclaration(tool="verify", visible=False, executable=True),
    ):
        assert incomplete.declared is False
        surface = pl.resolve_tool_surface(
            CATALOG, declarations=[incomplete], mutation_tools=MUTATIONS
        )
        kinds = {row.kind for row in surface.violations}
        assert "undeclared_hidden_capability" in kinds, incomplete.as_dict()


def test_over_hiding_is_a_failure():
    """A tool the agent NEEDS but cannot see produces a worse plan, silently."""
    surface = pl.ToolSurface(
        visible=("read", "list"),
        hidden=("edit", "write"),
        executable=("read", "list", "edit", "write"),
        denied=(),
    )
    kinds = {row.kind for row in pl.surface_violations(surface, mutation_tools=MUTATIONS)}
    assert "mutation_tool_hidden" in kinds
    assert "no_mutation_tool_visible" in kinds


def test_under_hiding_is_a_failure():
    """A surface that lies about what is permitted is worse than one that says no."""
    surface = pl.ToolSurface(
        visible=("read", "edit", "write", "bash"),
        hidden=(),
        executable=("read", "edit", "write", "bash"),
        denied=("bash",),
    )
    kinds = {row.kind for row in pl.surface_violations(surface, mutation_tools=MUTATIONS)}
    assert "denied_tool_visible" in kinds


def test_a_tool_the_policy_denied_and_that_is_hidden_is_NOT_over_hiding():
    """The control for the over-hiding check: a honoured deny must not be
    reported as an over-hiding failure, or the check would cry wolf."""
    surface = pl.resolve_tool_surface(
        CATALOG, decisions={"edit": "deny"}, mutation_tools=MUTATIONS
    )
    kinds = {row.kind for row in surface.violations}
    assert "write" in surface.visible
    assert kinds == set(), (
        "a honoured deny must not be reported as an over-hiding failure, "
        f"or the check cries wolf: {sorted(kinds)}"
    )


def test_the_violation_vocabulary_is_closed():
    """A new failure mode must be added to the vocabulary, not invented silently."""
    assert pl.SURFACE_VIOLATIONS == (
        "denied_tool_visible",
        "mutation_tool_hidden",
        "undeclared_hidden_capability",
        "no_mutation_tool_visible",
    )


def test_the_surface_serialises_for_a_trace_row():
    """A receipt nobody can read off disk is a comment."""
    surface = pl.resolve_tool_surface(
        CATALOG,
        decisions={"bash": "deny"},
        mutation_tools=MUTATIONS,
    )
    record = surface.as_dict()
    assert json.loads(json.dumps(record)) == record
    assert record["visible"] and record["denied"] == ["bash"]


# ---------------------------------------------------------------------------
# Structure.
# ---------------------------------------------------------------------------


def test_every_declared_public_name_resolves():
    for name in pl.__all__:
        assert hasattr(pl, name), name


def test_the_module_imports_without_runtime_present():
    """`shared` is the bottom of the ladder; driven through a REAL child process,
    because an in-process check would see an already-imported `runtime`."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name == 'runtime' or name.startswith('runtime.') "
        "else None\n"
        "    def load_module(self, name):\n"
        "        raise ImportError('blocked')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from shared import policy_lang as pl\n"
        "program = pl.shipped_policy()\n"
        "assert program.validated is True\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_the_evals_runner_reports_clean_and_exits_zero():
    """The examples are a CI SUITE, not just a load-time check."""
    result = subprocess.run(
        [sys.executable, "-m", "evals.policy_examples"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "policy examples: CLEAN" in result.stdout
    assert "rules without examples: none" in result.stdout


def test_the_evals_runner_exits_two_when_a_rule_stops_proving_itself(monkeypatch):
    """The runner can FAIL. A gate that cannot fail is a comment."""
    from evals import policy_examples

    def broken(*_args, **_kwargs):
        report = policy_examples.build_report.__wrapped__() if False else None
        return report

    real = pl.shipped_policy

    def sabotaged(**kwargs):
        program = real(**kwargs)
        liar = pl.PolicyRule(
            name="liar:claims-allow",
            decision="allow",
            layer="per_session",
            command_pattern=r"^echo\b",
            match=['tool=bash command="echo hi"'],
        )
        denier = pl.PolicyRule(
            name="liar:later-denies",
            decision="deny",
            layer="per_session",
            command_pattern=r"^echo\b",
            match=['tool=bash command="echo hi"'],
        )
        return program.with_rules(liar, denier)

    monkeypatch.setattr(pl, "shipped_policy", sabotaged)
    report = policy_examples.build_report()
    assert report["verdict"] == "REGRESSED"
    assert "liar:claims-allow" in report["policy"]["failing_rules"]
    assert policy_examples.main([]) == policy_examples.EXIT_REGRESSION
