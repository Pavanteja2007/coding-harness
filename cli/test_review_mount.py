"""T4.W1 - the review mount, hunk-level verdicts, and the sanitiser contract.

Three things are pinned here that were previously unpinned because the
surface was unmounted:

1. **The sanitiser contract.** A review renders a unified diff - the exact
   content class P0 found leaking. ``cli/review.py`` had ZERO references to
   the sanitiser before this round, and ``render_review`` had ZERO
   references anywhere including tests. The source-level pin reads
   ``_bound``'s AST so a future mount cannot render a diff without it.
2. **Per-hunk verdicts.** Accepting hunk 2 and rejecting hunk 5 of the same
   file is the interaction every terminal agent is asked for, and it was
   impossible: granularity was per-file only.
3. **The concurrent-edit rule under hunks.** A partial accept changes no
   bytes, so the gate must not start refusing ordinary reverts afterwards.
   That is the accounting claim, and it is measured rather than argued.

Every test builds its OWN repository and journal under ``tmp_path``: no
developer ``logs/``, no provider, no Docker, no network. The file lives in
``cli/`` rather than ``tests/`` for the reason the sibling suites give -
T5 owns ``tests/**`` and a gate its own module's owner cannot edit without
a cross-terminal request is a gate that rots.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from cli import review

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_SOURCE = REPO_ROOT / "cli" / "review.py"

BASE_FILES: Dict[str, bytes] = {
    # Long enough that two changes are far apart. A file with the changes on
    # adjacent lines produces ONE hunk at any context size, which would make
    # "accept hunk 1, reject hunk 2" silently mean "accept the file" and
    # every per-hunk test in this file vacuous.
    "src/app.py": b"".join(b"line%d = %d\n" % (index, index) for index in range(1, 41)),
    "src/keep.py": b"keep = True\n",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            *args,
        ],
        check=False,
        capture_output=True,
    )


def write(root: Path, path: str, data: bytes) -> Path:
    """Write BYTES, never text - Windows text mode would rewrite newlines."""
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def make_run(tmp_path: Path, task_id: str = "agent-1") -> Tuple[Path, Path]:
    repo = tmp_path / task_id / "repo"
    task = tmp_path / task_id / "logs" / task_id
    for path, data in BASE_FILES.items():
        write(repo, path, data)
        write(task / "pristine", path, data)
    git(repo, "init", "-q")
    for path in BASE_FILES:
        git(repo, "add", path)
    git(repo, "commit", "-q", "-m", "base")
    return repo, task


def journal(task: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    target = task / "trace.jsonl"
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(__import__("json").dumps(row, ensure_ascii=False) + "\n")
    return target


def _two_hunk_after() -> bytes:
    """The same file with one change at line 5 and one at line 35."""
    lines = [b"line%d = %d\n" % (index, index) for index in range(1, 41)]
    lines[4] = b"line5 = 555\n"
    lines[34] = b"line35 = 3535\n"
    return b"".join(lines)


TWO_HUNK_AFTER = _two_hunk_after()


def edited(path: str = "src/app.py") -> Dict[str, Any]:
    return {
        "event": "edit_applied",
        "payload": {
            "path": path,
            "pre_sha256": sha(BASE_FILES[path]),
            "post_sha256": sha(TWO_HUNK_AFTER),
        },
    }


@pytest.fixture()
def two_hunk_run(tmp_path: Path) -> Tuple[Path, Path]:
    """A run whose single changed file has TWO hunks, both real.

    Two hunks is the minimum that makes per-hunk granularity a real
    question: with one hunk, "accept the hunk" and "accept the file" are
    the same action and a broken implementation passes.
    """
    repo, task = make_run(tmp_path)
    write(repo, "src/app.py", TWO_HUNK_AFTER)
    journal(task, [edited()])
    return repo, task


# ---------------------------------------------------------------------------
# 1. The sanitiser contract
# ---------------------------------------------------------------------------


class TestTheReviewCannotRenderUnsanitisedDiffContent:
    def test_the_single_bounded_line_function_reaches_the_sanitiser(self) -> None:
        """THE SOURCE-LEVEL PIN, in two hops, and both are real.

        ``_bound`` is the one function every rendered review line passes
        through. The gate reads the AST rather than grepping the module,
        because a grep for ``sanitize_text`` anywhere in a 3,000-line file
        is satisfied by a docstring - and this is precisely the mount the
        P0 audit named as the one whose render path is untrusted.

        Two hops because the module keeps a single cached resolver (so the
        hot path does not re-import per line): ``_bound`` must call THAT
        resolver, and the resolver must be the thing that reaches
        ``cli.ui.sanitize_text``. Pinning only one of the two would let a
        future edit route the render path around the other.
        """
        tree = ast.parse(REVIEW_SOURCE.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_bound" in functions, "cli/review.py must keep its _bound line boundary"
        assert "_sanitizer" in functions, (
            "cli/review.py must keep one cached sanitiser resolver"
        )

        def _calls(name: str) -> set:
            return {
                node.func.id
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }

        assert "_sanitizer" in _calls("_bound"), (
            "review._bound no longer routes through the sanitiser: the review "
            "surface renders a unified diff, so a line that reaches a sink "
            "without the sanitiser is a credential leak on the path P0 "
            "already found open. Restore the call rather than the comment."
        )
        source = REVIEW_SOURCE.read_text(encoding="utf-8")
        assert '"sanitize_text"' in source, (
            "the resolver no longer names cli.ui.sanitize_text"
        )

    def test_a_credential_in_a_diff_line_is_redacted_in_the_rendered_receipt(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(
            repo,
            "src/app.py",
            b'value = 1\nkey = "sk-FAKE-SECRET-VALUE0123456789"\n',
        )
        journal(task, [edited()])
        document = review.build_review(task, repo, width=120, max_diff_lines=200)
        rendered = "\n".join(review.review_lines(document, expanded=["src/app.py"]))

        assert "sk-FAKE-SECRET-VALUE0123456789" not in rendered, (
            "the live credential shape reached the rendered review"
        )
        assert "REDACTED" in rendered, (
            "the line should be redacted, not dropped: a review that deletes "
            "the line it refused to print is hiding evidence"
        )

    def test_an_ansi_escape_splitting_a_credential_is_still_caught(
        self, tmp_path: Path
    ) -> None:
        """The ORDER the P0 round fixed, exercised through the review path.

        The escapes are inside the file's own bytes, so the diff carries
        them. ``_bound`` strips control characters BEFORE handing the text to
        the sanitiser, which is what reassembles the token into something
        the redactor can match. Redacting first would protect a string the
        reader never sees and the strip would then rebuild the credential.
        """
        repo, task = make_run(tmp_path)
        write(
            repo,
            "src/app.py",
            b"value = 1\nkey = sk\x1b[35m-FAKE-SECRET-VALUE0123456789\n",
        )
        journal(task, [edited()])
        document = review.build_review(task, repo, width=120, max_diff_lines=200)
        rendered = "\n".join(review.review_lines(document, expanded=["src/app.py"]))

        assert "FAKE-SECRET-VALUE0123456789" not in rendered
        assert "\x1b" not in rendered, "a raw escape byte reached the receipt"

    def test_the_review_fails_closed_when_the_sanitiser_cannot_be_reached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing sanitiser withholds. It never prints the raw value.

        A renderer that fails closed by rendering the raw value is a
        renderer that fails open, and the only way this can be believed is
        if the poisoned arm is actually taken.
        """
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", TWO_HUNK_AFTER)
        journal(task, [edited()])
        document = review.build_review(task, repo, width=120, max_diff_lines=200)

        monkeypatch.setattr(review, "_SANITIZE", None)
        monkeypatch.setattr(review, "_SANITIZE_RESOLVED", True)
        rendered = "\n".join(review.review_lines(document, expanded=["src/app.py"]))

        assert "detail withheld" in rendered
        assert "second = 22" not in rendered, (
            "a withheld line still printed its content"
        )

    def test_a_raising_sanitiser_also_withholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", TWO_HUNK_AFTER)
        journal(task, [edited()])
        document = review.build_review(task, repo, width=120, max_diff_lines=200)

        def _boom(value: Any = "", **_kwargs: Any) -> str:
            raise RuntimeError("sanitiser exploded")

        monkeypatch.setattr(review, "_SANITIZE", _boom)
        monkeypatch.setattr(review, "_SANITIZE_RESOLVED", True)
        rendered = "\n".join(review.review_lines(document, expanded=["src/app.py"]))

        assert "detail withheld" in rendered
        assert "second = 22" not in rendered

    def test_the_widget_id_a_shell_mounts_is_declared_beside_the_payload(
        self,
    ) -> None:
        assert review.REVIEW_WIDGET_ID == "neo-review"


# ---------------------------------------------------------------------------
# 2. Per-hunk verdicts
# ---------------------------------------------------------------------------


class TestAHunkCanBeDecidedOnItsOwn:
    def test_the_fixture_really_has_two_hunks(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert len(record.hunk_headers) >= 2, (
            "this suite's whole premise is a file with more than one hunk; a "
            "fixture that collapsed to one makes every hunk test vacuous"
        )

    def test_a_hunk_acceptance_round_trips_and_never_touches_the_tree(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        before = (repo / "src/app.py").read_bytes()

        first = review.review_command(task, repo, "src/app.py#1", verb="accept")
        assert first["ok"] is True
        assert first["granularity"] == "hunk"
        assert (repo / "src/app.py").read_bytes() == before

        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert record.hunk_verdict(1) == "accepted"
        assert record.hunk_verdict(2) == "pending", (
            "accepting hunk 1 decided hunk 2 as well"
        )

    def test_two_hunks_of_one_file_can_carry_opposite_verdicts(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        review.review_command(task, repo, "src/app.py#2", verb="reject")

        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert record.hunk_verdict(1) == "accepted"
        assert record.hunk_verdict(2) == "rejected"

    def test_a_mixed_file_reduces_to_a_partial_word_not_to_either_verdict(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """The honesty point, and the reason the reduction exists.

        "Accept hunk 2, reject hunk 5" leaves the file neither accepted nor
        rejected. Rendering it as either is a fabricated verdict, and a
        reviewer who acts on it acts on a word this product did not mean.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        review.review_command(task, repo, "src/app.py#2", verb="reject")

        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert record.verdict == "pending/hunks-partial"
        assert record.verdict not in {"accepted", "rejected"}

    def test_an_untouched_review_reduces_to_exactly_the_per_file_decision(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """The reduction must be a NO-OP when no hunk was ever decided.

        Without this, adding hunk granularity would silently change the
        meaning of every review recorded before it existed.
        """
        repo, task = two_hunk_run
        review.record_decision(task, ["src/app.py"], "accepted")
        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert record.verdict == "accepted"
        assert record.hunk_decisions == ()

    def test_the_collapsed_row_counts_the_hunks_nobody_decided(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """Undecided hunks are COUNTED, never dropped.

        A row reading "1 accepted" when half the file was never looked at
        is a measurement nobody took.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        document = review.build_review(task, repo, width=140)
        rendered = "\n".join(review.review_lines(document))

        assert "hunks " in rendered
        assert "1 accepted" in rendered
        assert "0 rejected" in rendered
        assert "undecided" in rendered

    def test_the_hunk_word_rides_the_header_row_not_a_row_of_its_own(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """Per-hunk granularity must cost no extra vertical space.

        A 200-file change set with 40 lines per file cannot afford a
        separate verdict line per hunk; that is why the word is on the
        header a reader is already looking at.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        document = review.build_review(task, repo, width=140)
        rendered = review.review_lines(document, expanded=["src/app.py"])

        headers = [line for line in rendered if "hunk 1 [" in line]
        assert headers, "the expanded view did not show a hunk verdict"
        assert "[accepted]" in headers[0]

    def test_hunk_and_file_decisions_are_separate_records(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """A hunk decision must not write the FILE's decision row.

        Two records became one and a file read as decided because one of
        its five hunks was.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")

        assert review.decisions(task) == {}
        assert review.hunk_decisions(task) == {"src/app.py": {1: "accepted"}}

    def test_a_range_and_a_list_address_several_hunks_at_once(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        result = review.review_command(task, repo, "src/app.py#1-2", verb="accept")
        assert result["ok"] is True

        document = review.build_review(task, repo, width=120)
        record = {item.path: item for item in document.files}["src/app.py"]
        assert record.hunk_verdict(1) == "accepted"
        assert record.hunk_verdict(2) == "accepted"

    def test_an_unreadable_hunk_address_is_refused_with_the_spelling(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        result = review.review_command(task, repo, "src/app.py#nine", verb="accept")
        assert result["ok"] is False
        assert "path#N" in "\n".join(result["lines"])
        assert review.hunk_decisions(task) == {}

    def test_the_receipt_echoes_the_targets_it_parsed(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """A receipt that cannot say WHICH hunks it acted on hides a parse
        disagreement between the shell and the person typing."""
        repo, task = two_hunk_run
        result = review.review_command(task, repo, "src/app.py#1,2", verb="accept")
        assert "src/app.py#1,2" in "\n".join(result["lines"])


# ---------------------------------------------------------------------------
# 3. The concurrent-edit rule, with hunks
# ---------------------------------------------------------------------------


class TestTheConcurrentEditRuleStillHoldsWithHunkDecisions:
    def test_a_partial_accept_does_not_trip_the_gate_on_a_later_revert(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """THE ACCOUNTING CLAIM, measured.

        The gate refuses a restore only when the live hash appears NOWHERE
        in the range the run is accountable for. A partial accept writes no
        bytes, so the live hash is untouched and the range is untouched -
        and the ordinary revert the feature exists for must still work
        afterwards. If this ever goes red, hunk granularity has started
        corrupting the accounting.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.ok is True, (
            "accepting one hunk made the file's own revert look like a "
            "concurrent third-party edit"
        )
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]

    def test_a_hunk_rejection_that_covers_the_whole_file_still_restores(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        result = review.reject_hunks(task, repo, {"src/app.py": [1, 2]})

        assert result["applied"] is True
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]

    def test_a_partial_rejection_is_recorded_and_REPORTED_as_not_applied(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """The honesty boundary of hunk granularity.

        Reverting one hunk of a unified diff means reconstructing a file
        from a patch, and this module's restore is hash-pinned to whole
        bytes for a reason. So a partial rejection is recorded and said NOT
        to be applied - rather than half-applied behind a receipt that reads
        like a success.
        """
        repo, task = two_hunk_run
        result = review.reject_hunks(task, repo, {"src/app.py": [1]})

        assert result["decision"]["ok"] is True, "the decision must still be recorded"
        assert result["applied"] is False
        assert "NOT rewritten" in result["reason"]
        assert (repo / "src/app.py").read_bytes() == TWO_HUNK_AFTER, (
            "a partial rejection rewrote bytes it never claimed to have"
        )
        assert review.hunk_decisions(task)["src/app.py"][1] == "rejected"

    def test_a_revert_does_not_erase_the_recorded_hunk_decisions(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """The read-modify-write data loss a whole-receipt rewrite causes.

        ``_append_revert`` rewrites the whole receipt. Dropping a key it
        does not know about is how "reject hunk 2, then revert" silently
        un-records the rejection.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        review.revert_paths(task, repo, ["src/app.py"])

        assert review.hunk_decisions(task) == {"src/app.py": {1: "accepted"}}

    def test_a_human_edit_after_a_partial_accept_is_still_refused(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """The control arm. The gate must not be weakened by the new path.

        A test that only proves "the revert still works" would also pass
        with a gate that never refuses anything.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        (repo / "src/app.py").write_bytes(b"value = 99\n")

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.ok is False
        assert (repo / "src/app.py").read_bytes() == b"value = 99\n"
        assert any(
            str(row.get("reason")) == "concurrent_user_edit"
            for row in receipt.not_restored
        )


# ---------------------------------------------------------------------------
# 4. The mounted surface speaks the shared result shape
# ---------------------------------------------------------------------------


class TestApprovalOnDiffIsTheDefaultAndTheGateCannotBeForgotten:
    """The gate is DERIVED FROM CAPABILITY, not from a declared word.

    The previous rule was ``"require" if profile.approval == "ask"``. That
    works today only because ``build`` is both the only writing mode AND the
    only one that remembered the word - a trust property resting on a data
    field nobody is forced to set. The control arm below is the one that
    matters: a mode that can edit and forgets is still gated.
    """

    def test_every_mode_that_can_write_is_gated(self) -> None:
        from cli import commands

        ungated: List[str] = []
        for name in commands.MODE_NAMES:
            spec = commands.mode_spec(name)
            if spec is None:
                continue
            value, _reason = commands.resolve_agent_approval(spec)
            if commands.mode_writes(spec) and value != "require":
                ungated.append(name)
        assert not ungated, f"these modes can write and are NOT gated: {ungated}"

    def test_a_new_editing_mode_that_forgets_to_declare_approval_is_still_gated(
        self,
    ) -> None:
        from dataclasses import replace

        from cli import commands

        forgetful = replace(
            commands.mode_spec("ask"),
            name="newedit",
            visible_tools=("read", "edit", "write", "apply_patch"),
            denied_side_effects=(),
        )
        value, reason = commands.resolve_agent_approval(forgetful)

        assert value == "require"
        assert "newedit" in reason, (
            "the receipt must name the capability it gated on, or the "
            "derivation is not auditable"
        )

    def test_a_mode_that_denies_workspace_write_is_not_gated_twice(self) -> None:
        """`debug` runs tests, and a test run writes caches.

        Gating it would mean asking a person to approve every ``pytest``
        invocation. Its declared denial is a STRONGER control than a prompt
        and is applied independently, so this declines to double it.
        """
        from cli import commands

        spec = commands.mode_spec("debug")
        assert spec is not None
        assert "workspace_write" in spec.denied_side_effects
        assert commands.mode_writes(spec) is False
        assert commands.resolve_agent_approval(spec)[0] == "allow"

    def test_a_mode_with_no_write_tool_is_not_asked_at_all(self) -> None:
        """A prompt with no diff behind it is worse than no prompt.

        A person who is asked to approve something they cannot see learns to
        dismiss it, and that habit then applies to the runs where there IS
        a diff.
        """
        from cli import commands

        for name in ("ask", "explore", "plan", "review"):
            spec = commands.mode_spec(name)
            assert spec is not None
            value, _reason = commands.resolve_agent_approval(spec)
            assert value == "allow", f"{name} was prompted with nothing to show"

    def test_the_opt_out_exists_and_is_explicit(self) -> None:
        from cli import commands

        spec = commands.mode_spec("build")
        value, reason = commands.resolve_agent_approval(
            spec, {"mode_approval": "allow"}
        )
        assert value == "allow"
        assert "opted out" in reason, (
            "an opt-out that does not say it was an opt-out looks like the gate failing"
        )

    def test_a_typo_in_the_opt_out_does_not_disable_the_gate(self) -> None:
        """Fail closed, always. A mistyped opt-out must not be an opt-out."""
        from cli import commands

        spec = commands.mode_spec("build")
        assert commands.resolve_agent_approval(spec, {"mode_approval": "alow"})[0] == (
            "require"
        )
        assert commands.resolve_agent_approval(spec, {"mode_approval": ""})[0] == (
            "require"
        )
        assert commands.resolve_agent_approval(spec, {"mode_approval": True})[0] == (
            "require"
        )

    def test_the_gate_can_only_be_weakened_never_strengthened_by_config(
        self,
    ) -> None:
        from cli import commands

        spec = commands.mode_spec("build")
        assert commands.resolve_agent_approval(spec, {"mode_approval": "require"})[
            0
        ] == ("require")
        # A non-writing mode cannot be talked INTO a prompt, which would be
        # a dialog with nothing behind it.
        read_only = commands.mode_spec("ask")
        assert commands.resolve_agent_approval(read_only, {"mode_approval": "require"})[
            0
        ] == ("allow")


class TestTheMountedSurfaceKeepsTheSharedResultShape:
    def test_show_still_returns_the_undo_receipt_shape(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        result = review.review_command(task, repo, verb="show")

        assert set(result) >= {"handled", "kind", "ok", "lines", "payload"}
        assert result["handled"] is True
        assert result["kind"] == "show"
        assert result["lines"], "a show verb that renders nothing is a dead surface"

    def test_a_verb_this_module_does_not_own_is_still_handed_back(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """``handled=False`` means "not mine".

        This is what keeps ``/diff undo`` on the historical engine, and it
        is why the check runs on the TYPED verb before normalisation.
        """
        repo, task = two_hunk_run
        result = review.review_command(task, repo, "all", verb="undo")
        assert result["handled"] is False

    def test_render_review_does_not_drop_the_hunk_decisions(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        """``render_review`` re-projects every field by hand.

        A field it forgets is a decision that silently disappears when the
        surface changes width - which is the least noticeable moment to
        lose one.
        """
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        document = review.build_review(task, repo, width=120)
        re_rendered = review.render_review(document, width=60)

        assert re_rendered.hunk_decisions == document.hunk_decisions
        record = {item.path: item for item in re_rendered.files}["src/app.py"]
        assert record.hunk_verdict(1) == "accepted"

    def test_the_json_projection_carries_every_hunk_verbdict(
        self, two_hunk_run: Tuple[Path, Path]
    ) -> None:
        repo, task = two_hunk_run
        review.review_command(task, repo, "src/app.py#1", verb="accept")
        document = review.build_review(task, repo, width=120)
        payload = document.as_dict()
        record = {item["path"]: item for item in payload["files"]}["src/app.py"]

        assert payload["hunk_decisions"]["src/app.py"]["1"] == "accepted"
        assert record["hunk_verdicts"]["1"] == "accepted"
        assert record["hunk_verdicts"]["2"] == "pending", (
            "a --json consumer must be able to tell an undecided hunk from a "
            "decided one; a missing key would read as either"
        )
