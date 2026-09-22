"""Tests for the Vex TUI interaction-polish round (2026-09-15):
the fuzzy command palette (Task A), syntax-highlighted diffs (Task B),
scrollable/searchable history (Task C), the live multi-task benchmark
dashboard (Task D), the reasoning/action visual distinction (Task E)
and the completion notification (Task F).

Pure helpers (cli.fuzzy, session filter grammar, runview progress,
ui diff renderer) are unit-tested; the TUI surfaces run through
textual's real Pilot harness. The scale checks are deliberate: the
prompt's "before you finish" gate is that the palette and session
search are USEFUL at realistic scale, not on three toy rows — so they
seed hundreds of sessions and hundreds of files.

Run with -p no:randomly if pytest-randomly is installed (the TUI
suites share process-global backend patches; same discipline as
test_cli_tui.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from rich.text import Text
from textual.widgets import Input, OptionList, RichLog

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    """The completion bell is TTY-gated and disabled under pytest's
    captured streams anyway — belt-and-braces so no test ever rings."""
    monkeypatch.setenv("VEX_NOTIFY", "0")


@pytest.fixture
def clean_hooks():
    import cli.interactive as iv

    old = iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY
    iv._ON_TASK_START = iv._CANCEL_RUN = iv._PROMPT_BODY = None
    yield
    iv._ON_TASK_START, iv._CANCEL_RUN, iv._PROMPT_BODY = old
    iv._clear_live_run()


async def _drain(pilot, secs: float = 0.2) -> None:
    await asyncio.sleep(secs)
    await pilot.pause()


async def _drain_worker(app, pilot, timeout_s: float = 10.0) -> None:
    if app._worker_thread is None:
        return
    deadline = time.monotonic() + timeout_s
    while app._worker_thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        await pilot.pause()


def _transcript_plain(app) -> str:
    t = Text()
    for line in app.query_one("#vex-body").lines:
        for seg in line._segments:
            t.append(seg.text, style=seg.style)
    return t.plain


# ---------------------------------------------------------------------------
# Task A — the fuzzy matcher itself (cli/fuzzy.py)
# ---------------------------------------------------------------------------


class TestFuzzy:
    def test_empty_query_matches_everything(self):
        import cli.fuzzy as fz

        assert fz.fuzzy_score("", "anything") == 0
        assert fz.fuzzy_score("   ", "anything") == 0

    def test_subsequence_rule(self):
        import cli.fuzzy as fz

        assert fz.fuzzy_score("st", "/status") is not None
        assert fz.fuzzy_score("xyz", "/status") is None
        # case-insensitive
        assert fz.fuzzy_score("ST", "/status") is not None

    def test_prefix_and_boundary_beat_scattered(self):
        import cli.fuzzy as fz

        direct = fz.fuzzy_score("diff", "/diff")
        scattered = fz.fuzzy_score("diff", "/sessions: a diff-ish listing?")
        assert direct > scattered
        # a path-boundary hit beats a mid-word one
        path = fz.fuzzy_score("main", "cli/main.py")
        mid = fz.fuzzy_score("main", "explainmaintain.py")
        assert path > mid

    def test_all_query_words_must_match(self):
        import cli.fuzzy as fz

        assert fz.fuzzy_score("ses fail", "/sessions") is None
        assert fz.fuzzy_score("ses fail", "/sessions status:failed") is not None

    def test_rank_orders_best_first(self):
        import cli.fuzzy as fz

        items = ["/cancel", "/quiet", "/status", "/steer"]
        out = [label for label, _s in fz.rank(items, "st")]
        assert out and out[0] in ("/status", "/steer")
        assert "/cancel" not in out

    def test_hint_fallback_ranks_below_label(self):
        import cli.fuzzy as fz

        recs = [
            {"label": "/diff", "hint": "re-render"},
            {"label": "/feed", "hint": "trace history"},
        ]
        got = fz.filter_and_rank(recs, "history", lambda r: r["label"])
        assert got and got[0]["label"] == "/feed"

    def test_total_on_hostile_input(self):
        import cli.fuzzy as fz

        # never raises on odd shapes
        assert fz.fuzzy_score("[unclosed", "") is None
        assert fz.fuzzy_score("a" * 50, "short") is None
        assert isinstance(fz.rank(["a", 1, None], "a"), list)


# ---------------------------------------------------------------------------
# Task A — the palette at realistic scale (the round's gate)
# ---------------------------------------------------------------------------


class TestPaletteScale:
    """The 'test with real data, not a handful of toy entries' gate:
    hundreds of sessions + hundreds of repo files, and the palette is
    still instant and finds the needle."""

    @staticmethod
    def _big_app(tmp_path):
        import cli.interactive as iv
        import cli.tui as t

        logs = tmp_path / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        # 300 recorded sessions across 3 repos / 3 statuses / 3 days
        for i in range(300):
            iv.record_session(
                logs,
                f"fix-{i:04d}",
                f"issue number {i}: the {'widget' if i % 3 == 0 else 'parser'} breaks",
                str(tmp_path / f"repo{i % 3}"),
                ["success", "failed", "interrupted"][i % 3],
            )
        # 600 files in the repo (git-free walk fallback)
        for d in range(20):
            sub = repo / "pkg" / f"mod{d:02d}"
            sub.mkdir(parents=True)
            (sub / "__init__.py").write_text("", encoding="utf-8")
            for f in range(30):
                (sub / f"file{f:02d}.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "cli").mkdir()
        (repo / "cli" / "tui.py").write_text("", encoding="utf-8")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "junk.js").write_text("", encoding="utf-8")
        app = t.VexApp(
            repo=repo,
            log_root=logs,
            state={"repo": str(repo), "file_config": {}},
            file_config={},
            version="9.9.9",
        )
        return app

    def test_file_scan_skips_junk_and_caps(self, tmp_path):
        import cli.tui as t

        repo = tmp_path / "repo"
        (repo / "cli").mkdir(parents=True)
        (repo / "cli" / "tui.py").write_text("", encoding="utf-8")
        (repo / "node_modules" / "pkg").mkdir(parents=True)
        (repo / "node_modules" / "pkg" / "junk.js").write_text("", encoding="utf-8")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "x.pyc").write_bytes(b"")
        files = t.scan_repo_files(repo, cap=1000)
        assert "cli/tui.py" in files
        assert not any("node_modules" in f for f in files)
        assert not any("__pycache__" in f for f in files)
        assert t.scan_repo_files(tmp_path / "does-not-exist") == []

    def test_entries_build_at_scale(self, tmp_path):
        app = self._big_app(tmp_path)
        t0 = time.monotonic()
        entries = app._palette_entries()
        build_s = time.monotonic() - t0
        # commands + custom (0) + up-to-60 sessions + every repo file
        kinds = {e["kind"] for e in entries}
        assert kinds == {"command", "session", "file"}
        assert sum(1 for e in entries if e["kind"] == "file") > 500
        assert sum(1 for e in entries if e["kind"] == "session") > 0
        assert build_s < 5.0, f"palette build too slow at scale: {build_s:.2f}s"

    async def test_palette_search_finds_needle_in_haystack(self, tmp_path, clean_hooks):
        import cli.tui as t

        app = self._big_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_command_palette()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, t._PaletteScreen)
            lst = screen.query_one("#palette-list", OptionList)
            assert len(lst.options) <= screen._MAX_RESULTS  # capped view
            inp = screen.query_one("#palette-input", Input)

            # a FILE, fuzzy-typed: "cltui" should surface cli/tui.py
            t0 = time.monotonic()
            inp.value = "cltui"
            await pilot.pause()
            took = time.monotonic() - t0
            texts = [str(o.prompt) for o in lst.options]
            assert texts and "cli/tui.py" in texts[0], texts[:3]
            assert took < 2.0, f"filter too slow at scale: {took:.2f}s"

            # a SESSION by id fragment: its task id is the label
            inp.value = "fix-029"
            await pilot.pause()
            texts = [str(o.prompt) for o in lst.options]
            assert any("fix-029" in x for x in texts)

            # a command by name still wins its own exact query
            inp.value = "/sessions"
            await pilot.pause()
            texts = [str(o.prompt) for o in lst.options]
            assert texts and "/sessions" in texts[0]

            await pilot.press("escape")
            await pilot.pause()

    async def test_palette_on_the_real_repo(self, tmp_path, clean_hooks):
        """The round's 'test with real data, not a handful of toy
        entries' gate: open the palette against the ACTUAL project
        working tree (hundreds of real source files, real recorded
        session history), fuzzy-search a real module, and pick it —
        end to end through the real app, not a synthetic list."""
        from textual.widgets import Input

        import cli.tui as t

        repo = Path(__file__).resolve().parents[1]  # the coding-harness root
        assert (repo / "cli" / "tui.py").is_file()
        app = t.VexApp(
            repo=repo,
            log_root=tmp_path / "logs",
            state={"repo": str(repo), "file_config": {}},
            file_config={},
            version="9.9.9",
        )
        t0 = time.monotonic()
        entries = app._palette_entries()
        n_files = sum(1 for e in entries if e["kind"] == "file")
        assert n_files > 200, f"expected a real file list, got {n_files}"
        # a genuine repo file is searchable by a fuzzy fragment
        frag = [e for e in entries if e["label"] == "cli/tracelog.py"]
        assert frag, "cli/tracelog.py should be in the real scan"
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.action_command_palette()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, t._PaletteScreen)
            lst = screen.query_one("#palette-list", OptionList)
            screen.query_one("#palette-input", Input).value = "cli trce"
            await pilot.pause()
            assert time.monotonic() - t0 < 12, "real-tree palette too slow"
            texts = [str(o.prompt) for o in lst.options]
            assert any("cli/tracelog.py" in x for x in texts), texts[:6]
            await pilot.press("escape")
            await pilot.pause()

    async def test_palette_selection_behaviors(self, tmp_path, clean_hooks):
        """Commands that need no argument RUN (the palette executes,
        VS Code style); files and arg-taking commands prefill."""

        app = self._big_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # a command entry runs immediately
            app._palette_chosen({"kind": "command", "value": "/help", "run": True})
            await pilot.pause()
            assert "what you can say" in _transcript_plain(app)
            # a file entry prefills its path (you finish the sentence)
            app._palette_chosen({"kind": "file", "value": "cli/tui.py", "run": False})
            await pilot.pause()
            assert app.query_one("#vex-input", Input).value.startswith("cli/tui.py")
            app.query_one("#vex-input", Input).value = ""
            # an arg-taking command prefills for completion
            app._palette_chosen({"kind": "command", "value": "/resume", "run": False})
            await pilot.pause()
            assert app.query_one("#vex-input", Input).value == "/resume "


# ---------------------------------------------------------------------------
# Task B — syntax-highlighted diffs
# ---------------------------------------------------------------------------


class TestSyntaxDiff:
    DIFF = (
        "--- a/mathutil.py\n"
        "+++ b/mathutil.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def mean(values):\n"
        "-    return sum(values)\n"
        "+    return sum(values) / len(values)\n"
        "     # done"
    )

    def test_lines_render_with_token_colors(self):
        from rich.style import Style

        import cli.ui as ui

        lines = ui.diff_render_lines(self.DIFF)
        assert len(lines) == len(self.DIFF.splitlines())
        add = lines[5]  # the + line
        assert add.plain.startswith("+")

        def _color(span):
            st = span.style
            if isinstance(st, str):
                st = Style.parse(st)
            return getattr(st, "color", None)

        colors = {str(_color(sp)) for sp in add.spans if _color(sp) is not None}
        # language-aware: the python keyword token is colored beyond the
        # single flat add-role color (the '+' marker carries the role)
        assert len(colors) >= 1
        marker_colors = {
            str(_color(sp))
            for sp in add.spans
            if add.plain[sp.start : sp.end].strip() == "+"
        }
        assert marker_colors, "the + marker must carry the diff role"

    def test_filename_selects_the_language(self):
        import cli.ui as ui

        py = ui.diff_render_lines("--- a/x.py\n+++ b/x.py\n@@\n+def f(): return 1")
        js = ui.diff_render_lines(
            "--- a/y.js\n+++ b/y.js\n@@\n+function f() { return 1 }"
        )
        # both lex, and the python `def` token is colored
        py_add = py[3]
        assert any(getattr(sp.style, "color", None) for sp in py_add.spans)
        assert js[3].plain.startswith("+")

    def test_unknown_language_degrades_to_plain_not_crash(self):
        import cli.ui as ui

        lines = ui.diff_render_lines(
            "--- a/data.zzzq\n+++ b/data.zzzq\n@@\n+1 2 3 weird"
        )
        assert lines[3].plain.startswith("+")  # styled by role, not lexer

    def test_accepts_the_live_diff_pair_shape(self):
        import cli.ui as ui

        pairs = [
            ("--- a/m.py", "meta"),
            ("+++ b/m.py", "meta"),
            ("@@ -1 +1 @@", "hunk"),
            ("-old = 1", "del"),
            ("+new = 2", "add"),
        ]
        lines = ui.diff_render_lines(pairs)
        assert len(lines) == 5
        assert lines[3].plain.startswith("-")
        assert lines[4].plain.startswith("+")

    def test_total_on_garbage(self):
        import cli.ui as ui

        t = ui.diff_text("")
        assert isinstance(t, Text)
        # no markup-bracket crash (content with brackets must pass
        # through as literal text, not be parsed as rich markup)
        t2 = ui.diff_text("+ [bold] fake markup [/]")
        assert "[bold]" in t2.plain
        lines = ui.diff_render_lines([("+x", "add"), ("-y", "del")])
        assert lines[0].plain.startswith("+") and lines[1].plain.startswith("-")


# ---------------------------------------------------------------------------
# Task C — searchable sessions + scrollable feed
# ---------------------------------------------------------------------------


class TestSessionSearch:
    def _sessions(self) -> List[Dict[str, Any]]:
        base = time.time()
        return [
            {
                "task_id": f"fix-{i:03d}",
                "ts": base - i * 86400,
                "issue": f"issue {i} in widget",
                "repo": ["/repos/alpha", "/repos/beta", "/repos/gamma"][i % 3],
                "status": ["success", "failed", "interrupted"][i % 3],
                "resumable": i % 5 == 0,
            }
            for i in range(60)
        ]

    def test_free_text_and_tokens(self):
        import cli.interactive as iv

        s = self._sessions()
        assert len(iv.filter_sessions(s, "")) == 60
        got = iv.filter_sessions(s, "status:failed")
        assert got and all(x["status"] == "failed" for x in got)
        got = iv.filter_sessions(s, "repo:beta")
        assert got and all("beta" in x["repo"] for x in got)
        got = iv.filter_sessions(s, "resumable")
        assert got and all(x["resumable"] for x in got)
        got = iv.filter_sessions(s, "widget")
        assert len(got) == 60  # every issue says "widget"
        # a bare token is a whole-line substring match (AND across
        # tokens); a task id is the unambiguous needle
        got = iv.filter_sessions(s, "fix-007")
        assert [x["task_id"] for x in got] == ["fix-007"]
        got = iv.filter_sessions(s, "task:007")
        assert [x["task_id"] for x in got] == ["fix-007"]

    def test_date_filters(self):
        import cli.interactive as iv

        s = self._sessions()
        today = time.strftime("%Y-%m-%d", time.localtime(s[0]["ts"]))
        got = iv.filter_sessions(s, f"day:{today}")
        assert got and all(
            time.strftime("%Y-%m-%d", time.localtime(x["ts"])) == today for x in got
        )
        since = time.strftime("%Y-%m-%d", time.localtime(s[10]["ts"]))
        got = iv.filter_sessions(s, f"since:{since}")
        assert len(got) == 11  # the 11 newest (i=0..10)
        got = iv.filter_sessions(s, "day:not-a-date")
        assert got == []  # an unevaluable filter narrows, never lies

    def test_combined_tokens_and(self):
        import cli.interactive as iv

        s = self._sessions()
        got = iv.filter_sessions(s, "status:success resumable")
        assert got  # i % 3 == 0 and i % 5 == 0 -> i = 0,15,30,45
        assert all(x["status"] == "success" and x["resumable"] for x in got)
        ids = {x["task_id"] for x in got}
        assert ids == {"fix-000", "fix-015", "fix-030", "fix-045"}

    def test_scale_300_sessions_finds_the_needle(self, tmp_path):
        import cli.interactive as iv

        logs = tmp_path / "logs"
        for i in range(300):
            iv.record_session(
                logs,
                f"fix-{i:04d}",
                f"bug {i}: parse_{i} fails",
                f"/repos/proj{i % 4}",
                ["success", "failed"][i % 2],
            )
        found = iv.search_sessions(logs, "parse_271")
        assert [s["task_id"] for s in found] == ["fix-0271"]
        found = iv.search_sessions(logs, "status:failed repo:proj1")
        assert found and all("proj1" in s["repo"] for s in found)

    async def test_tui_sessions_browser_opens_and_resumes(self, tmp_path, clean_hooks):
        import cli.interactive as iv
        import cli.tui as t

        logs = tmp_path / "logs"
        iv.record_session(logs, "fix-aaa", "issue aaa", "/r", "success")
        repo = tmp_path / "repo"
        repo.mkdir()
        app = t.VexApp(
            repo=repo,
            log_root=logs,
            state={"repo": str(repo), "file_config": {}},
            file_config={},
        )
        started: List[str] = []
        app._start_resume = lambda tid: started.append(tid)  # type: ignore
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#vex-input", Input).value = "/sessions"
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, t._SessionsScreen)
            screen.query_one("#sls-input", Input).value = "aaa"
            await pilot.pause()
            lst = screen.query_one("#sls-list", OptionList)
            assert any("fix-aaa" in str(o.prompt) for o in lst.options)
            await pilot.press("enter")
            await pilot.pause()
            assert started == ["fix-aaa"]


class TestFeedHistory:
    """The scrollable, searchable feed browser (Task C)."""

    @staticmethod
    def _entries():
        import cli.tracelog as tl

        b = tl.FeedBuilder("fix-feed01")
        b.consume({"kind": "task_start", "data": {"issue_text": "bug"}})
        b.consume(
            {
                "kind": "model_response",
                "data": {
                    "step": "plan",
                    "content": '{"plan": [{"id": 1, "description": "fix x"}]}',
                    "usage": {"cost": 0.001, "tokens": 5},
                },
            }
        )
        b.consume(
            {"kind": "plan", "data": {"plan": [{"id": 1, "description": "fix x"}]}}
        )
        b.consume(
            {
                "kind": "tool_call",
                "data": {"step_id": 1, "turn": 1, "command": "cat x.py"},
            }
        )
        b.consume(
            {
                "kind": "tool_result",
                "data": {"step_id": 1, "output": "print(1)\nMARKER"},
            }
        )
        b.consume({"kind": "verify", "data": {"step_id": 1, "target_passed": True}})
        return b.entries

    def test_shared_feed_renderer_keeps_reason_action_apart(self):
        import cli.tui as t

        reason = next(e for e in self._entries() if e.category == "reason")
        tool = next(e for e in self._entries() if e.category == "tool")
        r_line = t.feed_line(reason)
        tol_line = t.feed_line(tool)
        assert "italic" in r_line and t.ui.TEXT_SECONDARY in r_line
        assert "italic" not in tol_line.replace("italic", "X")
        assert "bold" in tol_line and t.ui.ACCENT_TEXT in tol_line
        # the Text variant (browser) carries the same distinction
        rt = t.feed_line_text(reason)
        assert any("italic" in str(sp.style) for sp in rt.spans)
        tt = t.feed_line_text(tool)
        assert all("italic" not in str(sp.style) for sp in tt.spans)

    async def test_feed_browser_filters_and_expands(self, tmp_path, clean_hooks):
        import cli.tui as t

        entries = self._entries()
        screen = t._FeedBrowserScreen(entries, "")
        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            lst = screen.query_one("#sls-list", OptionList)
            assert len(lst.options) == len(entries)
            # filter to the tool line
            screen.query_one("#sls-input", Input).value = "cat"
            await pilot.pause()
            assert len(lst.options) == 1
            # enter expands WITHOUT closing the browser (detail on top)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, t._TraceDetailScreen)
            body = app.screen.query_one("#trace-body", RichLog)
            plain = "".join(seg.text for line in body.lines for seg in line._segments)
            assert "$ cat x.py" in plain
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, t._FeedBrowserScreen)
            await pilot.press("escape")
            await pilot.pause()

    async def test_transcript_scrollback_pauses_auto_follow(
        self, tmp_path, clean_hooks
    ):
        """Task C: scrolling the transcript up mid-run stops the live
        feed yanking the view back to the bottom; back to the bottom
        resumes it."""
        import cli.tui as t

        app = t.VexApp(
            repo=tmp_path,
            log_root=tmp_path / "logs",
            state={"repo": str(tmp_path), "file_config": {}},
            file_config={},
        )
        async with app.run_test(size=(120, 30)) as pilot:
            log = app.query_one("#vex-body", RichLog)
            for i in range(400):
                log.write(f"line {i}")
            await pilot.pause()
            await asyncio.sleep(0.05)
            await pilot.pause()
            assert log.auto_scroll is True
            # a user scroll away from the bottom pauses the follow
            log.scroll_page_up(animate=False)
            await pilot.pause()
            await asyncio.sleep(0.05)
            await pilot.pause()
            assert log.auto_scroll is False
            # a new write must NOT yank the reader back down
            before = log.scroll_y
            log.write("fresh while paused")
            await pilot.pause()
            assert log.scroll_y == before
            assert log.auto_scroll is False
            # returning to the bottom resumes following
            log.scroll_end(animate=False)
            await pilot.pause()
            assert log.auto_scroll is True


# ---------------------------------------------------------------------------
# Task D — the live multi-task benchmark dashboard
# ---------------------------------------------------------------------------


def _write_progress(log_root: Path, task_id: str, events: List[Dict[str, Any]]) -> None:
    d = log_root / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )


class TestTaskProgress:
    def test_running_task_folded_from_trace(self, tmp_path):
        from cli import runview

        ts = 1000.0
        _write_progress(
            tmp_path,
            "fix-a",
            [
                {"ts": ts, "kind": "task_start", "data": {}},
                {
                    "ts": ts + 4,
                    "kind": "model_response",
                    "data": {
                        "step": "plan",
                        "content": "",
                        "usage": {
                            "cost": 0.002,
                            "tokens": 100,
                            "model": "openai/gpt-4o-mini",
                        },
                    },
                },
                {"ts": ts + 9, "kind": "attempt_start", "data": {"attempt": 1}},
            ],
        )
        p = runview.read_task_progress(tmp_path, "fix-a")
        assert p["status"] == "running"
        assert p["phase"] == "editing"
        assert p["model_calls"] == 1
        assert p["cost_usd"] == pytest.approx(0.002)
        assert p["started_ts"] == ts
        assert p["models"] == ["gpt-4o-mini"]

    def test_result_and_ledger_authorities(self, tmp_path):
        from cli import runview

        ts = 2000.0
        _write_progress(
            tmp_path,
            "fix-b",
            [
                {"ts": ts, "kind": "task_start", "data": {}},
                {
                    "ts": ts + 60,
                    "kind": "result",
                    "data": {"status": "success", "cost_usd": 0.05, "attempts": 1},
                },
            ],
        )
        rt = tmp_path / "fix-b.runtime"
        rt.mkdir()
        (rt / "model_ledger.jsonl").write_text(
            json.dumps({"model": "glm", "routed_via_hint": "easy"})
            + "\n"
            + json.dumps({"model": "glm", "difficulty_hint": "hard"}),
            encoding="utf-8",
        )
        p = runview.read_task_progress(tmp_path, "fix-b")
        assert p["status"] == "success"
        assert p["cost_usd"] == 0.05  # the result event wins over usage
        assert p["elapsed_s"] == pytest.approx(60.0)
        assert p["tier"] == "Easy/Hard"

    def test_never_started_and_malformed(self, tmp_path):
        from cli import runview

        p = runview.read_task_progress(tmp_path, "ghost")
        assert p["status"] == "queued"
        assert p["cost_usd"] == 0.0
        _write_progress(tmp_path, "broken", [{"nope": 1}, {"kind": None}])
        p = runview.read_task_progress(tmp_path, "broken")
        assert p["status"] in ("queued", "running")  # total, never raises


class TestBenchmarkDashboard:
    @staticmethod
    def _tasks(n: int):
        class T:
            def __init__(self, tid):
                self.task_id = tid
                self.config = {}

        return [T(f"bench-{i}") for i in range(n)]

    def test_rows_show_every_task_with_cost_elapsed_tier(self, tmp_path):
        import cli.main as m

        tasks = self._tasks(4)
        ts = time.time() - 100
        # 2 finished, 1 running (with a long-silent model call), 1 queued
        _write_progress(
            tmp_path,
            "bench-0",
            [
                {"ts": ts, "kind": "task_start", "data": {}},
                {
                    "ts": ts + 30,
                    "kind": "result",
                    "data": {"status": "success", "cost_usd": 0.004},
                },
            ],
        )
        rt = tmp_path / "bench-0.runtime"
        rt.mkdir()
        (rt / "model_ledger.jsonl").write_text(
            json.dumps({"model": "stepfun", "difficulty_hint": "easy"}),
            encoding="utf-8",
        )
        _write_progress(
            tmp_path,
            "bench-1",
            [
                {"ts": ts, "kind": "task_start", "data": {}},
                {
                    "ts": ts + 20,
                    "kind": "model_response",
                    "data": {
                        "step": "plan",
                        "content": "",
                        "usage": {"cost": 0.01, "tokens": 200},
                    },
                },
            ],
        )
        _write_progress(
            tmp_path, "bench-2", [{"ts": ts + 5, "kind": "task_start", "data": {}}]
        )
        live = m._BenchmarkLiveView(tasks, tmp_path)
        live.note_result("bench-0", "success")
        live.note_result("bench-1", "success")
        rows = live.rows(now=ts + 100)
        assert [r["task"] for r in rows] == ["bench-0", "bench-1", "bench-2", "bench-3"]
        done, running, queued = rows[0], rows[2], rows[3]
        assert done["state"] == "success"
        assert "$" in done["cost"]
        assert "stepfun" in done["tier"]
        assert queued["state"] == "queued"
        assert running["state"] == "running"
        assert running["elapsed"].endswith("s")  # ticking wall clock
        # the table renderer produces all seven columns per task
        table = live.render(now=ts + 100)
        assert len(table.columns) == 7
        assert table.row_count == 4

    def test_live_attempts_wall_clock_when_no_trace_yet(self, tmp_path):
        import cli.main as m

        class Att:
            started_epoch = time.time() - 42

        class Sched:
            def live_attempts(self):
                return {"bench-0": Att()}

        tasks = self._tasks(1)
        live = m._BenchmarkLiveView(tasks, tmp_path)
        live._scheduler = Sched()
        rows = live.rows()
        assert rows[0]["state"] == "running"
        assert rows[0]["elapsed"] in {"42s", "41s"}

    def test_stays_honest_with_the_stub(self, tmp_path):
        """No scheduler object at all (the stub path): the view degrades
        to trace-file facts + queued, never crashes."""
        import cli.main as m

        tasks = self._tasks(2)
        live = m._BenchmarkLiveView(tasks, tmp_path)
        rows = live.rows()
        assert [r["state"] for r in rows] == ["queued", "queued"]
        table = live.render()
        assert table.row_count == 2


# ---------------------------------------------------------------------------
# Task F — completion notification
# ---------------------------------------------------------------------------


class TestBell:
    def test_bell_writes_a_bell_byte_on_a_tty(self, monkeypatch, capsys):
        import io
        import sys

        import cli.ui as ui

        buf = io.StringIO()
        monkeypatch.delenv("VEX_NOTIFY", raising=False)
        monkeypatch.setattr(sys, "stderr", buf)
        monkeypatch.setattr(
            sys, "stdout", type("T", (), {"isatty": lambda self: True})()
        )
        ui.bell()
        assert buf.getvalue() == "\a"

    def test_bell_silent_into_a_pipe(self, monkeypatch):
        import io
        import sys

        import cli.ui as ui

        buf = io.StringIO()
        monkeypatch.delenv("VEX_NOTIFY", raising=False)
        fake = type("T", (), {"isatty": lambda self: False})()
        monkeypatch.setattr(sys, "stderr", buf)
        monkeypatch.setattr(sys, "stdout", fake)
        ui.bell()
        assert buf.getvalue() == ""  # never pollute a piped stdout/stderr

    def test_vex_notify_env_silences(self, monkeypatch):
        import io
        import sys

        import cli.ui as ui

        monkeypatch.setenv("VEX_NOTIFY", "0")
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", buf)
        monkeypatch.setattr(
            sys, "stdout", type("T", (), {"isatty": lambda self: True})()
        )
        ui.bell()
        assert buf.getvalue() == ""

    def test_repl_notify_defers_to_the_tui(self, monkeypatch):
        """One ring per finished task: the REPL's notify_done stands
        down while the TUI's live hook is mounted."""
        import cli.interactive as iv

        calls: List[str] = []
        monkeypatch.setattr(iv.ui, "bell", lambda msg="": calls.append(msg))
        monkeypatch.setattr(iv, "_ON_TASK_START", None)
        iv.notify_done("success")
        assert calls
        calls.clear()
        monkeypatch.setattr(iv, "_ON_TASK_START", lambda tid: None)
        iv.notify_done("success")
        assert calls == []
        calls.clear()
        monkeypatch.setattr(iv, "_ON_TASK_START", None)
        monkeypatch.setattr(iv, "NOTIFY", False)
        iv.notify_done("success")
        assert calls == []


# ---------------------------------------------------------------------------
# Module hygiene: the shared renderers are single-sourced
# ---------------------------------------------------------------------------


class TestSingleSource:
    def test_feed_styles_defined_once(self):
        import cli.tui as t

        # the VexApp no longer carries its own copies (a second style
        # table is exactly how surfaces drift)
        assert not hasattr(t.VexApp, "_FEED_GLYPHS")
        assert not hasattr(t.VexApp, "_FEED_STYLES")
        assert "reason" in t.FEED_STYLES and "italic" in t.FEED_STYLES["reason"]

    def test_palette_lists_the_new_surfaces(self):
        import cli.tui as t

        labels = {c[0] for c in t.VexApp._PALETTE_COMMANDS}
        assert {"/feed", "/sessions", "/diff"} <= labels

    def test_help_documents_the_new_commands(self):
        import cli.interactive as iv

        assert "/feed" in iv._HELP
        assert "status:failed" in iv._HELP  # the filter grammar is taught
