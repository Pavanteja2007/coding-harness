"""VEX-TERM-UX-05 round 2 — files, diffs, diagnostics, context, checkpoints.

One test per required behaviour, named after the behaviour rather than the
function. The four that matter most are the honesty ones: a per-file
``verified`` claim that could promote a FAILED run, a language-server call
that raised a ``TypeError`` behind a bare ``except: pass``, a relevant-files
list that was twelve arbitrary files, and diagnostics that rendered in the
same visual language as model output.

Offline and deterministic: every test builds its own repository and its own
run journal under ``tmp_path``, and never reads the developer's real
``logs/``, never contacts a provider, and never needs Docker.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli import a11y, commands, fileview

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "repo"
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def _projection(tmp_path: Path, **overrides):
    root = _repo(
        tmp_path,
        {
            "cli/alpha.py": "def alpha():\n    return 1\n",
            "cli/beta.py": "def beta():\n    return 2\n",
        },
    )
    snapshot = {
        "task_id": "fix-test",
        "status": "failed",
        "verification_state": "failed",
    }
    snapshot.update(overrides)
    return fileview.build_file_projection(
        None, root, snapshot=snapshot, include_git=False
    )


def _diff_record() -> dict:
    return {
        "path": "cli/alpha.py",
        "status": "modified",
        "hunks": [
            {
                "index": 1,
                "header": "@@ -1,3 +1,4 @@",
                "old_start": 1,
                "old_count": 3,
                "new_start": 1,
                "new_count": 4,
                "lines": [" ctx", "-old one", "+new one", "+new two"],
                "line_numbers": [1, 1, 1, 2],
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. The verifier gate: a per-file claim cannot promote a failed run
# ---------------------------------------------------------------------------


class TestFileChangeHonesty:
    @pytest.mark.parametrize(
        "run_state",
        ["failed", "error", "flaky", "not_run", "unknown", "unverified", "pending", ""],
    )
    def test_a_file_claim_cannot_promote_an_unverified_run(self, tmp_path, run_state):
        """A journal row claiming `verified: true` may not upgrade the run.

        This is the R2-G45 class in a new place. The projection read
        `meta["verified"]` FIRST and only then consulted the run's own state,
        so a single row could render a file as verified while the run was
        `failed` — and the guard written right below it could only ever
        assign False to something already False. A gate that cannot fail is
        worse than no gate, because it reads as one.
        """
        projection = fileview.build_file_projection(
            None,
            _repo(tmp_path, {"cli/alpha.py": "x = 1\n"}),
            snapshot={
                "task_id": "fix-test",
                "verification_state": run_state,
                "file_changes": [{"path": "cli/alpha.py", "verified": True}],
            },
            include_git=False,
        )
        record = projection["file_changes"][0]
        assert record["verified"] is False
        assert record["verification_state"] == (run_state or "not_run")
        assert projection["totals"]["verified"] == 0

    def test_a_verified_run_verifies_its_files(self, tmp_path):
        projection = fileview.build_file_projection(
            None,
            _repo(tmp_path, {"cli/alpha.py": "x = 1\n"}),
            snapshot={
                "task_id": "fix-test",
                "verification_state": "verified",
                "file_changes": [{"path": "cli/alpha.py"}],
            },
            include_git=False,
        )
        record = projection["file_changes"][0]
        assert record["verified"] is True
        assert record["verification_state"] == "verified"
        assert projection["totals"]["verified"] == 1

    def test_a_truncated_view_never_claims_verified(self, tmp_path):
        """A bounded view cannot vouch for the lines it dropped."""
        assert fileview.file_change_verified("verified", True, truncated=True) is False
        assert fileview.file_change_verified("verified", None, truncated=True) is False
        assert fileview.file_change_verified("verified", None, truncated=False) is True

    def test_the_authority_is_total_and_fails_closed(self):
        """Every non-`verified` run state denies, including nonsense input."""
        for value in (None, "", "  ", "VERIFIED!", 1, True, [], {}):
            assert fileview.file_change_verified(value) is False
        assert fileview.file_change_verified("verified") is True
        # a claim can only CONFIRM, never create
        assert fileview.file_change_verified("verified", "yes") is True
        assert fileview.file_change_verified("verified", "no") is False
        assert fileview.file_change_verified("failed", "yes") is False

    def test_every_file_change_carries_all_five_attributions(self, tmp_path):
        """The prompt's five questions are answerable for every file.

        who / why / verified / undoable / which-checkpoint. A record missing
        one is a change the surface cannot describe, so the test walks the
        product's own table rather than a local copy of it.
        """
        projection = _projection(
            tmp_path,
            file_changes=[
                {
                    "path": "cli/alpha.py",
                    "actor": "agent",
                    "reason": "fixes the divisor",
                }
            ],
        )
        assert len(fileview.ATTRIBUTION_FIELDS) == 5
        for record in projection["file_changes"]:
            for field in fileview.ATTRIBUTION_FIELDS:
                assert field in record, f"{record['path']} is missing {field}"
        record = projection["file_changes"][0]
        assert record["actor"] == "agent"
        assert record["reason"] == "fixes the divisor"
        assert record["verified"] is False
        assert record["undoable"] is False
        assert record["checkpoint_ids"] == []

    def test_every_renderer_states_the_attribution(self, tmp_path):
        """The TUI and the REPL render the same five facts, from one producer.

        The diff browser used to print the actor and nothing else, so three
        of the five answers were absent from the surface a person reads
        first. Both shells now build the fragment from the record's
        fail-closed flag rather than re-deriving a verdict.
        """
        from cli import interactive, tui

        record = {
            "path": "cli/alpha.py",
            "actor": "agent",
            "reason": "fixes the divisor",
            "verification_state": "unverified",
            "undoable": False,
            "checkpoint_ids": ["cp-1", "cp-2"],
        }
        tui_text = tui._fv_attribution_text(record)
        repl_text = interactive.file_change_attribution(record)
        for needle in ("agent", "fixes the divisor", "unverified", "no", "cp-1"):
            assert needle in tui_text, needle
            assert needle in repl_text, needle
        # both name the verification STATE, so a reader never sees a bare
        # "yes" that could be mistaken for success
        assert "unverified" in tui_text and "unverified" in repl_text


# ---------------------------------------------------------------------------
# 2. Line-level diff navigation
# ---------------------------------------------------------------------------


class TestLineLevelDiffNavigation:
    @pytest.mark.parametrize(
        "text,expected",
        [
            (
                "cli/tui.py",
                {"path": "cli/tui.py", "hunk": None, "line": None, "column": None},
            ),
            (
                "cli/tui.py#2",
                {"path": "cli/tui.py", "hunk": 2, "line": None, "column": None},
            ),
            (
                "cli/tui.py:340",
                {"path": "cli/tui.py", "hunk": None, "line": 340, "column": None},
            ),
            (
                "cli/tui.py:340:9",
                {"path": "cli/tui.py", "hunk": None, "line": 340, "column": 9},
            ),
            ("x.py#2:5", {"path": "x.py", "hunk": 2, "line": 5, "column": None}),
            ("a#b.py", {"path": "a#b.py", "hunk": None, "line": None, "column": None}),
            ("", {"path": "", "hunk": None, "line": None, "column": None}),
        ],
    )
    def test_the_four_typed_forms_parse(self, text, expected):
        """`path`, `path#hunk`, `path:line`, `path:line:column`.

        The transposition case is the load-bearing one: consuming the numeric
        suffixes left-to-right turns `f.py:340:9` into line 9, which sends a
        reader to the wrong place in a 4000-line file with no error at all.
        """
        assert fileview.parse_diff_target(text) == expected

    def test_a_windows_drive_letter_is_not_a_line_suffix(self):
        """`C:\\src\\x.py` is a path, not `C` and a line number."""
        result = fileview.parse_diff_target("C:/src/x.py")
        assert result["line"] is None
        result = fileview.parse_diff_target("C:/src/x.py:12")
        assert result["line"] == 12
        assert result["path"] == ""  # refused: outside the repository

    def test_a_line_address_resolves_its_hunk_and_row(self):
        projection = {"diff": {"files": [_diff_record()]}}
        result = fileview.open_diff_line(projection, "cli/alpha.py", 2)
        assert result is not None
        assert result["selected_line"] == 2
        assert result["selected_offset"] == 3
        assert result["selected_line_kind"] == "added"
        assert result["selected_hunk"]["index"] == 1

    def test_a_line_outside_the_diff_says_so_rather_than_guessing(self):
        """Landing on a neighbour is worse than saying the line is absent."""
        projection = {"diff": {"files": [_diff_record()]}}
        result = fileview.open_diff_line(projection, "cli/alpha.py", 9999)
        assert result["selected_line"] == 9999
        assert result["selected_line_kind"] == "outside_diff"
        assert result["selected_offset"] is None

    def test_a_context_line_is_not_reported_as_added(self):
        """Line 1 of the new file is a context line here, not an addition.

        A new-file line number is shared by a context line and the removal
        that replaced it, so the KIND has to come from the line's prefix.
        Calling a context line "added" tells a reader the run wrote something
        it did not.
        """
        projection = {"diff": {"files": [_diff_record()]}}
        result = fileview.open_diff_line(projection, "cli/alpha.py", 1)
        assert result["selected_line_kind"] == "context"

    def test_the_change_index_walks_only_added_and_removed_lines(self):
        steps = fileview.changed_line_index(_diff_record())
        assert [step["side"] for step in steps] == ["deleted", "added", "added"]
        assert [step["line"] for step in steps] == [1, 1, 2]
        assert all(step["hunk"] == 1 for step in steps)

    def test_an_unknown_file_resolves_to_nothing(self):
        assert fileview.open_diff_line({"diff": {"files": []}}, "nope.py", 1) is None


# ---------------------------------------------------------------------------
# 3. The relevant-files view
# ---------------------------------------------------------------------------


class TestRelevantFilesView:
    def test_rows_state_why_they_are_relevant(self):
        """A list of files with no stated basis is a worse `/files`."""
        projection = {
            "task_id": "fix-test",
            "repo": ".",
            "changed_files": ["cli/alpha.py"],
            "file_changes": [
                {"path": "cli/alpha.py", "staged": True},
                {"path": "cli/beta.py", "staged": False},
            ],
            "sources": ["cli/alpha.py", "docs/design.md"],
            "relevant_files": ["cli/alpha.py"],
            "diagnostics": [{"path": "cli/beta.py"}],
        }
        rows = fileview.relevant_file_rows(projection)
        by_path = {row["path"]: row for row in rows}
        assert "changed in this run" in by_path["cli/alpha.py"]["reason"]
        assert "staged for commit" in by_path["cli/alpha.py"]["reason"]
        assert "carries a diagnostic" in by_path["cli/beta.py"]["reason"]
        assert "docs/design.md" in by_path
        for row in rows:
            assert row["reason"], "every row states why"
        # a file carrying a diagnostic LEADS: that is the file a reader needs,
        # and the ordering is declared in the product's own strength table
        assert rows[0]["path"] == "cli/beta.py"
        assert rows[0]["rank"] == 1
        assert [row["rank"] for row in rows] == list(range(1, len(rows) + 1))

    def test_ranking_is_deterministic_and_ranks_are_one_based(self):
        projection = {
            "task_id": "t",
            "changed_files": ["b.py", "a.py"],
            "file_changes": [{"path": "b.py"}, {"path": "a.py"}],
            "sources": ["a.py", "b.py"],
            "relevant_files": ["a.py", "b.py"],
        }
        first = fileview.relevant_file_rows(projection)
        second = fileview.relevant_file_rows(projection)
        assert first == second
        assert [row["rank"] for row in first] == list(range(1, len(first) + 1))

    def test_a_file_earning_no_row_is_not_invented(self, tmp_path):
        """No cited context and no changes: say so rather than pad a list.

        The projection's own `relevant_files` key is a
        `_walk_repository_files(root, 12)` FALLBACK whenever a run cited
        nothing, so a NON-EMPTY repository makes the "is that a relevance
        claim?" question real. Reading it as one phrased a claim nobody made
        for twelve arbitrary files in directory order.
        """
        projection = {
            "task_id": "t",
            "repo": str(tmp_path),
            "changed_files": [],
            "file_changes": [],
            "sources": [],
            # the fallback: twelve arbitrary files, no citation behind them
            "relevant_files": [f"pkg/mod{i}.py" for i in range(12)],
        }
        assert fileview.relevant_file_rows(projection) == []
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod0.py").write_text("x = 1\n", encoding="utf-8")
        assert fileview.relevant_file_rows(projection) == []

    def test_a_git_only_change_is_not_called_this_runs_change(self, tmp_path):
        """`git status` on a shared tree reports OTHER people's files.

        Every uncommitted file in a working tree several people are editing
        shows up in `git status`. Attributing those to "this run" names a
        cause that did not happen, and it is the same class of lie as calling
        an unverified result verified.

        The mechanism is the DIFF SOURCE, not a label in the journal: a file
        whose only evidence is `git status` has source `git`, and that is
        what makes the actor `workspace`. So this drives a real repository
        (staged, never committed — the round forbids committing) rather than
        a synthetic snapshot.
        """
        root = tmp_path / "r"
        root.mkdir()
        _git(root, "init", "-q")
        (root / "cli").mkdir()
        (root / "cli" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
        _git(root, "add", "cli/alpha.py")
        projected = fileview.build_file_projection(None, root, include_git=True)
        record = projected["file_changes"][0]
        assert record["source"] == "git"
        assert record["actor"] == "workspace"
        rows = {row["path"]: row for row in fileview.relevant_file_rows(projected)}
        assert "not this run's change" in rows["cli/alpha.py"]["reason"]

    def test_a_journal_sourced_change_is_called_this_runs_change(self, tmp_path):
        """The counterpart: a file the RUN's own artifacts attribute."""
        projection = {
            "task_id": "fix-x",
            "changed_files": ["cli/alpha.py", "cli/beta.py"],
            "file_changes": [
                {"path": "cli/alpha.py", "source": "git"},
                {"path": "cli/beta.py", "source": "workspace"},
            ],
            "sources": [],
            "relevant_files": ["cli/alpha.py", "cli/beta.py"],
        }
        rows = {row["path"]: row for row in fileview.relevant_file_rows(projection)}
        assert "not this run's change" in rows["cli/alpha.py"]["reason"]
        assert "changed in this run" in rows["cli/beta.py"]["reason"]

    def test_an_agent_run_still_attributes_to_the_agent(self, tmp_path):
        """The workspace actor must not swallow a real agent run's edits."""
        root = tmp_path / "r"
        root.mkdir()
        _git(root, "init", "-q")
        (root / "cli").mkdir()
        (root / "cli" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
        _git(root, "add", "cli/alpha.py")
        projected = fileview.build_file_projection(
            None,
            root,
            snapshot={"task_id": "agent-abc"},
            include_git=True,
        )
        assert projected["file_changes"][0]["actor"] == "agent"

    def test_a_query_narrows_by_path_and_by_reason(self):
        projection = {
            "task_id": "t",
            "changed_files": ["cli/alpha.py", "cli/beta.py"],
            "file_changes": [{"path": "cli/alpha.py"}, {"path": "cli/beta.py"}],
            "sources": [],
            "relevant_files": ["cli/alpha.py", "cli/beta.py"],
        }
        assert [
            r["path"] for r in fileview.relevant_file_rows(projection, "alpha")
        ] == ["cli/alpha.py"]
        assert fileview.relevant_file_rows(projection, "staged for commit") == []

    def test_the_command_is_registered_and_dispatchable(self):
        spec = commands.command_spec("/relevant")
        assert spec is not None
        assert spec.result_presentation == "browser"
        assert commands.headless_policy("/relevant") == "mapped"
        labels = [entry.get("value") for entry in commands.command_palette_entries()]
        assert "/relevant" in labels
        assert commands.resolve_command_line("/relevant").spec.name == "/relevant"

    def test_the_command_is_in_the_alias_resolution_path(self):
        assert commands.command_spec("/related") is not None


# ---------------------------------------------------------------------------
# 4. Diagnostics: provenance, distinctness, real links, and an honest receipt
# ---------------------------------------------------------------------------


class TestDiagnosticsPanel:
    def test_the_live_language_server_call_is_reachable(self, tmp_path, monkeypatch):
        """The live path raised `TypeError` on every attempt, swallowed.

        `LspManager.from_config(config, cwd=repo)` — the second parameter is
        named `repo_path`, so the call raised on the first try in every
        session, the `except Exception: pass` hid it, and live diagnostics
        NEVER reached `/diagnostics`. This drives the real call with a fake
        manager whose constructor records its keyword arguments.
        """
        captured = {}

        class _Manager:
            def __init__(self, config, repo_path=None):
                captured["repo_path"] = repo_path

            @classmethod
            def from_config(cls, config, repo_path=None):
                return cls(config, repo_path=repo_path)

            def start(self):
                return True

            def close(self):
                captured["closed"] = True

        class _Item:
            def __init__(self, payload):
                self._payload = payload

            def to_dict(self):
                return self._payload

        monkeypatch.setattr(
            "harness.lsp.LspManager",
            _Manager,
            raising=False,
        )
        monkeypatch.setattr(
            "harness.lsp.get_diagnostics",
            lambda manager: [
                _Item(
                    {
                        "file": "cli/alpha.py",
                        "line": 4,
                        "column": 0,
                        "message": "undefined name",
                        "severity": "error",
                    }
                )
            ],
            raising=False,
        )
        root = _repo(tmp_path, {"cli/alpha.py": "x = 1\n"})
        report = fileview.lsp_state_report(root, {"command": "fake-lsp"})
        assert report["available"] is True
        assert report["state"] == "live"
        assert report["count"] == 1
        assert captured["repo_path"] is not None
        assert captured["closed"] is True
        rows = fileview.lsp_diagnostics(root, {"command": "fake-lsp"})
        assert rows and rows[0]["provenance"] == "lsp"

    def test_the_receipt_names_which_of_three_facts_it_is(self, tmp_path):
        """Not configured, unusable, and unavailable are three sentences.

        The old `pass` made all three render as "no diagnostics available",
        which is indistinguishable from a CLEAN workspace.
        """
        root = _repo(tmp_path, {"cli/alpha.py": "x = 1\n"})
        assert fileview.lsp_state_report(root)["state"] == "not_configured"
        (root / ".neo").mkdir(exist_ok=True)
        (root / ".neo" / "lsp.json").write_text("{not json", encoding="utf-8")
        assert fileview.lsp_state_report(root)["state"] == "unreadable_config"
        (root / ".neo" / "lsp.json").write_text("{}", encoding="utf-8")
        assert fileview.lsp_state_report(root)["state"] == "unreadable_config"

    def test_a_journal_row_cannot_relabel_itself_as_live(self):
        """Provenance is derived from the SHAPE, never from a label."""
        assert fileview.diagnostic_provenance({"path": "a.py", "line": 1}) == "journal"
        assert (
            fileview.diagnostic_provenance({"file": "a.py", "line": 1, "column": 1})
            == "lsp"
        )
        assert (
            fileview.diagnostic_provenance(
                {"path": "a.py", "line": 1, "provenance": "lsp"}
            )
            == "journal"
        )
        assert set(fileview.DIAGNOSTIC_PROVENANCE) == {"journal", "lsp"}

    def test_a_diagnostic_links_to_the_affected_file_and_line(self):
        row = fileview.normalize_diagnostic(
            {"path": "cli/alpha.py", "line": 12, "column": 3, "message": "boom"}
        )
        assert row["link"] == "cli/alpha.py:12:3"
        assert "cli/alpha.py:12:3" in a11y.diagnostic_row(row)

    def test_live_rows_are_collected_once_not_twice(self, tmp_path, monkeypatch):
        """A configured language server must not be queried twice.

        `diagnostic_rows` used to call `_diagnostic_lines(log_root, repo,
        None)` in the no-task branch — which already appends live LSP rows —
        and then append `lsp_diagnostics(repo)` as well. With a server
        configured, every finding was collected, started, and appended
        twice, and the start-up cost was paid twice on a worker thread.
        """
        calls = {"n": 0}

        def _fake(repo=None, config=None):
            calls["n"] += 1
            return [
                fileview.normalize_diagnostic(
                    {"file": "cli/alpha.py", "line": 1, "column": 0, "message": "x"},
                    repo=repo,
                )
            ]

        monkeypatch.setattr(fileview, "lsp_diagnostics", _fake)
        repo = _repo(tmp_path, {"cli/alpha.py": "x = 1\n"})
        rows = fileview.diagnostic_rows(repo, None, None, log_root=tmp_path / "logs")
        assert calls["n"] == 1
        assert len(rows) == 1

    def test_a_hunk_and_a_line_resolve_together(self):
        """`file#2:340` names a hunk AND a line; both must take effect.

        Resolving only the hunk leaves `selected_line` absent, so the cursor
        seats on the hunk's FIRST change and the line argument is silently
        ignored — an address that resolves to the wrong place without saying
        so.
        """
        projection = {
            "diff": {
                "files": [
                    {
                        "path": "a.py",
                        "hunks": [
                            {
                                "index": 1,
                                "header": "@@ -1 +1 @@",
                                "old_start": 1,
                                "old_count": 1,
                                "new_start": 1,
                                "new_count": 1,
                                "lines": ["+one"],
                                "line_numbers": [1],
                            },
                            {
                                "index": 2,
                                "header": "@@ -10 +10 @@",
                                "old_start": 10,
                                "old_count": 1,
                                "new_start": 10,
                                "new_count": 1,
                                "lines": ["+ten"],
                                "line_numbers": [10],
                            },
                        ],
                    }
                ]
            }
        }
        target = fileview.parse_diff_target("a.py#2:10")
        assert target["hunk"] == 2 and target["line"] == 10
        result = fileview.open_diff_line(projection, "a.py", target["line"])
        assert result["selected_line"] == 10
        assert result["selected_hunk"]["index"] == 2
        assert result["selected_line_kind"] == "added"

    def test_choosing_a_diagnostic_reaches_the_composer(self, tmp_path):
        """The prompt requires a link, not a list.

        `_DiagnosticsScreen` was pushed WITHOUT its `_diagnostic_chosen`
        callback, so selecting a row closed the modal and did nothing: a
        panel that shows a `path:line:column` and cannot take you there is
        the requirement unmet, and the bug was invisible until a real app was
        driven.
        """
        import asyncio

        from cli import tui

        async def _drive():
            repo = _repo(tmp_path, {"cli/alpha.py": "x = 1\n"})
            logs = tmp_path / "logs"
            logs.mkdir(exist_ok=True)
            app = tui.NeoApp(
                repo=str(repo),
                log_root=str(logs),
                state={"repo": str(repo), "file_config": {}},
                file_config={},
            )
            async with app.run_test(size=(200, 50)) as pilot:
                await pilot.pause()
                row = fileview.normalize_diagnostic(
                    {
                        "path": "cli/alpha.py",
                        "line": 2,
                        "column": 5,
                        "message": "boom",
                        "severity": "error",
                    },
                    repo=repo,
                )
                app._diagnostic_chosen(row)
                return str(app.query_one("#neo-input").value)

        assert "@cli/alpha.py:2" in asyncio.run(_drive())

    def test_the_diagnostics_panel_keeps_its_link_callback(self):
        """The wiring itself, readable without booting the whole app."""
        import inspect

        from cli import tui

        source = inspect.getsource(tui.NeoApp._diagnostics_done)
        assert "_diagnostic_chosen" in source
        assert "_DiagnosticsScreen(values, receipt)" in source

    def test_the_diagnostics_panel_opens_when_there_IS_a_diagnostic(self, tmp_path):
        """The panel used to open only when it had nothing to show.

        `_diagnostics_done` announced with `self._announce(sentence)` while
        `_announce` is `(key, text)`. The `TypeError` was raised inside the
        worker-thread callback, BEFORE `push_screen`, so the panel never
        opened whenever diagnostics existed — and the empty path returns
        before the announce, so it "worked" precisely when there was nothing
        to show. Found by driving the real app; reading the code did not
        show it, because both call sites look plausible.
        """
        import asyncio

        from cli import tui

        root = _repo(tmp_path, {"cli/alpha.py": "def alpha():\n    return 1\n"})
        logs = tmp_path / "logs"
        task = logs / "fix-a11y"
        task.mkdir(parents=True)
        (task / "trace.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-09-28T00:00:00Z",
                    "kind": "diagnostics",
                    "data": {
                        "diagnostics": [
                            {
                                "path": "cli/alpha.py",
                                "line": 2,
                                "column": 5,
                                "severity": "error",
                                "message": "boom",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        async def _drive():
            app = tui.NeoApp(
                repo=str(root),
                log_root=str(logs),
                state={"repo": str(root), "file_config": {}},
                file_config={},
            )
            async with app.run_test(size=(200, 50)) as pilot:
                await pilot.pause()
                app.last["task_id"] = "fix-a11y"
                await pilot.pause()
                app._open_diagnostics_screen()
                for _ in range(120):
                    if type(app.screen).__name__ == "_DiagnosticsScreen":
                        break
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                return type(app.screen).__name__

        assert asyncio.run(_drive()) == "_DiagnosticsScreen"

    def test_announce_is_called_with_its_key(self):
        """`_announce(key, text)` — the key is the transition identity."""
        import inspect

        from cli import tui

        signature = inspect.signature(tui.NeoApp._announce)
        assert list(signature.parameters)[:3] == ["self", "key", "text"]
        source = inspect.getsource(tui.NeoApp._diagnostics_done)
        assert 'self._announce("diagnostic"' in source

    def test_the_row_shape_is_not_a_sentence(self):
        """Model output arrives as prose; a tool's findings must not read so."""
        row = fileview.normalize_diagnostic(
            {
                "path": "cli/alpha.py",
                "line": 12,
                "column": 3,
                "message": "undefined name 'foo'",
                "severity": "error",
                "source": "pyright",
                "code": "reportUndefinedVariable",
            }
        )
        text = a11y.diagnostic_row(row)
        assert text.startswith("!! error [journal] cli/alpha.py:12:3")
        assert "pyright" in text
        assert "undefined name 'foo'" in text
        # four fields the eye can pattern-match on before reading a word
        assert "[" in text and "]" in text

    def test_provenance_is_visible_in_the_row(self):
        journal = fileview.normalize_diagnostic(
            {"path": "a.py", "line": 1, "message": "m", "severity": "warning"}
        )
        live = fileview.normalize_diagnostic(
            {
                "file": "a.py",
                "line": 1,
                "column": 1,
                "message": "m",
                "severity": "warning",
            }
        )
        assert "[journal]" in a11y.diagnostic_row(journal)
        assert "[lsp]" in a11y.diagnostic_row(live)

    def test_an_unknown_severity_sorts_last_and_marks_itself(self):
        """A severity nobody defined must not lead a breakage scan."""
        order = sorted(
            ["hint", "weird", "error", "fatal", "warning"],
            key=a11y.diagnostic_severity_rank,
        )
        assert order[-1] == "weird"
        assert order[0] == "fatal"
        assert a11y.diagnostic_glyph("weird") == "??"
        assert a11y.diagnostic_glyph("error") == "!!"

    def test_the_receipt_renders_four_distinct_sentences(self):
        rendered = {
            a11y.lsp_state_sentence({"state": "live", "count": 2}),
            a11y.lsp_state_sentence({"state": "not_configured"}),
            a11y.lsp_state_sentence(
                {"state": "unreadable_config", "reason": "bad json"}
            ),
            a11y.lsp_state_sentence({"state": "unavailable", "reason": "no server"}),
        }
        assert len(rendered) == 4

    def test_the_diagnostics_command_still_resolves(self):
        spec = commands.command_spec("/diagnostics")
        assert spec is not None
        assert spec.required_permissions == ("workspace:read",)


# ---------------------------------------------------------------------------
# 5. Summary-first diffs and the bounded view
# ---------------------------------------------------------------------------


class TestSummaryFirstDiffs:
    def test_a_large_diff_is_summarized_not_dumped(self):
        files = [
            {"path": f"f{i}.py", "hunks": [{"lines": ["+x"] * 40}], "additions": 40}
            for i in range(8)
        ]
        summary = fileview.diff_summary({"files": files})
        assert summary["large"] is True
        assert "8 file(s)" in summary["headline"]

    def test_a_small_diff_is_not_marked_large(self):
        summary = fileview.diff_summary(
            {"files": [{"path": "a.py", "hunks": [{"lines": ["+x"]}]}]}
        )
        assert summary["large"] is False

    def test_the_transcript_bound_says_what_it_dropped(self, tmp_path):
        """A silent 240-line cap reads as "that was the diff"."""
        from cli import tui

        app = tui.NeoApp.__new__(tui.NeoApp)
        said = []
        app.transcript = said.append
        tui.NeoApp._render_diff_value(app, "\n".join(f"+line {i}" for i in range(500)))
        assert any("showing the first 240" in line for line in said)
        assert any("/diff detail" in line for line in said)


# ---------------------------------------------------------------------------
# 6. Checkpoints: compare, and a refusal that is useful
# ---------------------------------------------------------------------------


class TestCheckpointRestore:
    def test_a_subset_restore_refuses_with_a_per_file_preflight(
        self, tmp_path, monkeypatch
    ):
        """The refusal stopped being a shrug.

        `restore_checkpoint(files=[...])` returned
        `{"status": "selection_not_supported"}` and one fixed sentence. A
        user asking "can I restore just this file?" got no answer. The
        preflight now measures each requested path against the checkpoint.
        """
        root = _repo(tmp_path, {"cli/alpha.py": "x = 1\n"})

        def _review(repo, log_root, checkpoint_id, **kwargs):
            return {
                "status": "changed",
                "files": [
                    {"path": "cli/alpha.py", "status": "unchanged"},
                    {"path": "cli/beta.py", "status": "modified"},
                ],
            }

        monkeypatch.setattr(fileview, "compare_checkpoint", _review)
        result = fileview.restore_checkpoint(
            root, tmp_path, "cp-1", files=["cli/alpha.py", "cli/beta.py", "cli/gone.py"]
        )
        assert result["status"] == "selection_not_supported"
        assert result["ok"] is False
        assert result["restored_files"] == []
        preflight = result["preflight"]
        assert preflight["restorable"] == ["cli/alpha.py"]
        assert preflight["conflicts"] == ["cli/beta.py"]
        states = {row["path"]: row["state"] for row in preflight["files"]}
        assert states["cli/gone.py"] == "absent"
        assert all(row["detail"] for row in preflight["files"])

    def test_the_preflight_states_an_unreviewable_checkpoint(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            fileview,
            "compare_checkpoint",
            lambda *a, **k: {"status": "not_found"},
        )
        result = fileview.restore_selection_preflight(
            _repo(tmp_path, {"a.py": "x\n"}), tmp_path, "cp-x", ["a.py"]
        )
        assert result["ok"] is False
        assert result["reason"]

    def test_restore_files_and_restore_conversation_are_separate_choices(self):
        """`files only` and `files and conversation` are distinct actions."""
        spec = commands.command_spec("/checkpoints")
        assert spec is not None
        from cli import tui

        # the action modal accepts both tokens, and the restore call carries
        # `include_conversation` as an explicit argument
        assert "include_conversation" in {
            key for key in tui.NeoApp._checkpoint_restore.__code__.co_varnames
        }


# ---------------------------------------------------------------------------
# 7. Staged / unstaged
# ---------------------------------------------------------------------------


class TestStagedUnstaged:
    def test_git_status_reports_both_axes(self, tmp_path):
        """Both axes come from one `git status --porcelain -z` read.

        NO COMMIT: the round's instructions forbid committing, and a
        staged/unstaged distinction is observable from the INDEX alone — a
        file added to the index and never committed is `A`, and modifying it
        afterwards makes it `AM` (staged AND unstaged). So the two axes are
        separable without writing a single object to a repository.
        """
        root = tmp_path / "r"
        root.mkdir()
        _git(root, "init", "-q")
        (root / "staged.py").write_text("b = 1\n", encoding="utf-8")
        (root / "untouched.py").write_text("a = 1\n", encoding="utf-8")
        _git(root, "add", "staged.py", "untouched.py")

        # staged only
        status = fileview.read_git_status(root)
        assert status["staged.py"]["staged"] is True
        assert not status["staged.py"].get("unstaged")

        # staged, then modified again: BOTH axes, which is the case the
        # prompt's "staged/unstaged distinction" is really about
        (root / "staged.py").write_text("b = 2\n", encoding="utf-8")
        status = fileview.read_git_status(root)
        assert status["staged.py"]["staged"] is True
        assert status["staged.py"]["unstaged"] is True

        # untracked is unstaged
        (root / "fresh.py").write_text("c = 1\n", encoding="utf-8")
        status = fileview.read_git_status(root)
        assert status["fresh.py"]["unstaged"] is True
        # a file that is only in the index is staged-only, which is the third
        # distinct shape the prompt's distinction has to cover
        assert status["untouched.py"]["staged"] is True
        assert not status["untouched.py"].get("unstaged")
        # a genuinely clean file is ABSENT from porcelain v1 rather than
        # listed as clean: reporting it would make "no change" and "not
        # looked at" the same row
        assert "never-seen.py" not in status

    def test_a_path_escape_is_refused_rather_than_resolved(self, tmp_path):
        """A status key can never be an absolute or `..` path."""
        status = fileview.read_git_status(tmp_path)
        for key in status:
            assert not key.startswith("/")
            assert ".." not in key.split("/")
            assert "\\" not in key


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# 8. Registry / wiring integrity
# ---------------------------------------------------------------------------


class TestWiring:
    def test_the_new_surfaces_are_reachable_in_both_shells(self):
        from cli import interactive, tui

        repl = Path(interactive.__file__).read_text(encoding="utf-8")
        tui_src = Path(tui.__file__).read_text(encoding="utf-8")
        assert '"/relevant"' in repl
        assert '"/relevant"' in tui_src
        assert "relevant_file_rows" in repl
        assert "relevant_file_rows" in tui_src

    def test_the_file_change_surface_has_no_mojibake(self):
        """`Â·` is what a reader sees in `/files` and `/checkpoints` output.

        The interactive module carries a whole-file double-encoding from an
        earlier round. This round repaired the four lines in the file, diff,
        and checkpoint surfaces it owns — the ones a person reading this
        prompt's features actually sees — and left the rest for its owner
        rather than sweeping a shared file with a 138-line diff. The count
        is pinned so the hand-off stays MEASURABLE: a new mojibake in these
        surfaces fails, and so does an unrelated change to the debt.
        """
        from cli import interactive

        source = Path(interactive.__file__).read_text(encoding="utf-8")
        lines = source.splitlines()
        owned_markers = ("checkpoints", "files[/]", "seq ", "open a file with /diff")
        for line in lines:
            if any(marker in line for marker in owned_markers):
                assert "Â" not in line, line
        # The hand-off: 15 occurrences on 14 lines remain, none of them in
        # this prompt's surfaces. See cli/AGENTS.md.
        assert source.count("Â·") == 15, "unexpected change in the hand-off count"
        assert sum(1 for line in lines if "Â·" in line) == 14

    def test_no_public_chrome_uses_a_literal_colour(self):
        """The new renderers go through the theme, like every other surface."""
        from cli import theme

        tokens = theme.token_coverage()
        assert theme.unmapped_tokens() == theme.UNMAPPED_TOKENS
        assert tokens  # the table is not empty

    def test_the_diagnostics_panel_sorts_by_severity(self):
        """A panel someone scans for breakage leads with the breakage."""
        from cli import tui

        screen = tui._DiagnosticsScreen.__new__(tui._DiagnosticsScreen)
        screen._values = [
            {"severity": "hint", "path": "a.py", "line": 1, "message": "h"},
            {"severity": "fatal", "path": "b.py", "line": 1, "message": "f"},
            {"severity": "warning", "path": "c.py", "line": 1, "message": "w"},
        ]
        screen._lsp_state = {}
        screen._initial = ""
        rows = screen.rows("")
        assert rows[0][1]["severity"] == "fatal"
        assert rows[-1][1]["severity"] == "hint"
