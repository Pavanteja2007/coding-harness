"""Tests for the branding round (2026-09-13): the oxblood theme + block
wordmark, the splash-vs-compact-header split (Task C), and the intent
gate (Task E — a real defect: "hi" used to launch a fix task; pinned
here so it can never regress).

Cross-terminal note: same convention as test_cli_vex.py / vex2 —
Terminal 4's command-layer suites stay authoritative for flags;
this file owns the interactive-session presentation + intent surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import ui

# ---------------------------------------------------------------------------
# Task A — theme + wordmark
# ---------------------------------------------------------------------------


class TestOxbloodTheme:
    def test_accent_roles_use_oxblood_ramp(self):
        """The brand accent is the crimson ramp (re-themed 2026-09-22 from
        oxblood to crimson-on-black): accent #E8114A (~4.6:1 on #000000,
        measured — #DC143C in the same hue is ~4.2:1, below the 4.5:1
        bar, so brightness was adjusted, not hue). Active/in-progress
        states (vex.running) use the same logo crimson — active =
        accent, per the doc, never orange. The rose glow (accent-glow
        token) is reserved, not the running color."""
        accent = ui.VEX_THEME.styles.get("vex.accent")
        running = ui.VEX_THEME.styles.get("vex.running")
        assert accent.color.get_truecolor().hex.lower() == "#e8114a"
        assert running.color.get_truecolor().hex.lower() == "#e8114a"
        # the rose glow (accent-glow token) is reserved, not the running color
        glow = ui.VEX_THEME.styles.get("vex.glow")
        assert glow.color.get_truecolor().hex.lower() == "#ff7a93"

    def test_wordmark_reads_as_VEX(self):
        """The wordmark: 6 rows, uniform 26-column span, letterforms
        intact. For the box-drawing variant we pin the DISTINCTIVE
        fragments of each row (V's diverging strokes, E's bars, X's
        crossing strokes) — a stronger legibility pin than geometry:
        a bad edit can't quietly produce an illegible mark. The '#'
        fallback pins clean 3-space gutters instead."""
        rows = ui.wordmark_lines()
        assert len(rows) == 6
        assert all(len(r) == 26 for r in rows), [len(r) for r in rows]
        if any("╗" in r for r in rows):  # ANSI-shadow variant
            assert rows[0].startswith("██╗   ██╗")  # V's two strokes
            assert "███████" in rows[0]  # E's top bar
            assert "██╗  ██╗" in rows[0]  # X's strokes
            assert "╚██╗ ██╔╝" in rows[3]  # V narrows
            assert "█████╗" in rows[2]  # E's mid bar
            assert "╚═══╝ ╚══════╝" in rows[5]  # baseline serif
        else:  # '#' block fallback: gutters between glyphs
            for row in rows:
                assert row[7:10].strip() == ""
                assert row[16:19].strip() == ""

    def test_wordmark_ascii_fallback(self):
        """Non-UTF8 consoles get '#' letterforms, same VEX geometry —
        box-drawing glyphs crash cp1252 writers (GLYPS discipline)."""
        rows = ui._WORDMARK_BLOCK
        assert all(set(r) <= {"#", " "} for r in rows)
        assert len(rows) == 6

    def test_splash_renders_all_lines(self, capsys):
        ui.print_splash(
            repo=Path("/x/repo"),
            log_root=Path("/x/repo/logs"),
            model="m1",
            version="1.2.3",
        )
        out = capsys.readouterr().out
        assert "verified, not vibed" in out  # tagline
        assert "repo" in out and "logs" in out
        assert "m1" in out and "1.2.3" in out
        assert "/help" in out
        # the hairline rule renders (rich Rule degrades to '-'/ASCII)
        assert "───" in out or "---" in out

    def test_compact_header_one_line(self, capsys):
        """Claude-Code pattern: version + model + repo, ONE line, no
        giant art (the splash is NOT repeated)."""
        ui.print_compact_header(repo=Path("/x/repo"), model="m1", version="1.2.3")
        out = capsys.readouterr().out
        assert "vex 1.2.3" in out and "model" in out and "m1" in out
        assert ui.DOT in out  # dot separators, one visual grammar
        assert "verified, not vibed" not in out  # tagline is splash-only

    def test_spinner_safe_for_legacy_consoles(self):
        """ui.SPINNER picks a braille-free spinner when the console
        encoding can't render braille (cp1252) — rich's default
        'dots' crashed LegacyWindowsTerm writers (probe-found)."""
        if ui._enc_ok("\u280b"):
            assert ui.SPINNER == "dots"
        else:
            assert ui.SPINNER != "dots"
        # and the chosen spinner exists in rich's table
        from rich._spinners import SPINNERS

        assert ui.SPINNER in SPINNERS


# ---------------------------------------------------------------------------
# Task C — splash vs compact (first launch detection)
# ---------------------------------------------------------------------------


class TestFirstLaunch:
    def test_first_launch_when_log_root_empty(self, tmp_path):
        from cli.interactive import _is_first_launch

        assert _is_first_launch(tmp_path) is True

    def test_not_first_launch_with_session_index(self, tmp_path):
        from cli.interactive import _is_first_launch, record_session

        record_session(tmp_path, "t-1", "issue", "/r", "success")
        assert _is_first_launch(tmp_path) is False

    def test_not_first_launch_with_prior_run_dir(self, tmp_path):
        from cli.interactive import _is_first_launch

        d = tmp_path / "fix-abc123"
        d.mkdir()
        (d / "trace.jsonl").write_text('{"kind": "task_start"}\n', encoding="utf-8")
        assert _is_first_launch(tmp_path) is False

    def test_underscore_dirs_are_harness_artifacts_not_runs(self, tmp_path):
        """logs/_code-graph etc. are shared harness state, not session
        history — a fresh interactive session in a repo where only the
        code-graph cache exists STILL earns the splash."""
        from cli.interactive import _is_first_launch

        (tmp_path / "_code-graph").mkdir()
        (tmp_path / "_code-graph" / "graph.json").write_text("{}", encoding="utf-8")
        assert _is_first_launch(tmp_path) is True


# ---------------------------------------------------------------------------
# Task E — the intent gate (bug fix: "hi" launched a fix task)
# ---------------------------------------------------------------------------


class TestIntentGate:
    """THE regression for the defect this round fixes: casual input
    used to fall straight into _run_one_fix. Every case here must stay
    classified without launching anything."""

    @pytest.mark.parametrize(
        "line",
        [
            "hi",
            "hello",
            "hey",
            "hey there",
            "yo",
            "good morning",
            "what can you do?",
            "what can you do",
            "who are you?",
            "are you an AI agent?",
            "what model are you using?",
            "how does vex work?",
            "help me",
            "help",
            "thanks",
            "thank you",
            "ok",
            "cool",
            "how's it going?",
            "what's up",
            "you there?",
        ],
    )
    def test_conversational_input_never_launches(self, line):
        from cli.intent import classify

        it = classify(line)
        assert it.kind == "convo", line
        assert it.reply.strip(), line  # an actual answer, never silence

    @pytest.mark.parametrize(
        "line",
        [
            # the canonical sentence from the live drives
            "mean() in mathutil.py returns the sum; make it the mean",
            "fix the login bug where the password is empty",
            "the parser crashes on empty input",
            "tests/test_mathutil.py::test_mean fails",
            "TypeError in serializers.py when the payload is null",
            "there's an off-by-one in the index calculation",
            "remove the deprecated flag from build.sh",
            "help me fix the flaky timeout in the retry loop",
            "make it stop throwing KeyError when the cache is cold",
            "the retry loop hangs forever under load",
        ],
    )
    def test_bug_descriptions_launch(self, line):
        from cli.intent import classify

        assert classify(line).kind == "fix", line

    @pytest.mark.parametrize(
        "line",
        [
            "is this repo big?",
            "what time is it?",
            "help me move apartments",
            "the weather is nice today",
        ],
    )
    def test_ambiguous_input_asks_instead_of_running(self, line):
        from cli.intent import classify

        it = classify(line)
        assert it.kind == "ambiguous", line
        assert it.reply.strip(), line  # a clarifying question

    def test_empty_input_is_ambiguous(self):
        from cli.intent import classify

        assert classify("").kind == "ambiguous"
        assert classify("   ").kind == "ambiguous"

    def test_session_loop_gates_before_run(self, tmp_path, monkeypatch, capsys):
        """The wiring test: inside run_interactive's loop the gate
        fires BEFORE _run_one_fix — 'hi' produces an answer line and
        never reaches the executor (monkeypatched to explode if
        called)."""
        from cli import interactive

        def explode(*a, **k):
            raise AssertionError("conversational input must not reach _run_one_fix")

        monkeypatch.setattr(interactive, "_run_one_fix", explode)

        # drive exactly one loop iteration via the session loop body:
        # simplest honest seam = call classify + the loop's own branch
        # shape. We exercise the REAL loop by feeding one line and
        # intercepting exit: input returns 'hi' once then EOF.
        lines = iter(["hi"])

        def fake_input(prompt=""):
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.chdir(tmp_path)  # session CWD: never scaffold the real tree
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / ".vex"))
        rc = interactive.run_interactive(log_root=tmp_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "fix that bug" in out or "describe what's wrong" in out
        assert "task fix-" not in out  # nothing launched

    def test_session_loop_launches_real_bug_sentence(self, tmp_path, monkeypatch):
        """The positive wiring: a genuine bug sentence DOES reach
        _run_one_agent with the sentence as the task text (the session
        dispatches agent tasks through the ONE live-repo loop)."""
        from cli import interactive

        captured = {}

        def fake_run_one_agent(
            issue, repo, state, log_root, file_config=None, task_id=None
        ):
            captured["issue"] = issue
            return None

        monkeypatch.setattr(interactive, "_run_one_agent", fake_run_one_agent)
        lines = iter(["mean() in mathutil.py returns the sum; make it the mean"])

        def fake_input(prompt=""):
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.chdir(tmp_path)  # session CWD: never scaffold the real tree
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / ".vex"))
        rc = interactive.run_interactive(log_root=tmp_path)
        assert rc == 0
        assert captured["issue"].startswith("mean() in mathutil.py")


# ---------------------------------------------------------------------------
# Task F — status-line parity (run-events summary shape)
# ---------------------------------------------------------------------------


class TestStatusLine:
    def test_monitor_summary_renders_events_calls_tokens_cost(self, tmp_path, capsys):
        from cli.interactive import LiveMonitor

        d = tmp_path / "fix-line"
        d.mkdir()
        trace = d / "trace.jsonl"
        # NOT quiet: the summary line is the product surface (quiet
        # exists for tests; the summary must survive normal mode).
        mon = LiveMonitor("fix-line", tmp_path).start()
        try:
            trace.write_text(
                json.dumps(
                    {
                        "kind": "model_response",
                        "data": {"usage": {"tokens": 120, "cost": 0.0042}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            import time

            time.sleep(0.4)
        finally:
            mon.stop()
        out = capsys.readouterr().out
        assert "run" in out and "1 events" in out
        assert "model calls" in out
        assert "120 tokens" in out
        assert "$0.004200" in out
