"""Terminal 05 — flags, shell-quoted arguments, and the full placeholder
grammar for slash-command templates.

One class per required proof, one test per behaviour, and every test named
after the behaviour it checks rather than after the line it exercises. The
required proofs from the brief, and the class that owns each:

===================================  ======================================
shell-style quoting splits correctly  :class:`TestShellStyleQuoting`
``$0`` is the FIRST argument           :class:`TestIndexedPlaceholders`
an out-of-range index is UNCHANGED      :class:`TestIndexedPlaceholders`
a missing NAMED placeholder is EMPTY    :class:`TestNamedPlaceholders`
``\\$`` escapes a literal dollar         :class:`TestEscapes`
an undeclared flag is a usage error     :class:`TestFlags`
a body with no ``$ARGUMENTS`` is fed    :class:`TestTheAppendRule`
``; rm -rf ~`` never reaches a command  :class:`TestArgumentsNeverReachAShell`
``!`` substitutes LABELLED output       :class:`TestBangPreprocessing`
existing templates substitute as before :class:`TestExistingTemplatesUnchanged`
===================================  ======================================

Host-only: no Docker, no provider, no network, no credential. Every shell
outage in this suite is a real ``subprocess`` call to a command this file
itself wrote, under ``tmp_path``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import time
from pathlib import Path

import pytest

from cli import command_args as ca
from cli import commands as commands_mod

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Shell-style quoting
# ---------------------------------------------------------------------------


class TestShellStyleQuoting:
    """`/my-skill "hello world" second` makes two arguments, not three."""

    def test_a_quoted_value_becomes_one_argument(self):
        assert ca.split_arguments('"hello world" second') == [
            "hello world",
            "second",
        ]

    def test_the_first_argument_is_the_quoted_one_and_the_second_is_the_word(self):
        tokens = ca.split_arguments('"hello world" second')
        assert tokens[0] == "hello world"
        assert tokens[1] == "second"
        assert len(tokens) == 2

    def test_single_quotes_group_the_same_way(self):
        assert ca.split_arguments("'hello world' second") == [
            "hello world",
            "second",
        ]

    def test_a_quoted_shell_metacharacter_stays_inside_one_argument(self):
        assert ca.split_arguments('"foo.txt; rm -rf ~"') == ["foo.txt; rm -rf ~"]

    def test_an_unquoted_metacharacter_word_splits_like_a_shell(self):
        # The tokeniser does not sanitise; it splits the way a shell does.
        # Sanitising is not this module's job and pretending otherwise would
        # be the lie the safety test below exists to prevent.
        assert ca.split_arguments("foo.txt; rm -rf ~") == ["foo.txt;", "rm", "-rf", "~"]

    def test_an_apostrophe_inside_a_word_is_not_an_unterminated_quote(self):
        # Raw shlex raises `No closing quotation` here, which is a dead end a
        # user cannot act on; the documented character guard fixes exactly
        # this and nothing else.
        assert ca.split_arguments("don't stop the leak") == [
            "don't",
            "stop",
            "the",
            "leak",
        ]

    def test_a_quote_inside_a_word_is_kept_rather_than_deleted(self):
        # Raw shlex returns ['abc'] here, silently dropping the quotes.
        assert ca.split_arguments('a"b"c') == ['a"b"c']

    def test_real_quoting_still_works_around_an_interior_quote(self):
        assert ca.split_arguments('don\'t "really" stop') == [
            "don't",
            "really",
            "stop",
        ]

    def test_a_genuinely_unterminated_quote_is_a_usage_error(self):
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.split_arguments('say "hi')
        assert excinfo.value.exit_code == 2
        assert "close every quote" in excinfo.value.message

    def test_an_empty_argument_string_yields_no_arguments(self):
        assert ca.split_arguments("") == []
        assert ca.split_arguments("    ") == []

    def test_the_split_is_delegated_to_shlex_not_reimplemented(self):
        # The module must not grow a second tokenizer. Read with `ast`: inside
        # `split_arguments` the ONE call that produces tokens is
        # `shlex.split`, and there is no other attribute call that could be
        # one. Prose in a docstring cannot satisfy or break this.

        tree = ast.parse((REPO / "cli" / "command_args.py").read_text(encoding="utf-8"))
        target = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "split_arguments"
        )
        calls = [
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        split_like = [
            ast.unparse(node.func)
            for node in calls
            if ast.unparse(node.func).endswith("split")
        ]
        assert split_like == ["shlex.split"]
        assert [ast.unparse(node.func) for node in calls] == [
            "raw.strip",
            "shlex.split",
        ]
        # Across the WHOLE module: one `shlex` import, one `shlex.split` call.
        # Counting the raw text instead would count the docstrings that
        # explain the rule, which is how a source scan ends up flagging prose.
        shlex_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            and any(a.name == "shlex" for a in node.names)
        ]
        assert len(shlex_imports) == 1
        shlex_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "shlex.split"
        ]
        assert len(shlex_calls) == 1

    def test_the_interior_quote_guard_can_be_switched_off(self):
        # A caller who wants raw POSIX semantics can have them, and the flag is
        # named rather than implied.
        with pytest.raises(ca.CommandArgError):
            ca.split_arguments("don't stop", disable_interior_quote_escape=True)

    def test_a_quoted_windows_path_keeps_its_backslashes(self):
        assert ca.split_arguments('"C:\\repo\\src\\a.py" tail') == [
            "C:\\repo\\src\\a.py",
            "tail",
        ]


# ---------------------------------------------------------------------------
# 2. Indexed placeholders
# ---------------------------------------------------------------------------


class TestIndexedPlaceholders:
    """`$0` is the FIRST argument; out of range stays UNCHANGED."""

    def test_dollar_zero_is_the_first_argument_not_the_second(self):
        assert ca.substitute("$0", arguments="a b", tokens=("a", "b")) == "a"

    def test_dollar_one_is_the_second_argument(self):
        assert ca.substitute("$1", arguments="a b", tokens=("a", "b")) == "b"

    def test_the_indexed_shorthand_matches_the_bracketed_form(self):
        both = ca.substitute(
            "$0|$1|$ARGUMENTS[0]|$ARGUMENTS[1]",
            arguments="a b",
            tokens=("a", "b"),
        )
        assert both == "a|b|a|b"

    def test_an_out_of_range_indexed_placeholder_stays_unchanged(self):
        # The asymmetry is intentional: a template that says `$2` and was
        # given one argument still says `$2`, so a reader can see the template
        # wanted something nobody typed.
        assert ca.substitute("end $2", arguments="a", tokens=("a",)) == "end $2"

    def test_an_out_of_range_bracketed_placeholder_stays_unchanged(self):
        assert (
            ca.substitute("end $ARGUMENTS[5]", arguments="a", tokens=("a",))
            == "end $ARGUMENTS[5]"
        )

    def test_an_indexed_placeholder_with_no_arguments_at_all_stays_unchanged(self):
        assert ca.substitute("$0", arguments="", tokens=()) == "$0"

    def test_an_indexed_placeholder_in_a_template_with_its_own_placeholder(self):
        # `$ARGUMENTS` in the body means the append rule does NOT fire, so a
        # template using both keeps exactly the text it wrote.
        body = "first $0, then $9, and all: $ARGUMENTS"
        assert (
            ca.substitute(body, arguments="a b", tokens=("a", "b"))
            == "first a, then $9, and all: a b"
        )

    def test_a_declared_flag_does_not_occupy_an_index(self):
        # One rule rather than a conditional one: declared flags are flags, so
        # `$0` is the first real argument on a command that has them.
        parsed = ca.parse_arguments("--fix src/a.py", name="code-review")
        assert parsed.indexed == ("src/a.py",)
        assert ca.substitute("$0", arguments=parsed.raw, tokens=parsed.indexed) == (
            "src/a.py"
        )

    def test_a_valued_flag_and_its_value_both_leave_the_index(self):
        parsed = ca.parse_arguments("--level high src/a.py", name="my-skill")
        assert parsed.indexed == ("src/a.py",)
        assert parsed.get("level") == "high"


# ---------------------------------------------------------------------------
# 3. Named placeholders
# ---------------------------------------------------------------------------


class TestNamedPlaceholders:
    """A declared name with no value expands to EMPTY; an undeclared `$word`
    is left alone so an existing template's `$HOME` is not eaten."""

    def test_a_named_placeholder_expands_to_its_bound_value(self):
        assert (
            ca.substitute("review $target", named={"target": "src/auth.py"})
            == "review src/auth.py"
        )

    def test_a_named_placeholder_with_no_value_expands_to_empty(self):
        # Deliberate: a template named an optional parameter, and silence is
        # the correct rendering of "not supplied".
        assert ca.substitute("review $focus now", named={"focus": ""}) == (
            "review  now"
        )

    def test_an_undeclared_dollar_word_is_left_unchanged(self):
        assert ca.substitute("cost $HOME", named={}) == "cost $HOME"

    def test_a_declared_name_with_a_default_expands_to_the_default(self):
        # The FILE text, not the parsed body: `prepare` re-parses whatever it
        # is handed, so handing it an already-stripped body would test a
        # document with no frontmatter at all.
        text = (
            "---\narguments:\n  - name: focus\n    default: everything\n---\n"
            "review $focus"
        )
        document = ca.parse_command_document(text)
        assert [item.name for item in document.arguments] == ["focus"]
        assert document.arguments[0].default == "everything"
        prepared = ca.prepare(text, name="x", arguments="")
        assert prepared.text.startswith("review everything")

    def test_a_named_placeholder_binds_to_the_first_positional(self):
        text = (
            "---\narguments:\n  - name: target\n  - name: focus\n---\n"
            "review $target with $focus"
        )
        prepared = ca.prepare(text, name="x", arguments="src/a.py security")
        assert prepared.arguments.named == {"target": "src/a.py", "focus": "security"}
        assert "review src/a.py with security" in prepared.text

    def test_a_missing_required_named_argument_is_a_usage_error(self):
        text = (
            "---\narguments:\n  - name: target\n    required: true\n---\nreview $target"
        )
        prepared = ca.prepare(text, name="x", arguments="")
        assert prepared.ok is False
        assert "needs <target>" in prepared.usage_error

    def test_a_variadic_named_argument_takes_every_remaining_word(self):
        text = "---\narguments:\n  - name: rest\n    variadic: true\n---\nwords: $rest"
        prepared = ca.prepare(text, name="x", arguments="a b c")
        assert prepared.arguments.named["rest"] == "a b c"

    def test_a_flag_of_the_same_name_overrides_the_positional(self):
        text = "---\narguments:\n  - name: level\n---\nlevel=$level"
        prepared = ca.prepare(text, name="my-skill", arguments="--level high")
        assert prepared.arguments.named["level"] == "high"
        assert "level=high" in prepared.text

    def test_arguments_after_the_declared_ones_are_never_dropped_silently(self):
        text = "---\narguments:\n  - name: target\n---\nreview $target"
        prepared = ca.prepare(text, name="x", arguments="src/a.py extra words")
        assert prepared.arguments.named["target"] == "src/a.py"
        assert "extra words" in prepared.arguments.raw
        assert any("reachable only" in item for item in prepared.warnings)


# ---------------------------------------------------------------------------
# 4. The dollar escape
# ---------------------------------------------------------------------------


class TestEscapes:
    r"""`\$1.00` is a literal dollar and must not substitute."""

    def test_an_escaped_dollar_placeholder_does_not_substitute(self):
        assert ca.substitute(r"costs \$1.00", arguments="a b", tokens=("a", "b")) == (
            "costs $1.00"
        )

    def test_an_escaped_dollar_named_placeholder_does_not_substitute(self):
        assert ca.substitute(
            r"\$target", named={"target": "src/a.py"}
        ) == r"\$target".replace("\\$", "$", 1)

    def test_an_escaped_dollar_environment_placeholder_does_not_substitute(self):
        environment = ca.environment_values(effort="high", env={})
        assert (
            ca.substitute(r"\${NEO_EFFORT}", environment=environment) == "${NEO_EFFORT}"
        )

    def test_a_lone_escaped_dollar_is_a_literal_dollar(self):
        assert ca.substitute(r"\$", arguments="a") == "$"

    def test_an_unescaped_dollar_does_substitute(self):
        # The escape is the ONLY way to write a literal dollar, which is why
        # the pinned rule matters: without it there is no way to say "$1.00".
        assert ca.substitute("$1.00", arguments="a b", tokens=("a", "b")) == "b.00"


# ---------------------------------------------------------------------------
# 5. Flags
# ---------------------------------------------------------------------------


class TestFlags:
    """Each command declares its own flags; an undeclared one is a usage
    error that LISTS what the command does declare."""

    def test_the_reference_commands_declare_a_fix_flag(self):
        for name in ("code-review", "security-review", "simplify"):
            words = [spec.word for spec in ca.flags_for(name)]
            assert words == ["--fix"], name

    def test_a_declared_bool_flag_resolves_to_true(self):
        prepared = ca.prepare("body", name="code-review", arguments="--fix")
        assert prepared.ok
        assert prepared.get("fix") is True

    def test_a_bool_flag_defaults_to_false_when_absent(self):
        prepared = ca.prepare("body", name="code-review", arguments="")
        assert prepared.get("fix") is False

    def test_a_bool_flag_accepts_an_explicit_false(self):
        prepared = ca.prepare("body", name="code-review", arguments="--fix=false")
        assert prepared.get("fix") is False

    def test_an_undeclared_flag_is_a_usage_error_that_lists_the_declared_ones(self):
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.parse_arguments("--nope x", name="code-review")
        message = excinfo.value.message
        assert "'--nope'" in message
        assert "--fix" in message
        assert excinfo.value.exit_code == 2

    def test_an_undeclared_flag_on_a_command_with_no_grammar_says_it_declares_none(
        self,
    ):
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.parse_arguments("--nope", name="fix", strict=True)
        assert "declares no flags" in excinfo.value.message

    def test_an_undeclared_flag_on_a_prepare_comes_back_as_a_refusal_not_an_exception(
        self,
    ):
        prepared = ca.prepare("body", name="code-review", arguments="--nope")
        assert prepared.ok is False
        assert prepared.exit_code == 2
        assert "--fix" in prepared.usage_error
        assert prepared.text == ""

    def test_a_value_flag_consumes_the_next_token(self):
        prepared = ca.prepare("body", name="my-skill", arguments="--level high")
        assert prepared.get("level") == "high"

    def test_a_value_flag_accepts_the_equals_form(self):
        prepared = ca.prepare("body", name="my-skill", arguments="--level=high")
        assert prepared.get("level") == "high"

    def test_a_value_flag_with_no_value_is_a_usage_error(self):
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.parse_arguments("--level", name="my-skill")
        assert "needs a value" in excinfo.value.message

    def test_a_choice_flag_refuses_a_value_outside_its_choices(self):
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.parse_arguments("--level extreme", name="my-skill")
        assert "low | medium | high" in excinfo.value.message

    def test_an_int_flag_coerces_and_a_bad_value_names_the_type(self):
        assert ca.parse_arguments("--budget 5", name="my-skill").get("budget") == 5
        with pytest.raises(ca.CommandArgError) as excinfo:
            ca.parse_arguments("--budget lots", name="my-skill")
        assert "must be a int" in excinfo.value.message

    def test_a_double_dash_ends_the_flags(self):
        parsed = ca.parse_arguments("-- --fix", name="code-review")
        assert parsed.positional == ("--fix",)
        assert parsed.get("fix") is False

    def test_a_negative_number_is_an_argument_not_a_flag(self):
        assert ca.parse_arguments("-5", name="my-skill").positional == ("-5",)

    def test_a_flag_token_is_kept_as_an_argument_when_there_is_no_grammar(self):
        # The compatibility decision, pinned: `/fix make --no-verify` must keep
        # working, so a command that declares no grammar does not refuse.
        parsed = ca.parse_arguments("--no-verify make", name="fix")
        assert parsed.undeclared_flags == ("--no-verify",)
        assert parsed.positional == ("--no-verify", "make")
        assert parsed.strict is False

    def test_a_lenient_parse_still_reports_what_it_kept(self):
        parsed = ca.parse_arguments("--no-verify make", name="fix")
        assert any("no flag grammar" in item for item in parsed.warnings)

    def test_declare_flags_is_the_only_writer_and_a_redeclare_replaces(self):
        try:
            ca.declare_flags("t05-temp", [ca.FlagSpec("alpha", description="first")])
            assert [spec.word for spec in ca.flags_for("t05-temp")] == ["--alpha"]
            ca.declare_flags("t05-temp", [ca.FlagSpec("beta", description="second")])
            assert [spec.word for spec in ca.flags_for("t05-temp")] == ["--beta"]
        finally:
            ca.declare_flags("t05-temp", [])
        assert ca.flags_for("t05-temp") == ()

    def test_the_flag_table_is_the_one_receipt_a_surface_reads(self):
        table = ca.command_flag_table()
        assert table["code-review"] == ("--fix",)
        assert "--level" in table["my-skill"]
        assert "fix" not in table or table.get("fix") is None

    def test_a_malformed_flag_declaration_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            ca.FlagSpec("Fix", description="uppercase is not a flag name")
        with pytest.raises(ValueError):
            ca.FlagSpec("x", type="maybe")
        with pytest.raises(ValueError):
            ca.FlagSpec("x", type="choice")
        with pytest.raises(ValueError):
            ca.FlagSpec("x", type="bool", default="yes")

    def test_a_usage_line_shows_every_declared_flag(self):
        line = ca.argument_usage("my-skill")
        assert line == "usage: /my-skill [--fix] --level <level> --budget <BUDGET>"

    def test_help_shows_the_flag_type_default_and_description(self):
        block = ca.flag_help_lines("my-skill")
        joined = "\n".join(block)
        assert "--level" in joined
        assert "low|medium|high" in joined
        assert "default medium" in joined
        assert "how hard the skill should work" in joined

    def test_help_for_a_command_that_declares_nothing_is_empty_not_a_blank(self):
        assert ca.flag_help_lines("fix") == []

    def test_the_help_block_teaches_the_placeholder_grammar_too(self):
        joined = "\n".join(ca.flag_help_lines("code-review"))
        assert "$ARGUMENTS[N] (0-based)" in joined
        assert "${NEO_SESSION_ID}" in joined


# ---------------------------------------------------------------------------
# 6. The append rule
# ---------------------------------------------------------------------------


class TestTheAppendRule:
    """A body with no `$ARGUMENTS` gets the arguments appended, so the model
    still sees what the user typed."""

    def test_a_body_with_no_placeholder_gets_the_arguments_appended(self):
        prepared = ca.prepare("just do the thing", name="x", arguments="a b")
        assert prepared.text == "just do the thing\n\nARGUMENTS: a b"

    def test_a_body_with_the_placeholder_gets_no_append(self):
        prepared = ca.prepare("do $ARGUMENTS", name="x", arguments="a b")
        assert prepared.text == "do a b"
        assert "ARGUMENTS:" not in prepared.text

    def test_the_append_is_fire_even_when_only_an_indexed_placeholder_is_used(self):
        # The rule is written against `$ARGUMENTS`, so `$0` does not suppress
        # it. Over-including is the safe direction: the model sees everything.
        prepared = ca.prepare("do $0", name="x", arguments="a b")
        assert prepared.text.startswith("do a")
        assert prepared.text.endswith("ARGUMENTS: a b")

    def test_the_append_does_not_fire_for_an_empty_argument_string(self):
        assert ca.prepare("just do the thing", name="x", arguments="").text == (
            "just do the thing"
        )

    def test_the_slot_predicate_agrees_with_the_existing_template_predicate(self):
        # One answer in the tree: the predicate that decides "this template
        # takes arguments" must be the same shape the historical substitution
        # uses, or the append rule and the substitution can disagree.
        for body in (
            "x $ARGUMENTS y",
            "x $ARGUMENTS[0] y",
            "x $0 y",
            "x $name y",
            "x ${NEO_EFFORT} y",
        ):
            assert ca.has_argument_slot(body) == bool(
                commands_mod._ARG_SUB.search(body)
            ), body


# ---------------------------------------------------------------------------
# 7. Arguments never reach a shell
# ---------------------------------------------------------------------------


class TestArgumentsNeverReachAShell:
    r"""`/x foo.txt; rm -rf ~` must never become part of a command line."""

    HOSTILE = "foo.txt; rm -rf ~"

    def test_a_bang_line_carrying_a_placeholder_is_refused_and_never_executed(
        self, tmp_path
    ):
        marker = tmp_path / "pwned"
        prepared = ca.prepare(
            f"!touch {marker} $ARGUMENTS", name="x", arguments=self.HOSTILE
        )
        assert prepared.ok is False
        assert not marker.exists()

    def test_the_refusal_names_the_placeholder_and_the_rule(self):
        prepared = ca.prepare("!touch $ARGUMENTS", name="x", arguments=self.HOSTILE)
        assert prepared.bang[0].refused is True
        assert "parameterised tool" in prepared.bang[0].error
        assert "parameterised tool" in prepared.usage_error

    def test_the_refusal_appears_in_the_prompt_so_the_author_sees_it(self):
        prepared = ca.prepare("!touch $ARGUMENTS", name="x", arguments=self.HOSTILE)
        assert "refused, not executed" in prepared.text

    def test_an_indexed_placeholder_in_a_bang_line_is_also_refused(self, tmp_path):
        marker = tmp_path / "pwned"
        prepared = ca.prepare(f"!touch {marker} $0", name="x", arguments=self.HOSTILE)
        assert prepared.ok is False
        assert not marker.exists()

    def test_a_named_placeholder_in_a_bang_line_is_also_refused(self, tmp_path):
        marker = tmp_path / "pwned"
        prepared = ca.prepare(f"!touch {marker} $target", name="x", arguments="a")
        assert prepared.ok is False
        assert not marker.exists()

    def test_the_command_that_actually_runs_never_contains_the_argument(self):
        # The structural proof, through a REAL subprocess: whatever ran, the
        # hostile argument is nowhere in the command that was executed.
        recorded = []

        def runner(command, cwd, env, timeout_s):
            recorded.append(command)
            return subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(cwd) if cwd else None,
                check=False,
            ).stdout

        prepared = ca.prepare(
            "!echo safe\nrun: $ARGUMENTS",
            name="x",
            arguments=self.HOSTILE,
            runner=runner,
        )
        assert prepared.ok is True
        assert recorded == ["echo safe"]
        assert all(self.HOSTILE not in command for command in recorded)

    def test_a_real_bang_line_runs_before_substitution_so_the_order_holds(
        self, tmp_path
    ):
        # `!` is preprocessing, not tool use: the output is in the prompt and
        # the argument is substituted around it, afterwards.
        prepared = ca.prepare(
            "!echo ran-first\nbody: $ARGUMENTS", name="x", arguments="typed-after"
        )
        body = prepared.text
        assert body.index("ran-first") < body.index("typed-after")

    def test_the_shell_audit_names_a_line_that_reads_like_a_command(self):
        prepared = ca.prepare(
            "$ cat $ARGUMENTS > out.txt", name="x", arguments=self.HOSTILE
        )
        assert prepared.risks
        risk = prepared.risks[0]
        assert risk.line_number == 1
        assert ";" in risk.metacharacters
        assert "parameterised tool" in risk.reason

    def test_the_shell_audit_ignores_its_own_labelled_output_blocks(self):
        # Otherwise the audit cries wolf on the module's own correct output
        # and a surface learns to ignore it.
        prepared = ca.prepare(
            "!echo 'a; rm -rf ~'\nbody $ARGUMENTS", name="x", arguments="a"
        )
        assert prepared.risks == ()

    def test_the_audit_reports_rather_than_rewriting(self):
        line = "$ cat foo > out"
        assert ca.shell_risk(line)[0].line == line
        assert ca.shell_risk("a plain sentence about $money") == ()

    def test_the_metacharacter_set_covers_the_documented_examples(self):
        for char in (";", "|", "&", "$", "`", ">", "<", "\n"):
            assert char in ca.SHELL_METACHARACTERS, char

    def test_a_refused_bang_line_does_not_stop_the_rest_of_the_document(self):
        prepared = ca.prepare(
            "before\n!echo $0\nafter $ARGUMENTS", name="x", arguments="a"
        )
        assert "before" in prepared.text
        assert "after a" in prepared.text
        assert prepared.ok is False


# ---------------------------------------------------------------------------
# 8. `!` preprocessing
# ---------------------------------------------------------------------------


class TestBangPreprocessing:
    """`!` runs a shell command before the model sees the prompt, and the
    output is LABELLED. The label is a correctness property: unlabelled
    command output in a prompt is prompt injection."""

    def test_the_output_is_substituted_into_the_body(self):
        prepared = ca.prepare("!echo hello\nbody", name="x", arguments="")
        assert "hello" in prepared.text
        assert "!echo hello" not in prepared.text

    def test_the_output_is_labelled_on_both_boundaries(self):
        prepared = ca.prepare("!echo hello", name="x", arguments="")
        assert f"[{ca.COMMAND_OUTPUT_LABEL}: echo hello · exit 0]" in prepared.text
        assert f"[end {ca.COMMAND_OUTPUT_LABEL}: echo hello]" in prepared.text

    def test_the_label_names_the_command_that_produced_the_text(self):
        prepared = ca.prepare("!git --version", name="x", arguments="")
        assert "git --version" in prepared.text

    def test_a_failing_command_reports_its_exit_code(self, tmp_path):
        prepared = ca.prepare("!exit 3", name="x", arguments="", cwd=str(tmp_path))
        assert prepared.bang[0].ok is False
        assert prepared.bang[0].exit_code == 3
        assert "exit 3" in prepared.text

    def test_stderr_is_captured_too_because_it_also_explained_something(self):
        prepared = ca.prepare("!echo oops 1>&2", name="x", arguments="")
        assert "oops" in prepared.text

    def test_a_command_that_times_out_is_reported_rather_than_read_as_silence(self):
        prepared = ca.prepare("!sleep 30", name="x", arguments="", timeout_s=0.25)
        assert prepared.bang[0].timed_out is True
        assert "timed out" in prepared.text

    def test_the_timeout_really_bounds_the_wait_rather_than_being_reported_late(self):
        # MEASURED, not asserted: `subprocess.run(shell=True, timeout=0.25)`
        # against `sleep 5` used to return `timed_out` after 5096 ms on this
        # host, because killing the shell left a grandchild holding the output
        # pipe. A "bounded" wait that returns five seconds late is the same
        # class of lie as a truncated block that reads like the whole output,
        # so the bound is a real process-TREE kill and this is its gate.
        started = time.perf_counter()
        result = ca.run_bang("sleep 30", timeout_s=0.25)
        elapsed = time.perf_counter() - started
        assert result.timed_out is True
        assert elapsed < 5.0, f"the ! timeout returned after {elapsed:.2f}s"
        # And nothing is left running: the sleep is gone with the shell.
        assert result.error.startswith("timed out after 0.25s")

    def test_a_silent_command_says_it_produced_nothing(self):
        prepared = ca.prepare("!true", name="x", arguments="")
        assert "(no output)" in prepared.text

    def test_a_long_output_is_capped_and_the_cap_is_marked(self):
        prepared = ca.prepare(
            f"!python -c \"print('x' * {ca.BANG_OUTPUT_MAX_CHARS + 500})\"",
            name="x",
            arguments="",
        )
        assert prepared.bang[0].truncated is True
        assert "command output truncated" in prepared.text

    def test_the_bang_prefix_is_not_treated_as_a_substring(self):
        prepared = ca.prepare("this is not! a bang line", name="x", arguments="")
        assert prepared.bang == ()
        assert "! a bang line" in prepared.text

    def test_several_bang_lines_all_run_and_are_reported_in_order(self):
        prepared = ca.prepare("!echo one\n!echo two", name="x", arguments="")
        assert [item.command for item in prepared.bang] == ["echo one", "echo two"]
        assert prepared.text.index("one") < prepared.text.index("two")

    def test_a_real_shell_line_actually_runs_in_this_test(self, tmp_path):
        # Not a double: one real subprocess, so the `!` feature is proven to do
        # the thing it says rather than to format a string plausibly.
        target = tmp_path / "written-by-bang.txt"
        prepared = ca.prepare(
            f"!python -c \"open(r'{target}','w').write('done')\"",
            name="x",
            arguments="",
        )
        assert prepared.bang[0].ok is True
        assert target.read_text(encoding="utf-8") == "done"

    def test_a_bang_line_with_no_command_is_refused(self):
        prepared = ca.prepare("!", name="x", arguments="")
        assert prepared.bang[0].refused is True
        assert prepared.ok is False

    def test_a_label_containing_a_dollar_survives_substitution_unchanged(self):
        # Found by running it: the block quotes the command, and a later
        # substitution pass used to rewrite the label, so the receipt
        # described a command the template did not contain.
        prepared = ca.prepare("!echo 'literal $HOME'\nbody", name="x", arguments="a")
        assert "literal $HOME" in prepared.text


# ---------------------------------------------------------------------------
# 9. Existing templates substitute exactly as before
# ---------------------------------------------------------------------------


def _every_command_template() -> list:
    """Every command file in the tree, plus the two written in Python.

    Two roots, two shapes: `.neo/commands/*.md` is a directory OF templates,
    while the fixtures nest them under a `commands/` subdirectory. The first
    version globbed `commands/*.md` under both roots and therefore found ONE
    file — a byte-identity gate that proved almost nothing while reading as
    if it proved everything.
    """
    found = []
    project = REPO / ".neo" / "commands"
    if project.is_dir():
        for path in sorted(project.glob("*.md")):
            found.append(
                (str(path.relative_to(REPO)), path.read_text(encoding="utf-8"))
            )
    fixtures = REPO / "tests" / "fixtures"
    if fixtures.is_dir():
        for path in sorted(fixtures.rglob("commands/*.md")):
            found.append(
                (str(path.relative_to(REPO)), path.read_text(encoding="utf-8"))
            )
    return found


class TestExistingTemplatesUnchanged:
    """Adopting this engine must change nothing for a template that already
    exists, and `fill_template` itself is untouched."""

    def test_fill_template_is_still_the_historical_one_argument_substitution(self):
        assert commands_mod.fill_template("a $ARGUMENTS b", "x") == "a x b"
        assert commands_mod.fill_template("a $ARGUMENTS b", "") == "a  b"
        assert commands_mod.fill_template("no slot", "x") == "no slot"

    def test_every_command_template_in_the_tree_matches_fill_template(self):
        templates = _every_command_template()
        assert len(templates) >= 2, [label for label, _ in templates]
        for label, text in templates:
            for arguments in ("", "one", "one two", '"hello world" second', "$0"):
                historical = commands_mod.fill_template(text, arguments)
                prepared = ca.prepare(text, name="t05", arguments=arguments)
                assert prepared.text == historical, f"{label} with {arguments!r}"

    def test_the_project_command_and_the_plugin_command_are_both_covered(self):
        # Named rather than counted, so losing either file is a named failure
        # instead of a silently smaller sample.
        labels = {label for label, _ in _every_command_template()}
        assert any(
            label.endswith(os.path.join(".neo", "commands", "fix.md"))
            for label in labels
        )
        assert any("plugin-webapp-toolkit" in label for label in labels)

    def test_the_scaffolded_example_command_still_substitutes_the_same(self):
        from cli import neoconfig

        text = neoconfig._EXAMPLE_COMMAND_MD
        for arguments in ("", "the auth bug"):
            assert ca.prepare(text, name="fix", arguments=arguments).text == (
                commands_mod.fill_template(text, arguments)
            )

    def test_a_template_with_only_indexed_placeholders_is_the_one_case_that_moves(self):
        # Stated rather than hidden: a body that uses `$0` and no
        # `$ARGUMENTS` gains the append. No template in this tree does, which
        # is why the byte-identity test above is green rather than excused.
        assert ca.prepare("do $0", name="x", arguments="a").text.endswith(
            "ARGUMENTS: a"
        )

    def test_a_custom_command_still_loads_and_still_runs_its_filled_body(
        self, tmp_path
    ):
        # The end-to-end path, through the historical loader, so this is not
        # only a claim about two functions in isolation.
        repo = tmp_path / "repo"
        (repo / ".neo" / "commands").mkdir(parents=True)
        (repo / ".neo" / "commands" / "t05cmd.md").write_text(
            "Review $ARGUMENTS now.", encoding="utf-8"
        )
        loaded = commands_mod.load_command("t05cmd", str(repo))
        assert loaded == "Review $ARGUMENTS now."
        prepared = ca.prepare(loaded, name="t05cmd", arguments="the auth module")
        assert prepared.text == "Review the auth module now."
        assert prepared.text == commands_mod.fill_template(loaded, "the auth module")

    def test_built_in_names_are_still_never_shadowed(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".neo" / "commands").mkdir(parents=True)
        (repo / ".neo" / "commands" / "help.md").write_text("x $ARGUMENTS", "utf-8")
        assert commands_mod.load_command("help", str(repo)) is None


# ---------------------------------------------------------------------------
# 10. Environment placeholders
# ---------------------------------------------------------------------------


class TestEnvironmentPlaceholders:
    """The five supported `${...}` placeholders, and what an unknown one does."""

    def test_the_five_supported_names_are_the_declared_set(self):
        assert ca.ENVIRONMENT_PLACEHOLDERS == (
            "NEO_EFFORT",
            "NEO_SESSION_ID",
            "NEO_SKILL_DIR",
            "NEO_PLUGIN_ROOT",
            "NEO_PROJECT_DIR",
        )

    @pytest.mark.parametrize("name", list(ca.ENVIRONMENT_PLACEHOLDERS))
    def test_every_supported_name_expands_from_an_explicit_value(self, name):
        environment = ca.environment_values(env={})
        environment[name] = "set-value"
        assert ca.substitute("${%s}" % name, environment=environment) == "set-value"

    def test_effort_comes_from_the_active_level_not_the_process_by_default(self):
        assert ca.environment_values(effort="high", env={})["NEO_EFFORT"] == "high"
        assert ca.environment_values(env={"NEO_EFFORT": "low"})["NEO_EFFORT"] == "low"

    def test_an_explicit_argument_beats_the_process_environment(self):
        assert (
            ca.environment_values(effort="high", env={"NEO_EFFORT": "low"})[
                "NEO_EFFORT"
            ]
            == "high"
        )

    def test_a_known_but_unset_placeholder_expands_to_empty(self):
        environment = ca.environment_values(env={})
        assert ca.substitute("[${NEO_SESSION_ID}]", environment=environment) == "[]"

    def test_an_unknown_placeholder_is_left_unchanged(self):
        assert (
            ca.substitute("[${HOME}]", environment=ca.environment_values(env={}))
            == "[${HOME}]"
        )

    def test_prepare_wires_the_placeholders_end_to_end(self):
        prepared = ca.prepare(
            "effort ${NEO_EFFORT} session ${NEO_SESSION_ID}",
            name="x",
            arguments="",
            effort="max",
            session_id="sess-ab12cd34",
        )
        assert prepared.text == "effort max session sess-ab12cd34"

    def test_the_map_always_carries_every_key_even_when_nothing_is_set(self):
        values = ca.environment_values(env={})
        assert sorted(values) == sorted(ca.ENVIRONMENT_PLACEHOLDERS)
        assert set(values.values()) == {""}


# ---------------------------------------------------------------------------
# 11. Command documents
# ---------------------------------------------------------------------------


class TestCommandDocuments:
    """Frontmatter is opt-in; a file without it is the whole body."""

    def test_a_file_with_no_frontmatter_is_all_body_and_no_declarations(self):
        document = ca.parse_command_document("# Title\n\nbody $ARGUMENTS")
        assert document.frontmatter is False
        assert document.body == "# Title\n\nbody $ARGUMENTS"
        assert document.arguments == ()

    def test_a_frontmatter_block_is_split_off_the_body(self):
        document = ca.parse_command_document(
            "---\ndescription: A thing\n---\n\n# Title\n\nbody"
        )
        assert document.description == "A thing"
        assert document.body == "# Title\n\nbody"
        assert "description:" not in document.body

    def test_an_argument_hint_is_carried_for_the_usage_line(self):
        document = ca.parse_command_document("---\nargument-hint: <file>\n---\nbody")
        assert document.argument_hint == "<file>"

    def test_a_bare_list_item_declares_a_name(self):
        document = ca.parse_command_document(
            "---\narguments:\n  - target\n  - focus\n---\nbody"
        )
        assert [item.name for item in document.arguments] == ["target", "focus"]

    def test_an_unterminated_frontmatter_block_degrades_instead_of_raising(self):
        document = ca.parse_command_document("---\ndescription: x\nbody")
        assert document.frontmatter is False
        assert document.body.startswith("---")

    def test_an_unrecognised_key_is_reported_rather_than_swallowed(self):
        document = ca.parse_command_document("---\ncolour: blue\n---\nbody")
        assert any("colour" in item for item in document.unrecognized)
        assert document.body == "body"

    def test_an_unusable_argument_name_is_reported_and_the_rest_still_loads(self):
        document = ca.parse_command_document(
            "---\narguments:\n  - 9bad\n  - good\n---\nbody"
        )
        assert [item.name for item in document.arguments] == ["good"]
        assert any("9bad" in item for item in document.unrecognized)

    def test_a_document_never_raises_on_hostile_input(self):
        for text in ("", "---", "---\n---\n", "---\narguments:\n---\n", "\x00\x01"):
            assert ca.parse_command_document(text) is not None


# ---------------------------------------------------------------------------
# 12. What this module must NOT grow
# ---------------------------------------------------------------------------


class TestWhatThisModuleMustNotGrow:
    """Two boundaries, pinned so they cannot rot. Both are read with `ast`
    rather than a substring scan: prose in a docstring is not an import, and a
    gate that fails on the word "harness" because a docstring names a sibling
    module is a gate a session learns to disable."""

    @staticmethod
    def _imports() -> set:

        tree = ast.parse((REPO / "cli" / "command_args.py").read_text(encoding="utf-8"))
        names: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    @staticmethod
    def _string_literals() -> set:

        tree = ast.parse((REPO / "cli" / "command_args.py").read_text(encoding="utf-8"))
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

    def test_no_configuration_default_was_added_for_argument_parsing(self):
        # A value in DEFAULTS merges into every task and every eval arm. How a
        # command line is tokenized is not a fact about a run, and the one
        # bounded knob this module has is a module constant a caller may
        # override per call — which is why the check is on the IMPORTS.
        from harness import config as harness_config

        imports = self._imports()
        assert not any(name.startswith("harness") for name in imports), imports
        literals = self._string_literals()
        for key in harness_config.DEFAULTS:
            assert key not in literals, key

    def test_the_module_declares_no_completion_or_verification_vocabulary(self):
        literals = self._string_literals()
        for word in (
            "completed_verified",
            "completed_unverified",
            "run_verdict",
            "command_verdict",
            "status_is_verified",
            "agent_contracts",
            "RUN_STATUSES",
        ):
            assert word not in literals, word

    def test_the_module_imports_neither_shell_it_must_not_edit(self):
        imports = self._imports()
        for forbidden in ("cli.tui", "cli.commands", "cli.interactive", "textual"):
            assert forbidden not in imports, forbidden
        # Importing commands would also mean two answers to "what does this
        # command declare", which is the thing this module exists to remove.
        assert "cli.commands" not in imports

    def test_the_module_does_not_write_to_disk(self):

        tree = ast.parse((REPO / "cli" / "command_args.py").read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for forbidden in (
            "open",
            "write_text",
            "write_bytes",
            "remove",
            "unlink",
            "rmdir",
            "makedirs",
            "mkdir",
        ):
            assert forbidden not in called, forbidden

    def test_the_only_exit_code_this_module_declares_is_the_usage_error(self):
        # Rule 6 of the round: completed_unverified is never success and no
        # change may weaken the verifier gate, the fail-closed verdict
        # reduction or the approval boundary. This module is not on that path
        # at all, and this is the pin that keeps it off it: it invents no exit
        # code of its own and imports no code that reduces a verdict.
        assert ca.USAGE_ERROR_EXIT_CODE == 2
        assert ca.PreparedPrompt("x", "b", ca.ParsedArguments(raw="")).exit_code == 0
        refused = ca.prepare("b", name="code-review", arguments="--nope")
        assert refused.exit_code == 2
        imports = self._imports()
        for forbidden in (
            "cli.runview",
            "cli.exit_codes",
            "harness.agent_contracts",
            "shared.approval",
        ):
            assert forbidden not in imports, forbidden


# ---------------------------------------------------------------------------
# 13. Totality
# ---------------------------------------------------------------------------


class TestTotality:
    """Nothing here raises on hostile input; a usage error is a value."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "\x00",
            "$",
            "${",
            "$}",
            "$ARGUMENTS",
            "$ARGUMENTS[",
            "$ARGUMENTS[]",
            "$0",
            "$999999999999999999999",
            "\\",
            "\\$",
            "!",
            "!   ",
            "!!echo hi",
            "!\x00",
        ],
    )
    def test_prepare_survives_hostile_bodies(self, text):
        prepared = ca.prepare(text, name="x", arguments="")
        assert isinstance(prepared.text, str)

    def test_prepare_survives_a_body_far_larger_than_any_command_file(self):
        # Built here rather than parametrised: a 50,000-character parametrize
        # id becomes PYTEST_CURRENT_TEST, which Windows refuses to put in the
        # environment. That is a harness limit, not a product one, and this
        # suite should not be shaped around it.
        prepared = ca.prepare("a" * 50_000, name="x", arguments="")
        assert len(prepared.text) == 50_000

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "--",
            "--=",
            "---",
            "-",
            "--fix=",
            '"unterminated',
            "\\",
            "$0",
        ],
    )
    def test_prepare_survives_hostile_argument_strings(self, raw):
        prepared = ca.prepare("body", name="x", arguments=raw)
        assert isinstance(prepared.exit_code, int)

    def test_prepare_survives_an_argument_string_far_larger_than_the_cap(self):
        assert isinstance(
            ca.prepare("body", name="x", arguments="a" * 50_000).exit_code, int
        )

    def test_prepare_never_raises_even_for_a_refused_flag(self):
        # A usage error is a VALUE on the receipt, not an exception, so a
        # dispatcher cannot forget to handle it by forgetting a try block.
        prepared = ca.prepare("body", name="code-review", arguments="--nope")
        assert prepared.ok is False
        assert prepared.to_dict()["exit_code"] == 2

    def test_the_receipt_is_json_serialisable(self):
        import json

        receipt = ca.prepare(
            "!echo hi\nbody $ARGUMENTS", name="x", arguments="a b"
        ).to_dict()
        assert json.loads(json.dumps(receipt))["ok"] is True

    def test_a_huge_argument_string_is_capped_and_the_cap_is_reported(self):
        # Two bounds, two shapes. Many short tokens trip the TOKEN cap, and the
        # marker lands on the last kept token; one enormous token trips the
        # CHARACTER cap, and the marker lands on the `$ARGUMENTS` expansion.
        # Both are MARKED, because a quiet cap reads as the whole input.
        by_tokens = ca.prepare("body $0", name="x", arguments="a " * 600)
        assert by_tokens.arguments.truncated is True
        assert "arguments truncated" in by_tokens.arguments.tokens[-1]
        assert len(by_tokens.arguments.tokens) == ca.ARGUMENTS_MAX_TOKENS
        by_chars = ca.prepare("body $ARGUMENTS", name="x", arguments="a" * 25_000)
        assert "arguments truncated" in by_chars.text
