"""VEX-PF-05 - the diff review surface, and the revert that puts it back.

One class per required behaviour, named after the behaviour rather than the
function, so a rename cannot make a proof pass silently. Every test builds
its OWN repository and journal under ``tmp_path``: no developer ``logs/``,
no provider, no Docker, no network. Nothing here asserts timing, because a
timing assertion on a shared four-terminal host measures the scheduler.

The suite is deliberately hostile in three places, because that is where a
trust surface breaks: a repository path containing ``[`` (markup), a
concurrent human edit (the accounting rule), and a TAMPERED pre-image (the
one failure that would otherwise be reported as a verified restore).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from cli import review


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


BASE_FILES: Dict[str, bytes] = {
    "src/app.py": b"value = 1\n",
    "src/keep.py": b"keep = True\n",
    "docs/notes.md": b"notes\n",
}


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
    """Write BYTES, never text.

    Windows text mode rewrites ``\\n`` as ``\\r\\n``, which would make every
    recorded pre-image hash disagree with the file it describes and turn
    these tests into a newline test.
    """
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def make_run(
    tmp_path: Path, task_id: str = "agent-1", *, git_repo: bool = True
) -> Tuple[Path, Path]:
    """A real repository, a real pristine reference, a real journal."""
    repo = tmp_path / task_id / "repo"
    task = tmp_path / task_id / "logs" / task_id
    for path, data in BASE_FILES.items():
        write(repo, path, data)
    for path in BASE_FILES:
        write(task / "pristine", path, BASE_FILES[path])
    if git_repo:
        git(tmp_path, "-C", str(repo), "init", "-q")
        for path in BASE_FILES:
            git(repo, "add", path)
        git(repo, "commit", "-q", "-m", "base")
    return repo, task


def journal(task: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    target = task / "trace.jsonl"
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return target


def edited(
    path: str = "src/app.py",
    before: bytes = BASE_FILES["src/app.py"],
    after: bytes = b"value = 2\n",
) -> Dict[str, Any]:
    """The mutation receipt shape the harness actually writes."""
    return {
        "event": "edit_applied",
        "payload": {"path": path, "pre_sha256": sha(before), "post_sha256": sha(after)},
    }


def rows_of(receipt: Any) -> List[Mapping[str, Any]]:
    return list(receipt.not_restored)


def reasons(receipt: Any) -> List[str]:
    return [str(item.get("reason")) for item in rows_of(receipt)]


class TestTheReviewShowsWhatTheRunActuallyDid:
    def test_the_diff_is_measured_from_the_pristine_reference_not_declared(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        document = review.build_review(task, repo, width=100)
        rows = {item.path: item for item in document.files}

        assert rows["src/app.py"].state == "changed"
        assert rows["src/app.py"].additions >= 1
        assert document.additions >= 1

    def test_a_file_the_run_never_touched_is_not_in_the_change_set(
        self, tmp_path: Path
    ) -> None:
        """The review's subject is the run, not the working tree.

        On a shared tree `git status` reports every teammate's edit, and
        listing those under "what this run did" is the attribution lie the
        file layer already fixed once.
        """
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        document = review.build_review(task, repo, width=100)

        assert "src/keep.py" not in {item.path for item in document.files}

    def test_the_harness_change_set_and_the_review_disagree_loudly_when_they_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-check that cannot fail is not a cross-check.

        ``harness.editor.changed_files`` is stubbed to report one path more
        than the review measured, which is the disagreement the surface has
        to REPORT rather than resolve by picking a winner.
        """
        from harness import editor

        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        monkeypatch.setattr(
            editor,
            "changed_files",
            lambda *args, **kwargs: ("src/app.py", "src/only-harness-saw.py"),
        )

        document = review.build_review(task, repo, width=100)

        assert document.disagreement
        assert any("harness" in item for item in document.disagreement)

    def test_an_unverified_run_stays_fully_reviewable_and_is_never_called_success(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(
            task,
            [
                {"event": "task_start", "payload": {}},
                {
                    "event": "result",
                    "payload": {
                        "status": "completed_unverified",
                        "changed_files": ["src/app.py"],
                        "verification_evidence": [],
                    },
                },
            ],
        )

        document = review.build_review(task, repo, width=100)
        lines = review.review_lines(document, expanded=["src/app.py"])

        assert document.verdict == "unverified"
        assert document.verified is False
        assert any("NOT verified" in line for line in lines)
        # Reviewability is independent of verification: the actions are there.
        assert document.reviewable is True
        assert "unreviewable" not in " ".join(lines).lower()

    def test_the_per_file_verified_claim_cannot_promote_an_unverified_run(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(
            task,
            [
                {
                    "event": "edit_applied",
                    "payload": {"path": "src/app.py", "verified": True},
                },
                {
                    "event": "result",
                    "payload": {
                        "status": "completed_unverified",
                        "changed_files": ["src/app.py"],
                    },
                },
            ],
        )

        document = review.build_review(task, repo, width=100)
        rows = {item.path: item for item in document.files}

        assert document.verified is False
        assert rows["src/app.py"].verified is False


class TestTheDecisionIsSeparateFromTheOperation:
    def test_accepting_records_a_decision_and_never_touches_a_file(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        before = (repo / "src/app.py").read_bytes()

        result = review.accept_paths(task, ["src/app.py"])

        assert result["ok"] is True
        assert (repo / "src/app.py").read_bytes() == before
        assert review.decisions(task) == {"src/app.py": "accepted"}

    def test_an_acceptance_survives_into_the_next_review(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        review.accept_paths(task, ["src/app.py"])

        document = review.build_review(task, repo, width=100)
        rows = {item.path: item for item in document.files}

        assert rows["src/app.py"].verdict == "accepted"

    def test_rejecting_both_records_and_restores(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        result = review.reject_paths(task, repo, ["src/app.py"])

        assert result["decision"]["ok"] is True
        assert result["revert"].ok is True
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]
        assert review.decisions(task)["src/app.py"] == "rejected"

    def test_an_unknown_decision_is_refused_with_the_words_it_expects(
        self, tmp_path: Path
    ) -> None:
        _, task = make_run(tmp_path)

        result = review.record_decision(task, ["src/app.py"], "maybe")

        assert result["ok"] is False
        assert result["status"] == "unknown_decision"
        assert "accept" in result["reason"]


class TestTheRevertIsHashPinnedInBothDirections:
    def test_a_restore_is_only_reported_verified_when_the_bytes_match_the_pre_image(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.ok is True
        assert receipt.verified is True
        assert receipt.restored[0]["after_hash"] == receipt.restored[0]["pristine_hash"]
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]

    def test_the_receipt_writes_an_audit_trail_to_disk(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.written is True
        assert Path(receipt.receipt_path).is_file()
        stored = json.loads(Path(receipt.receipt_path).read_text(encoding="utf-8"))
        assert stored["reverts"], "the revert must be readable after the process exits"

    def test_a_second_revert_reports_the_file_already_at_its_pre_image(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        review.revert_paths(task, repo, ["src/app.py"])

        again = review.revert_paths(task, repo, ["src/app.py"])

        assert again.unchanged == ("src/app.py",)
        assert again.restored == ()

    def test_a_file_the_run_deleted_is_put_back(self, tmp_path: Path) -> None:
        """A run that deletes your file is not "no changes".

        Reading a missing file as nothing-to-do is how a destructive run
        produces an empty diff and a clean-looking receipt.
        """
        repo, task = make_run(tmp_path)
        (repo / "docs/notes.md").unlink()
        journal(
            task,
            [
                {
                    "event": "edit_applied",
                    "payload": {"path": "docs/notes.md", "pre_sha256": sha(b"notes\n")},
                }
            ],
        )

        receipt = review.revert_paths(task, repo, ["docs/notes.md"])

        assert (repo / "docs/notes.md").read_bytes() == b"notes\n"
        assert receipt.verified is True

    def test_a_file_the_run_created_is_never_deleted(self, tmp_path: Path) -> None:
        """Deleting is the one irreversible thing here, so it is refused.

        The reference holds no pre-image and the run recorded no pre-image,
        so a delete would be a guess with no receipt behind it.
        """
        repo, task = make_run(tmp_path)
        write(repo, "src/new.py", b"new = 1\n")
        journal(
            task,
            [
                {
                    "event": "tool_call",
                    "payload": {"tool": "write", "arguments": {"path": "src/new.py"}},
                }
            ],
        )

        receipt = review.revert_paths(task, repo, ["src/new.py"])

        assert (repo / "src/new.py").is_file()
        assert "src/new.py" in str(rows_of(receipt))
        assert "no_pre_image" in reasons(receipt)

    def test_a_half_revert_is_partial_not_a_failure_and_not_a_success(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        # The reference loses one of two pre-images, so only one can come back.
        write(repo, "src/keep.py", b"keep = False\n")
        (task / "pristine/src/keep.py").unlink()
        journal(
            task,
            [
                edited(),
                {
                    "event": "edit_applied",
                    "payload": {
                        "path": "src/keep.py",
                        "pre_sha256": sha(b"keep = True\n"),
                    },
                },
            ],
        )

        receipt = review.revert_paths(task, repo, ["src/app.py", "src/keep.py"])

        assert receipt.status == "partial"
        assert receipt.ok is False
        assert receipt.verified is False
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]


class TestAConcurrentUserEditIsRefusedNotOverwritten:
    def test_a_human_edit_is_refused_and_survives(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (repo / "src/app.py").write_bytes(b"value = 99\n")

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.ok is False
        assert (repo / "src/app.py").read_bytes() == b"value = 99\n"
        assert "concurrent_user_edit" in reasons(receipt)

    def test_the_refusal_names_the_content_it_refused_to_destroy(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (repo / "src/app.py").write_bytes(b"value = 99\n")

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        refusal = rows_of(receipt)[0]
        assert refusal["actual_hash"] == sha(b"value = 99\n")
        assert refusal["expected_hashes"], (
            "the receipt must say what it expected to find"
        )

    def test_the_runs_own_change_is_not_mistaken_for_a_concurrent_edit(
        self, tmp_path: Path
    ) -> None:
        """The ordinary case: the bytes on disk ARE the run's own output.

        Refusing here would refuse every revert the feature exists for.
        """
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert receipt.ok is True
        assert receipt.overwritten_user_edits == ()

    def test_forcing_over_a_human_edit_records_what_it_destroyed(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (repo / "src/app.py").write_bytes(b"value = 99\n")

        receipt = review.revert_paths(task, repo, ["src/app.py"], force=True)

        assert receipt.ok is True
        assert receipt.forced is True
        assert len(receipt.overwritten_user_edits) == 1
        assert receipt.overwritten_user_edits[0]["overwritten_hash"] == sha(
            b"value = 99\n"
        )
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]

    def test_a_forced_revert_that_reads_like_a_clean_one_would_be_the_defect(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (repo / "src/app.py").write_bytes(b"value = 99\n")
        review.revert_paths(task, repo, ["src/app.py"], force=True)

        rendered = "\n".join(
            review.render_receipt(receipt_lines(repo, task, ["src/app.py"]))
        )

        assert "forced" in rendered


def receipt_lines(repo: Path, task: Path, paths: Sequence[str]) -> Any:
    """A second revert of already-restored paths, for the render assertions."""
    return review.revert_paths(task, repo, list(paths))


class TestACorruptReferenceIsReportedAndRefused:
    def test_a_tampered_pre_image_is_detected_by_its_hash(self, tmp_path: Path) -> None:
        """Presence is not integrity.

        A presence-only check restores from altered bytes and calls the
        result verified, which is the worst receipt this module could emit.
        """
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (task / "pristine/src/app.py").write_bytes(b"attacker = 0\n")

        health = review.snapshot_health(task, repo)

        assert health.state == "corrupt"
        assert "src/app.py" in health.mismatched_preimages

    def test_a_tampered_pre_image_is_never_restored_from(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (task / "pristine/src/app.py").write_bytes(b"attacker = 0\n")

        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert (repo / "src/app.py").read_bytes() == b"value = 2\n"
        assert receipt.verified is False
        assert "src/app.py" in str(rows_of(receipt))

    def test_a_lost_pre_image_is_reported_by_name(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (task / "pristine/src/app.py").unlink()

        health = review.snapshot_health(task, repo)

        assert health.state == "corrupt"
        assert "src/app.py" in health.missing_preimages
        assert health.usable is False

    def test_missing_and_corrupt_are_different_answers(self, tmp_path: Path) -> None:
        """A run with no reference can still be reviewed; a corrupt one cannot be trusted."""
        _, task = make_run(tmp_path)
        no_reference = review.snapshot_health(task / "nothing-here", None)
        _, other = make_run(tmp_path, "agent-2")
        journal(other, [edited()])
        (other / "pristine/src/app.py").unlink()
        corrupt = review.snapshot_health(other, None)

        assert no_reference.state == "missing"
        assert corrupt.state == "corrupt"
        assert no_reference.state != corrupt.state

    def test_a_file_the_run_created_is_not_reported_as_a_lost_pre_image(
        self, tmp_path: Path
    ) -> None:
        """A run-created file has no pre-image, and its absence is not corruption."""
        repo, task = make_run(tmp_path)
        write(repo, "src/new.py", b"new = 1\n")
        journal(
            task,
            [
                {
                    "event": "tool_call",
                    "payload": {
                        "tool": "write",
                        "arguments": {"path": "src/new.py"},
                        "post_sha256": sha(b"new = 1\n"),
                    },
                }
            ],
        )

        health = review.snapshot_health(task, repo)

        assert health.state == "ok"
        assert "src/new.py" not in health.missing_preimages


class TestTheReviewNeverDeliversAMarkupPayload:
    @pytest.mark.parametrize(
        "name",
        ["weird[name].py", "bracket[0].py", "close][/].py", "amp&and.py"],
    )
    def test_a_hostile_repository_path_is_printed_not_eaten(
        self, tmp_path: Path, name: str
    ) -> None:
        repo, task = make_run(tmp_path, "agent-markup")
        write(repo, name, b"value = 1\n")
        write(task / "pristine", name, b"value = 1\n")
        write(repo, name, b"value = 2\n")
        journal(task, [edited(path=name, before=b"value = 1\n", after=b"value = 2\n")])

        document = review.build_review(task, repo, width=100)
        lines = review.review_lines(document, expanded=[name])

        assert any(name in line for line in lines), "the path must be visible"
        receipt = review.revert_paths(task, repo, [name])
        assert any(name in line for line in review.render_receipt(receipt))

    def test_the_highlighted_path_returns_text_not_markup(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        document = review.build_review(task, repo, width=100)

        highlighted = review.review_text_lines(document, expanded=["src/app.py"])

        try:
            from rich.text import Text
        except Exception:  # pragma: no cover - rich is a declared dependency
            pytest.skip("rich is not installed")
        assert highlighted, "the highlighted path must not silently return nothing"
        assert all(isinstance(item, Text) for item in highlighted)

    def test_every_rendered_line_fits_the_width_it_was_asked_for(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path, "agent-wide")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        document = review.build_review(task, repo, width=72)

        lines = review.review_lines(document, expanded=["src/app.py"])

        assert lines
        assert max(len(line) for line in lines) <= 72


class TestTheLiveIndicatorMakesScopeCreepVisible:
    def test_the_indicator_sees_every_path_that_differs(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        write(repo, "src/other.py", b"other = 1\n")
        journal(task, [edited()])

        indicator = review.changed_files_indicator(task, repo)

        assert set(indicator["changed"]) == {"src/app.py", "src/other.py"}

    def test_a_path_the_run_changed_but_never_planned_is_scope_creep(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        write(repo, "src/scope.py", b"creep = 1\n")
        journal(
            task,
            [
                {
                    "event": "plan",
                    "payload": {
                        "plan": [
                            {
                                "id": 1,
                                "description": "fix",
                                "checkpoint": "t",
                                "files_hint": ["src/app.py"],
                            }
                        ]
                    },
                },
                edited(),
                {
                    "event": "edit_applied",
                    "payload": {"path": "src/scope.py", "pre_sha256": sha(b"")},
                },
            ],
        )

        indicator = review.changed_files_indicator(task, repo)

        assert indicator["unplanned"] == ("src/scope.py",)
        assert indicator["scope_creep"] is True

    def test_a_run_with_no_plan_makes_no_scope_creep_claim(
        self, tmp_path: Path
    ) -> None:
        """ "The plan did not mention it" is a claim about a plan."""
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        indicator = review.changed_files_indicator(task, repo)

        assert indicator["unplanned"] == ()
        assert indicator["scope_creep"] is False

    def test_a_teammates_file_is_unevidenced_not_the_runs_work(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        write(repo, "src/theirs.py", b"theirs = 1\n")
        journal(task, [edited()])

        indicator = review.changed_files_indicator(task, repo)

        assert "src/theirs.py" in indicator["unevidenced"]
        assert "src/theirs.py" not in indicator["run_changed"]

    def test_resampling_reports_what_appeared_and_what_resolved(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        first = review.changed_files_indicator(task, repo)

        write(repo, "src/app.py", BASE_FILES["src/app.py"])
        write(repo, "src/second.py", b"second = 1\n")
        second = review.changed_files_indicator(task, repo, previous=first)

        assert second["new_paths"] == ("src/second.py",)
        assert second["resolved_paths"] == ("src/app.py",)

    def test_a_cheap_sample_says_which_checks_it_skipped_and_never_calls_them_passed(
        self, tmp_path: Path
    ) -> None:
        """A bound that reports a skipped check as a passed one is a lie.

        The live indicator skips two O(the whole run) checks so a frame
        budget can hold it. The document has to carry that as `unchecked`,
        never as `ok` - and the thorough build on the same tree still finds
        the tampering, which is what makes `unchecked` a scope statement
        rather than a missed bug.
        """
        repo, task = make_run(tmp_path, "agent-cheap")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        (task / "pristine/src/app.py").write_bytes(b"attacker = 0\n")

        indicator = review.changed_files_indicator(task, repo)
        thorough = review.build_review(task, repo, width=100)

        assert indicator["snapshot_state"] == "unchecked"
        assert thorough.snapshot.state == "corrupt"

    def test_a_bounded_indicator_reports_that_it_is_bounded(
        self, tmp_path: Path
    ) -> None:
        """A quiet cap would read as "the run is done"."""
        repo, task = make_run(tmp_path, "agent-bound")
        for index in range(6):
            name = f"src/mod{index}.py"
            write(repo, name, f"x = {index}\n".encode())
            write(task / "pristine", name, f"x = {index}\n".encode())
            write(repo, name, f"x = {index} + 1\n".encode())
            journal(
                task,
                [
                    edited(
                        path=name,
                        before=f"x = {index}\n".encode(),
                        after=f"x = {index} + 1\n".encode(),
                    )
                ],
            )

        indicator = review.changed_files_indicator(task, repo, max_files=3)

        assert indicator["truncated"] is True
        assert indicator["max_files"] == 3
        assert len(indicator["changed"]) <= 3


class TestTheBlastRadiusIsASurveyAndNotASpy:
    def test_reads_writes_and_commands_are_three_separate_questions(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        journal(
            task,
            [
                {
                    "event": "tool_call",
                    "payload": {"tool": "read", "arguments": {"path": "src/keep.py"}},
                },
                {
                    "event": "tool_call",
                    "payload": {"tool": "edit", "arguments": {"path": "src/app.py"}},
                },
                {"event": "tool_call", "payload": {"command": "pytest tests -q"}},
            ],
        )

        radius = review.blast_radius(task, repo)

        assert radius["files_read"] == ("src/keep.py",)
        assert radius["files_written"] == ("src/app.py",)
        assert radius["counts"]["commands"] == 1
        assert radius["read_only"] == ("src/keep.py",)

    def test_a_command_naming_a_path_outside_the_repository_is_flagged(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        journal(
            task, [{"event": "tool_call", "payload": {"command": "cat /etc/hosts"}}]
        )

        radius = review.blast_radius(task, repo)

        assert radius["counts"]["commands_outside_repository"] == 1
        assert radius["commands"][0]["outside"] is True

    def test_the_detection_is_labelled_as_a_lower_bound(self, tmp_path: Path) -> None:
        """A command can reach anywhere without naming a path.

        Claiming otherwise would be an invented finding, and an invented
        "this ran outside your repository" is a receipt nobody can act on.
        """
        repo, task = make_run(tmp_path)
        journal(
            task,
            [
                {
                    "event": "tool_call",
                    "payload": {"command": "curl https://example.invalid"},
                }
            ],
        )

        radius = review.blast_radius(task, repo)

        assert radius["detection"] == "recorded_command_text"
        assert radius["commands"][0]["outside"] is False
        assert "lower bound" in radius["reason"]


class TestTheLayoutIsChosenAndExplained:
    @pytest.mark.parametrize(
        ("preference", "width", "expected"),
        [
            ("auto", 200, "split"),
            ("auto", 100, "split"),
            ("auto", 99, "stacked"),
            ("auto", 80, "stacked"),
            ("stacked", 200, "stacked"),
        ],
    )
    def test_the_layout_follows_the_terminal_unless_it_is_told_otherwise(
        self, preference: str, width: int, expected: str
    ) -> None:
        resolved, reason = review.resolve_diff_style(preference, width)

        assert resolved == expected
        assert reason, "a layout that changed must be able to say why"

    @pytest.mark.parametrize("width", [0, -1, None, "wide"])
    def test_an_unmeasurable_width_resolves_to_the_layout_that_cannot_break(
        self, width: Any
    ) -> None:
        assert review.resolve_diff_style("auto", width)[0] == "stacked"

    def test_an_unrecognised_preference_degrades_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        document = review.build_review(
            task, repo, config={"review_diff_style": "sideways"}, width=200
        )

        assert document.resolved_style == "split"
        assert "unrecognised" in document.style_reason
        assert document.diff_style == "sideways", "the document echoes what was ASKED"

    def test_the_config_key_is_read_by_presence_and_is_not_a_global_default(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        default = review.build_review(task, repo, width=200)
        explicit = review.build_review(
            task, repo, config={"review_diff_style": "stacked"}, width=200
        )

        assert default.diff_style == "auto"
        assert explicit.diff_style == "stacked"
        assert explicit.resolved_style == "stacked"
        from harness import config as harness_config

        assert not any(key.startswith("review_") for key in harness_config.DEFAULTS)


class TestWhichTreeIsActedOn:
    def test_a_work_copy_run_never_touches_the_live_repository(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path, "agent-work")
        for path, data in BASE_FILES.items():
            write(task / "work", path, data)
        write(task / "work", "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        document = review.build_review(task, repo, width=100)
        receipt = review.revert_paths(task, repo, ["src/app.py"])

        assert document.tree == "work"
        assert "was not modified" in document.tree_note
        assert receipt.tree == "work"
        assert (task / "work/src/app.py").read_bytes() == BASE_FILES["src/app.py"]
        assert (repo / "src/app.py").read_bytes() == BASE_FILES["src/app.py"]

    def test_the_receipt_says_which_tree_it_wrote_into(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path, "agent-live")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        receipt = review.revert_paths(task, repo, ["src/app.py"])
        rendered = "\n".join(review.render_receipt(receipt))

        assert "your working tree" in rendered
        assert receipt.tree == "live"


class TestTheDispatcherIsTheOnePlaceTheseWordsAreSpoken:
    @pytest.mark.parametrize("verb", ["show", "accept", "reject", "revert"])
    def test_every_advertised_verb_is_dispatched(
        self, tmp_path: Path, verb: str
    ) -> None:
        repo, task = make_run(tmp_path, f"agent-{verb}")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        result = review.review_command(task, repo, "all", verb=verb)

        assert result["handled"] is True, f"/diff {verb} is advertised and must run"
        assert result["lines"], f"/diff {verb} must say something"

    def test_the_verb_vocabulary_is_closed_and_does_not_take_a_live_word(
        self, tmp_path: Path
    ) -> None:
        """A bare `/diff all` already means "revert everything".

        Taking that word for a read-only roster would turn a destructive
        command into a display one, and `show` already lists every file.
        """
        repo, task = make_run(tmp_path, "agent-vocab")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        assert "all" not in review.diff_review_verbs()
        assert review.review_command(task, repo, "", verb="all")["handled"] is False
        # ...while `all` remains a perfectly good ARGUMENT.
        assert review.review_command(task, repo, "all", verb="show")["handled"] is True

    @pytest.mark.parametrize("verb", ["undo", "all", "restore", "checkout", "reset"])
    def test_a_verb_the_historical_engine_owns_is_handed_back(
        self, tmp_path: Path, verb: str
    ) -> None:
        """`/diff undo` belongs to the historical engine and must stay there."""
        repo, task = make_run(tmp_path, f"agent-hist-{verb}")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        result = review.review_command(task, repo, "", verb=verb)

        assert result["handled"] is False
        assert (repo / "src/app.py").read_bytes() == b"value = 2\n"

    def test_the_mutating_verbs_are_refused_mid_run_and_the_read_only_ones_are_not(
        self,
    ) -> None:
        """One policy cannot express "read it now, write it later"."""
        from cli import commands as commands_mod

        spec = commands_mod.command_spec("/diff")
        read_only = commands_mod.resolve_command_line(
            "/diff show", commands_mod.CommandContext(surface="tui", in_flight=True)
        )
        writing = commands_mod.resolve_command_line(
            "/diff revert all",
            commands_mod.CommandContext(surface="tui", in_flight=True),
        )

        assert read_only.status == "ok"
        assert writing.status != "ok"
        assert "active" in str(writing.message or writing.args or "")
        assert spec is not None

    def test_bare_diff_shows_the_review(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        result = review.review_command(task, repo, "")

        assert result["kind"] == "show"
        assert any("src/app.py" in line for line in result["lines"])

    def test_a_live_run_refuses_to_restore(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])

        result = review.review_command(task, repo, "all", verb="revert", in_flight=True)

        assert result["ok"] is False
        assert (repo / "src/app.py").read_bytes() == b"value = 2\n"
        assert any("in flight" in line or "refused" in line for line in result["lines"])


class TestUnusableInputIsAValueAndNeverAnException:
    @pytest.mark.parametrize(
        "task_dir", [None, "", 123, "nope", "C:/nope", "../escape"]
    )
    @pytest.mark.parametrize("repo", [None, "", 123, "nope"])
    def test_every_entry_point_is_total(
        self, tmp_path: Path, task_dir: Any, repo: Any
    ) -> None:
        assert review.build_review(task_dir, repo).reviewable is True
        assert review.snapshot_health(task_dir, repo).state in {
            "ok",
            "missing",
            "corrupt",
            "degraded",
        }
        assert isinstance(
            review.review_lines(review.build_review(task_dir, repo)), list
        )
        assert isinstance(review.blast_radius(task_dir, repo), dict)
        assert isinstance(review.changed_files_indicator(task_dir, repo), dict)
        assert review.revert_paths(task_dir, repo, ["src/app.py"]).ok is False
        assert (
            review.review_command(task_dir, repo, "all", verb="revert")["handled"]
            is True
        )

    def test_a_traversing_path_is_refused_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path)
        outside = task.parent / "escape.py"
        outside.write_bytes(b"untouched\n")

        receipt = review.revert_paths(task, repo, ["../escape.py"])

        assert receipt.status == "nothing"
        assert outside.read_bytes() == b"untouched\n"

    def test_a_torn_journal_does_not_stop_the_review(self, tmp_path: Path) -> None:
        """Mid-run review is the normal case, and the tail is half-written."""
        repo, task = make_run(tmp_path)
        write(repo, "src/app.py", b"value = 2\n")
        with (task / "trace.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(edited()) + "\n")
            handle.write('{"event": "edit_app')

        document = review.build_review(task, repo, width=100)

        assert any(item.path == "src/app.py" for item in document.files)

    def test_a_corrupt_receipt_reads_as_no_decision_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        _, task = make_run(tmp_path)
        (task / review.REVIEW_RECEIPT_NAME).write_text("{not json", encoding="utf-8")

        assert review.read_review_receipt(task) == {}
        assert review.decisions(task) == {}


class TestTheAntiClutterRuleAndItsOneExemption:
    def test_a_single_note_is_not_rendered_as_a_section(self, tmp_path: Path) -> None:
        repo, task = make_run(tmp_path, "agent-clutter")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        document = review.build_review(task, repo, width=100)

        lines = review.review_lines(document)

        assert not any(line.startswith("note:") for line in lines)

    def test_a_one_file_review_still_renders_its_roster(self, tmp_path: Path) -> None:
        """The roster is the review's subject, and the documented exemption.

        A one-file review that rendered nothing would be a broken surface,
        not a tidy one.
        """
        repo, task = make_run(tmp_path, "agent-single")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        document = review.build_review(task, repo, width=100)

        lines = review.review_lines(document, expanded=["src/app.py"])

        assert any("src/app.py" in line for line in lines)

    def test_the_roster_is_collapsed_until_the_file_is_asked_for(
        self, tmp_path: Path
    ) -> None:
        repo, task = make_run(tmp_path, "agent-collapse")
        write(repo, "src/app.py", b"value = 2\n")
        journal(task, [edited()])
        document = review.build_review(task, repo, width=100)

        collapsed = review.review_lines(document)
        expanded = review.review_lines(document, expanded=["src/app.py"])

        assert len(expanded) > len(collapsed)
