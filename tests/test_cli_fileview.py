"""Tests for the terminal file, diff, context, and checkpoint projections."""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.widgets import Input

from cli import fileview
from cli.interactive import redo_result, undo_result

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_file_tree_and_symbol_picker_are_bounded(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    rows = fileview.file_picker_rows(repo, "app", limit=20)
    assert any(row.get("path") == "src/app.py" for row in rows)
    assert all(row.get("path") != ".." for row in rows)


def test_projection_reports_provenance_hunks_and_checkpoint_membership(tmp_path):
    repo = tmp_path / "repo"
    task = tmp_path / "logs" / "agent-1"
    (repo / "src").mkdir(parents=True)
    (task / "pristine" / "src").mkdir(parents=True)
    (task / "work" / "src").mkdir(parents=True)
    (task / "pristine" / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (task / "work" / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (task / "trace.jsonl").write_text(
        json.dumps(
            {
                "event": "checkpoint_saved",
                "payload": {
                    "checkpoint": {
                        "checkpoint_id": "cp-1",
                        "agent_owned_changes": ["src/app.py"],
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    snapshot = {
        "task_id": "agent-1",
        "changed_files": ["src/app.py"],
        "file_changes": [
            {
                "path": "src/app.py",
                "actor": "agent",
                "reason": "fix requested value",
                "verified": True,
                "undoable": True,
            }
        ],
        "verification_state": "verified",
    }
    projection = fileview.build_file_projection(task, repo, snapshot=snapshot)
    change = projection["file_changes"][0]
    assert change["path"] == "src/app.py"
    assert change["actor"] == "agent"
    assert change["verified"] is True
    assert change["undoable"] is True
    assert change["checkpoint_ids"] == ["cp-1"]
    assert change["hunks"]
    assert change["hunks"][0]["line_numbers"]
    assert projection["diff"]["summary"]["additions"] == 1
    assert projection["diff"]["summary"]["deletions"] == 1


def test_diagnostic_normalization_preserves_one_based_links(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    value = fileview.normalize_diagnostic(
        {"file": "src/app.py", "line": 0, "column": 0, "message": "bad"},
        repo=repo,
    )
    assert value["path"] == "src/app.py"
    assert value["line"] == 1
    assert value["column"] == 1
    assert value["link"] == "src/app.py:1:1"


def test_context_snapshot_degrades_to_empty_cited_shape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    value = fileview.context_snapshot(repo, "inspect app", token_budget=200)
    assert value["repo"] == str(repo.resolve())
    assert "sources" in value
    assert "skills" in value
    assert "memory" in value


def test_undo_redo_refuses_a_later_workspace_edit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "app.py"
    target.write_text("changed\n", encoding="utf-8")
    logs = tmp_path / "logs"
    task = logs / "agent-1"
    (task / "orig").mkdir(parents=True)
    (task / "orig" / "app.py").write_text("original\n", encoding="utf-8")
    (task / "trace.jsonl").write_text("", encoding="utf-8")
    assert (
        undo_result({"task_id": "agent-1"}, logs, repo, "app.py")["outcome"] == "done"
    )
    target.write_text("user edit\n", encoding="utf-8")
    result = redo_result({"task_id": "agent-1"}, logs, repo)
    assert result["outcome"] == "conflict"
    assert target.read_text(encoding="utf-8") == "user edit\n"


def test_checkpoint_records_include_journal_receipts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task = logs / "agent-1"
    task.mkdir(parents=True)
    (task / "trace.jsonl").write_text(
        json.dumps(
            {
                "event": "checkpoint_saved",
                "payload": {
                    "checkpoint": {"checkpoint_id": "cp-1", "resume_token": "r1"}
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    records = fileview.checkpoint_records(repo, logs, task_id="agent-1")
    assert records
    assert records[0]["checkpoint_id"] == "cp-1"


class TestTuiContextSurface:
    async def test_context_command_opens_a_source_panel(self, tmp_path):
        from cli.tui import NeoApp, _ContextSourcesScreen

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        app = NeoApp(
            repo,
            tmp_path / "logs",
            state={"repo": str(repo), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#neo-input", Input).value = "/context"
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause()
                if (
                    app._context_thread is None
                    and isinstance(app.screen, _ContextSourcesScreen)
                    and app.screen.query_one("#sls-list").option_count >= 1
                ):
                    break
                await asyncio.sleep(0.02)
            assert isinstance(app.screen, _ContextSourcesScreen)
            assert app.screen.query_one("#sls-list").option_count >= 1
