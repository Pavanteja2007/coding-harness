"""General agent loop (harness.agent_loop) — the interactive `vex` engine.

Covers the task's Done criteria at unit level (no Docker, no network):
- classify -> (question | agent_task | chit_chat): "hi" never launches,
  "explain how routing works" is a question, "add logging to X" /
  "run pytest ... and fix failures" / "refactor ..." are agent tasks.
- run_agent: read-only DONE flow (repo untouched, trace.jsonl written),
  READ->EDIT->DONE flow (live repo edited, diff + undo work), BASH via
  the injected sandbox, MEMORY + GLOB/GREP tools, no verifier gate by
  default vs verifier when tests are declared, approval=require refusal,
  steering abort/guide, and trace.jsonl per session.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Scripted:
    """Queue-driven fake model: pop one reply per call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kw):
        self.calls.append({"n_messages": len(messages), "kw": kw})
        assert self.replies, "model called more times than scripted"
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "router.py").write_text(
        "def route(kind):\n    return kind\n", encoding="utf-8"
    )
    (r / "src" / "util.py").write_text("X = 1\n", encoding="utf-8")
    return r


def _run(request, repo, replies, config=None, **kw):
    from harness import deps
    from harness.agent_loop import run_agent

    deps.set_call_model(Scripted(replies))
    try:
        return run_agent(
            request=request,
            repo_path=str(repo),
            config={"steering_enabled": False, **(config or {})},
            log_root=kw.get("log_root", repo.parent / "logs"),
            task_id=kw.get("task_id", "agent-test-1"),
            approve_fn=kw.get("approve_fn"),
        )
    finally:
        deps.reset_overrides()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestAgentClassify:
    @pytest.mark.parametrize("line", ["hi", "hello", "thanks", "ok", "bye"])
    def test_chitchat_never_launches(self, line):
        from harness.agent_loop import classify_agent_input

        it = classify_agent_input(line, config={})
        assert it.kind == "chit_chat", line
        assert it.reply.strip(), line

    @pytest.mark.parametrize(
        "line",
        [
            "explain how routing works",
            "how does the verify step work?",
            "what does harness/router.py do?",
            "describe how the agent loop works",
            "research how tree-sitter compares to other parsing libraries",
        ],
    )
    def test_questions_are_questions(self, line):
        from harness.agent_loop import classify_agent_input

        assert classify_agent_input(line, config={}).kind == "question", line

    @pytest.mark.parametrize(
        "line",
        [
            "add logging to src/router.py",
            "run pytest tests/test_cli.py and fix failures",
            "refactor the retry loop in core",
            "mean() in mathutil.py returns the sum; make it the mean",
            "fix the login bug where the password is empty",
            "debug why the suite hangs under load",
        ],
    )
    def test_work_is_agent_task(self, line):
        from harness.agent_loop import classify_agent_input

        assert classify_agent_input(line, config={}).kind == "agent_task", line

    def test_off_arm_is_all_task(self):
        from harness.agent_loop import classify_agent_input

        assert (
            classify_agent_input("hi", config={"agent_intent_enabled": False}).kind
            == "agent_task"
        )

    def test_gray_zone_model_failure_asks(self):
        from harness import deps
        from harness.agent_loop import classify_agent_input

        class Broken:
            def __call__(self, *a, **k):
                raise RuntimeError("down")

        deps.set_call_model(Broken())
        try:
            it = classify_agent_input("florp the wobble", config={})
        finally:
            deps.reset_overrides()
        assert it.kind == "chit_chat"
        assert it.reply.strip()


# ---------------------------------------------------------------------------
# Tool parsing
# ---------------------------------------------------------------------------


class TestParseToolCall:
    def test_json_forms(self):
        from harness.agent_loop import parse_tool_call

        assert parse_tool_call('{"tool": "read", "path": "a.py"}') == {
            "tool": "read",
            "path": "a.py",
        }
        assert parse_tool_call('```json\n{"tool":"done","answer":"ok"}\n```') == {
            "tool": "done",
            "answer": "ok",
        }

    def test_plain_forms(self):
        from harness.agent_loop import parse_tool_call

        assert parse_tool_call("READ src/router.py")["tool"] == "read"
        assert parse_tool_call("BASH python -m pytest -q")["tool"] == "bash"
        assert parse_tool_call("GREP def route src")["tool"] == "grep"
        assert parse_tool_call("GLOB **/*.py")["tool"] == "glob"
        assert parse_tool_call("MEMORY pytest conventions")["tool"] == "memory"
        assert parse_tool_call("VERIFY")["tool"] == "verify"
        assert parse_tool_call("DONE fixed it")["tool"] == "done"

    def test_garbage_is_none(self):
        from harness.agent_loop import parse_tool_call

        assert parse_tool_call("hello there") is None
        assert parse_tool_call("") is None
        assert parse_tool_call('{"tool": "nuke"}') is None


# ---------------------------------------------------------------------------
# Session wiring (the interactive `vex` engine, end to end with a
# scripted model — dispatch -> agent loop -> live edit -> session record)
# ---------------------------------------------------------------------------


class TestInteractiveAgentWiring:
    def _drive(self, lines, monkeypatch, tmp_path, repo):
        from cli import interactive

        monkeypatch.chdir(repo)
        # Scaffold isolation: the tmp repo is not a git repo, so repo
        # detection would walk up to a real ancestor repo — pin the
        # project dir into tmp instead.
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / ".vex"))
        it = iter(lines)

        def fake_input(prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)
        return interactive.run_interactive(log_root=tmp_path / "logs")

    def test_agent_sentence_edits_live_repo(self, tmp_path, monkeypatch, capsys):
        from harness import deps

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "router.py").write_text(
            "def route(kind):\n    return kind\n", encoding="utf-8"
        )
        deps.set_call_model(
            Scripted(
                [
                    '{"tool": "read", "path": "src/router.py"}',
                    '{"tool": "edit", "path": "src/router.py", '
                    '"old_string": "    return kind", '
                    '"new_string": "    print(kind)\\n    return kind"}',
                    '{"tool": "done", "answer": "added logging"}',
                ]
            )
        )
        try:
            rc = self._drive(
                ["add logging to src/router.py"], monkeypatch, tmp_path, repo
            )
        finally:
            deps.reset_overrides()
        assert rc == 0
        assert "print(kind)" in (repo / "src" / "router.py").read_text()
        out = capsys.readouterr().out
        assert "SUCCESS" in out
        # session recorded + trace.jsonl per session
        sessions = list((tmp_path / "logs").glob("agent-*/trace.jsonl"))
        assert len(sessions) == 1
        index = tmp_path / "logs" / ".vex-sessions.jsonl"
        assert index.is_file()

    def test_question_answers_without_mutation(self, tmp_path, monkeypatch, capsys):
        from harness import deps

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "router.py").write_text(
            "def route(kind):\n    return kind\n", encoding="utf-8"
        )
        deps.set_call_model(Scripted(['{"answer": "route returns its kind"}']))
        try:
            rc = self._drive(["explain how routing works"], monkeypatch, tmp_path, repo)
        finally:
            deps.reset_overrides()
        assert rc == 0
        assert (repo / "src" / "router.py").read_text() == (
            "def route(kind):\n    return kind\n"
        )
        assert "route returns its kind" in capsys.readouterr().out

    def test_hi_launches_nothing(self, tmp_path, monkeypatch, capsys):
        from harness import deps

        repo = tmp_path / "repo"
        repo.mkdir()

        class Explode:
            def __call__(self, *a, **k):
                raise AssertionError("chit-chat must not call the model")

        deps.set_call_model(Explode())
        try:
            rc = self._drive(["hi"], monkeypatch, tmp_path, repo)
        finally:
            deps.reset_overrides()
        assert rc == 0
        assert "task agent-" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_agent flows
# ---------------------------------------------------------------------------


class TestRunAgent:
    def test_done_immediately_is_success_no_diff(self, repo, tmp_path):
        out = _run(
            "explain how routing works",
            repo,
            ['{"tool": "done", "answer": "routing picks models"}'],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert "routing picks models" in out["answer"]
        assert out["diff"] == ""
        # live repo untouched
        assert (
            repo / "src" / "router.py"
        ).read_text() == "def route(kind):\n    return kind\n"
        # trace.jsonl per session
        trace = tmp_path / "logs" / "agent-test-1" / "trace.jsonl"
        assert trace.is_file()
        kinds = [json.loads(ln)["kind"] for ln in trace.read_text().splitlines()]
        assert "task_start" in kinds and "result" in kinds

    def test_read_then_edit_then_done(self, repo, tmp_path):
        out = _run(
            "add logging to src/router.py",
            repo,
            [
                '{"tool": "read", "path": "src/router.py"}',
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", "new_string": "    print(kind)\\n    return kind"}',
                '{"tool": "done", "answer": "added logging"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        text = (repo / "src" / "router.py").read_text()
        assert "print(kind)" in text
        assert out["diff"] and "print(kind)" in out["diff"]
        assert out["files_touched"] == ["src/router.py"]

    def test_undo_restores(self, repo, tmp_path):
        from harness.agent_loop import agent_diff, undo_edits

        _run(
            "add logging",
            repo,
            [
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", "new_string": "    return 42"}',
                '{"tool": "done", "answer": "ok"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert "return 42" in (repo / "src" / "router.py").read_text()
        assert "return 42" in agent_diff("agent-test-1", tmp_path / "logs", str(repo))
        res = undo_edits("agent-test-1", tmp_path / "logs", str(repo), steps=1)
        assert res["restored"] == ["src/router.py"]
        assert "return 42" not in (repo / "src" / "router.py").read_text()
        assert agent_diff("agent-test-1", tmp_path / "logs", str(repo)) == ""

    def test_bash_uses_sandbox(self, repo, tmp_path):
        from harness import deps
        from shared.types import ExecutionResult

        seen = []

        def fake_sandbox(repo_path, command, timeout_s=120, **kw):
            seen.append(command)
            return ExecutionResult(0, "1 passed", "", False)

        deps.set_call_model(
            Scripted(
                [
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                    '{"tool": "done", "answer": "green"}',
                ]
            )
        )
        try:
            deps.set_execute_sandboxed(fake_sandbox)
            from harness.agent_loop import run_agent

            out = run_agent(
                request="run pytest and fix failures",
                repo_path=str(repo),
                config={"steering_enabled": False},
                log_root=tmp_path / "logs",
                task_id="agent-bash-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert seen and "pytest" in seen[0]

    def test_no_verifier_by_default(self, repo, tmp_path):
        out = _run(
            "add logging",
            repo,
            ['{"tool": "done", "answer": "ok"}'],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert "verification" not in out

    def test_verifier_runs_when_tests_declared(self, repo, tmp_path, monkeypatch):
        import harness.agent_loop as al

        monkeypatch.setattr(
            al,
            "_run_verify",
            lambda repo_path, cfg, trace, emit: {
                "target_passed": True,
                "regression_passed": True,
                "flaky": False,
                "raw": "ok",
            },
        )
        out = _run(
            "fix it",
            repo,
            ['{"tool": "done", "answer": "ok"}'],
            config={"target_test": "tests/test_x.py::test_y"},
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert out["verification"]["target_passed"] is True

    def test_approval_require_refuses_without_approver(self, repo, tmp_path):
        out = _run(
            "add logging",
            repo,
            [
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", "new_string": "    return 42"}',
                '{"tool": "done", "answer": "done with reads"}',
            ],
            config={"agent_approval": "require", "agent_max_turns": 4},
            log_root=tmp_path / "logs",
        )
        # the edit was refused, the repo is unchanged, the loop still ends
        assert "return 42" not in (repo / "src" / "router.py").read_text()
        assert out["status"] == "success"

    def test_approval_require_allows_with_approver(self, repo, tmp_path):
        _run(
            "add logging",
            repo,
            [
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", "new_string": "    return 42"}',
                '{"tool": "done", "answer": "ok"}',
            ],
            config={"agent_approval": "require"},
            log_root=tmp_path / "logs",
            approve_fn=lambda tool, args, preview: True,
        )
        assert "return 42" in (repo / "src" / "router.py").read_text()

    def test_steering_abort(self, repo, tmp_path):
        from harness import deps, steering
        from harness.agent_loop import run_agent

        log_root = tmp_path / "logs"
        (log_root / "agent-steer-1").mkdir(parents=True)
        buf = steering.SteeringBuffer(log_root / "agent-steer-1", "agent-steer-1")
        buf.inject("stop, wrong approach", intent="abort", source="test")

        class Waiting:
            def __call__(self, messages, **kw):
                raise AssertionError("aborted runs must not call the model")

        deps.set_call_model(Waiting())
        try:
            out = run_agent(
                request="do something",
                repo_path=str(repo),
                config={},
                log_root=log_root,
                task_id="agent-steer-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "failed"
        assert "abort" in out["answer"].lower()

    def test_traversal_refused(self, repo, tmp_path):
        out = _run(
            "read secrets",
            repo,
            [
                '{"tool": "read", "path": "../outside.txt"}',
                '{"tool": "done", "answer": "refused as expected"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        trace = (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        assert "refused" in trace.lower()

    def test_glob_grep_memory_tools(self, repo, tmp_path):
        out = _run(
            "explore",
            repo,
            [
                '{"tool": "glob", "pattern": "src/*.py"}',
                '{"tool": "grep", "pattern": "def route"}',
                '{"tool": "memory", "query": "conventions"}',
                '{"tool": "done", "answer": "explored"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert (repo / "src" / "router.py").read_text().startswith("def route")

    def test_unknown_tool_is_honest_error(self, repo, tmp_path):
        from harness.agent_loop import parse_tool_call, route_tool

        # Unknown JSON tools never parse (retry nudge, never a crash)...
        assert parse_tool_call('{"tool": "frobnicate"}') is None
        assert route_tool("frobnicate", {}, {})["kind"] == "unknown"
        # ...and the loop recovers to DONE.
        out = _run(
            "do things",
            repo,
            [
                '{"tool": "frobnicate", "power": 11}',
                '{"tool": "done", "answer": "recovered"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert out["answer"] == "recovered"


# ---------------------------------------------------------------------------
# Agent plan preview (render_agent_plan + plan_guidance injection)
# ---------------------------------------------------------------------------


class TestAgentPlan:
    def test_render_plan_steps_and_files(self, repo):
        from harness.agent_loop import render_agent_plan

        plan = render_agent_plan("add logging to src/router.py", str(repo), {})
        assert len(plan["steps"]) >= 3
        assert plan["text"].strip()
        # lightweight heuristic: no verifier fabrications (no PASS claims,
        # no invented test node ids).
        assert "PASS" not in plan["text"]
        assert "::" not in plan["text"]

    def test_plan_guidance_injected_as_steering(self, repo, tmp_path):
        from harness import deps
        from harness.agent_loop import run_agent

        deps.set_call_model(Scripted(['{"tool": "done", "answer": "ok"}']))
        try:
            out = run_agent(
                request="add logging",
                repo_path=str(repo),
                config={"steering_enabled": False},
                log_root=tmp_path / "logs",
                task_id="agent-plan-1",
                plan_guidance="1. Explore\n2. Change\n3. Verify",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        kinds = [
            json.loads(ln)["kind"]
            for ln in (tmp_path / "logs" / "agent-plan-1" / "trace.jsonl")
            .read_text()
            .splitlines()
        ]
        assert "steering" in kinds


# ---------------------------------------------------------------------------
# FETCH tool (read-only web inside the agent loop)
# ---------------------------------------------------------------------------


class TestFetchTool:
    def test_parse_fetch_forms(self):
        from harness.agent_loop import parse_tool_call

        assert parse_tool_call("FETCH https://example.com/x") == {
            "tool": "fetch",
            "url": "https://example.com/x",
        }
        assert parse_tool_call('{"tool": "fetch", "url": "https://example.com/x"}') == {
            "tool": "fetch",
            "url": "https://example.com/x",
        }

    def test_fetch_disabled_is_honest_skip(self, repo, tmp_path):
        out = _run(
            "research the api",
            repo,
            [
                '{"tool": "fetch", "url": "https://example.com/x"}',
                '{"tool": "done", "answer": "skipped"}',
            ],
            config={"agent_fetch_enabled": False},
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"

    def test_fetch_success_and_no_shell_via_fetch(self, repo, tmp_path, monkeypatch):
        import harness.webfetch as wf
        from harness.webfetch import FetchResult

        def fake_fetch(url, **kw):
            res = FetchResult("ok", "docs text", url, True)
            try:
                kw.get("audit_hook")(res)
            except Exception:
                pass
            return f"FETCH results for {url}", res

        monkeypatch.setattr(wf, "fetch_and_render", fake_fetch)
        out = _run(
            "read the docs",
            repo,
            [
                '{"tool": "fetch", "url": "https://example.com/x"}',
                '{"tool": "bash", "command": "FETCH https://example.com/x"}',
                '{"tool": "done", "answer": "read"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        trace = (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        assert "web_fetch" in trace
        assert "use the fetch tool" in trace

    def test_fetch_budget_exhausts(self, repo, tmp_path, monkeypatch):
        import harness.webfetch as wf
        from harness.webfetch import FetchResult

        calls = []

        def fake_fetch(url, **kw):
            calls.append(url)
            res = FetchResult("ok", "t", url, True)
            try:
                kw.get("audit_hook")(res)
            except Exception:
                pass
            return "ok", res

        monkeypatch.setattr(wf, "fetch_and_render", fake_fetch)
        out = _run(
            "read a lot",
            repo,
            [
                '{"tool": "fetch", "url": "https://example.com/1"}',
                '{"tool": "fetch", "url": "https://example.com/2"}',
                '{"tool": "done", "answer": "enough"}',
            ],
            config={"agent_max_fetches": 1},
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert len(calls) == 1
        assert (
            "budget exhausted"
            in (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        )


# ---------------------------------------------------------------------------
# MCP + plugin verbs via the one tool_router
# ---------------------------------------------------------------------------


class TestToolRouter:
    def test_route_kinds(self):
        from harness.agent_loop import route_tool

        assert route_tool("read", {}, {})["kind"] == "builtin"
        assert route_tool("fetch", {}, {})["kind"] == "builtin"
        assert route_tool("mcp", {}, {})["kind"] == "mcp"
        assert route_tool("frobnicate", {}, {})["kind"] == "unknown"
        cfg = {"plugin_tool_verbs": ["myverb check"]}
        assert route_tool("myverb", {}, cfg)["kind"] == "plugin"

    def test_mcp_unknown_server_honest(self, repo, tmp_path):
        out = _run(
            "use mcp",
            repo,
            [
                '{"tool": "mcp", "server": "nope", "name": "t", "args": {}}',
                '{"tool": "done", "answer": "honest"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert (
            "unknown MCP server"
            in (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        )

    def test_mcp_call_live_once(self, repo, tmp_path, monkeypatch):
        import memory.mcp_client as mc

        seen = []

        def fake_call(server, tool, args, cwd=None):
            seen.append((server, tool, args))
            return {"ok": True, "text": "mcp says hi"}

        monkeypatch.setattr(mc, "call_mcp_tool", fake_call)
        out = _run(
            "use mcp",
            repo,
            [
                '{"tool": "mcp", "server": "demo", "name": "greet", "args": {"x": 1}}',
                '{"tool": "done", "answer": "called"}',
            ],
            config={"agent_mcp_servers": {"demo": "python -m demo_server"}},
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert seen == [("python -m demo_server", "greet", {"x": 1})]

    def test_mcp_failure_degrades_not_crashes(self, repo, tmp_path, monkeypatch):
        import memory.mcp_client as mc

        def boom(server, tool, args, cwd=None):
            raise RuntimeError("server down")

        monkeypatch.setattr(mc, "call_mcp_tool", boom)
        out = _run(
            "use mcp",
            repo,
            [
                '{"tool": "mcp", "server": "demo", "name": "t", "args": {}}',
                '{"tool": "done", "answer": "degraded"}',
            ],
            config={"agent_mcp_servers": {"demo": "python -m demo_server"}},
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert (
            "TOOL ERROR"
            in (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        )

    def test_mcp_needs_approval_in_require_mode(self, repo, tmp_path):
        out = _run(
            "use mcp",
            repo,
            [
                '{"tool": "mcp", "server": "demo", "name": "t", "args": {}}',
                '{"tool": "done", "answer": "refused"}',
            ],
            config={
                "agent_approval": "require",
                "agent_max_turns": 4,
                "agent_mcp_servers": {"demo": "python -m demo_server"},
            },
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"

    def test_plugin_readonly_runs_live(self, repo, tmp_path):
        from harness import deps
        from shared.types import ExecutionResult

        seen = []

        def fake_sandbox(repo_path, command, timeout_s=120, **kw):
            seen.append(command)
            return ExecutionResult(0, "checked", "", False)

        deps.set_call_model(
            Scripted(
                [
                    '{"tool": "myverb", "command": "check src"}',
                    '{"tool": "done", "answer": "plugged"}',
                ]
            )
        )
        try:
            deps.set_execute_sandboxed(fake_sandbox)
            from harness.agent_loop import run_agent

            out = run_agent(
                request="run the plugin check",
                repo_path=str(repo),
                config={
                    "steering_enabled": False,
                    "agent_approval": "require",
                    "plugin_tool_verbs": ["myverb check"],
                },
                log_root=tmp_path / "logs",
                task_id="agent-plugin-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert seen and seen[0].startswith("myverb")


# ---------------------------------------------------------------------------
# Undo hardening + live BASH semantics
# ---------------------------------------------------------------------------


class TestUndoHardening:
    def _two_edits(self, repo, tmp_path):
        return _run(
            "change two",
            repo,
            [
                '{"tool": "edit", "path": "src/router.py", '
                '"old_string": "    return kind", "new_string": "    return 1"}',
                '{"tool": "edit", "path": "src/util.py", '
                '"old_string": "X = 1", "new_string": "X = 2"}',
                '{"tool": "done", "answer": "ok"}',
            ],
            log_root=tmp_path / "logs",
        )

    def test_per_file_restore(self, repo, tmp_path):
        from harness.agent_loop import undo_edits

        self._two_edits(repo, tmp_path)
        res = undo_edits(
            "agent-test-1", tmp_path / "logs", str(repo), targets=["src/router.py"]
        )
        assert res["restored"] == ["src/router.py"]
        assert "return kind" in (repo / "src" / "router.py").read_text()
        assert "X = 2" in (repo / "src" / "util.py").read_text()

    def test_undo_all_deletes_created_and_logs_event(self, repo, tmp_path):
        from harness.agent_loop import agent_diff, undo_edits

        _run(
            "create",
            repo,
            [
                '{"tool": "write", "path": "src/newmod.py", "content": "Y = 3\\n"}',
                '{"tool": "done", "answer": "ok"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert (repo / "src" / "newmod.py").is_file()
        res = undo_edits("agent-test-1", tmp_path / "logs", str(repo), steps="all")
        assert res["deleted"] == ["src/newmod.py"]
        assert not (repo / "src" / "newmod.py").exists()
        trace = (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        assert '"kind":"undo"' in trace.replace(" ", "")
        # pristine/ never touched: still the reference (diff empty now).
        assert agent_diff("agent-test-1", tmp_path / "logs", str(repo)) == ""
        assert (
            tmp_path / "logs" / "agent-test-1" / "pristine" / "src" / "router.py"
        ).is_file()

    def test_bash_ctrl_c_stops_call_not_session(self, repo, tmp_path):
        from harness import deps

        calls = {"n": 0}

        def flaky(repo_path, command, timeout_s=120, **kw):
            from shared.types import ExecutionResult

            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt("ctrl+c")
            return ExecutionResult(0, "ok", "", False)

        deps.set_call_model(
            Scripted(
                [
                    '{"tool": "bash", "command": "python -m pytest -q"}',
                    '{"tool": "done", "answer": "survived"}',
                ]
            )
        )
        try:
            deps.set_execute_sandboxed(flaky)
            from harness.agent_loop import run_agent

            out = run_agent(
                request="run tests",
                repo_path=str(repo),
                config={"steering_enabled": False},
                log_root=tmp_path / "logs",
                task_id="agent-ctrlc-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert out["answer"] == "survived"

    def test_bash_deny_guard_live(self, repo, tmp_path):
        out = _run(
            "nuke",
            repo,
            [
                '{"tool": "bash", "command": "rm -rf /"}',
                '{"tool": "done", "answer": "guarded"}',
            ],
            log_root=tmp_path / "logs",
        )
        assert out["status"] == "success"
        assert (
            "REJECTED"
            in (tmp_path / "logs" / "agent-test-1" / "trace.jsonl").read_text()
        )


# ---------------------------------------------------------------------------
# Agent plan preview on the REPL (approve / edit-steers / cancel)
# ---------------------------------------------------------------------------


class TestAgentPlanPreviewREPL:
    def _repo(self, tmp_path):
        r = tmp_path / "repo"
        (r / "src").mkdir(parents=True)
        (r / "src" / "router.py").write_text(
            "def route(kind):\n    return kind\n", encoding="utf-8"
        )
        return r

    def test_approve_returns_request(self, tmp_path, monkeypatch, capsys):
        from cli import interactive as iv

        repo = self._repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        guidance = iv._agent_plan_preview("add logging to src/router.py", repo, {})
        assert guidance == "add logging to src/router.py"

    def test_edit_steers_guidance(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        repo = self._repo(tmp_path)
        answers = iter(["e", "only touch src/router.py"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        guidance = iv._agent_plan_preview("add logging to src/router.py", repo, {})
        assert guidance is not None and "only touch src/router.py" in guidance

    def test_cancel_returns_none(self, tmp_path, monkeypatch):
        from cli import interactive as iv

        repo = self._repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert iv._agent_plan_preview("add logging", repo, {}) is None

    def test_plan_then_run_end_to_end(self, tmp_path, monkeypatch):
        from cli import interactive as iv
        from harness import deps

        repo = self._repo(tmp_path)
        deps.set_call_model(Scripted(['{"tool": "done", "answer": "planned"}']))
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        try:
            out = iv._run_one_agent(
                "add logging to src/router.py",
                repo,
                {"repo": str(repo)},
                tmp_path / "logs",
                plan_guidance="approved plan",
            )
        finally:
            deps.reset_overrides()
        assert out is not None and out["status"] == "success"


# ---------------------------------------------------------------------------
# Agent resume: history replay, not a restart
# ---------------------------------------------------------------------------


class TestAgentResumeHistory:
    def _edit_once(self, repo, tmp_path, task_id="agent-resume-1"):
        return _run(
            "fix mean in src/util.py",
            repo,
            [
                '{"tool": "edit", "path": "src/util.py", '
                '"old_string": "X = 1", "new_string": "X = 2"}',
                '{"tool": "done", "answer": "bumped"}',
            ],
            log_root=tmp_path / "logs",
            task_id=task_id,
        )

    def test_load_resume_history_replays_request_and_files(self, repo, tmp_path):
        from harness.agent_loop import load_resume_history

        out = self._edit_once(repo, tmp_path)
        assert out["status"] == "success"
        hist = load_resume_history("agent-resume-1", tmp_path / "logs")
        assert "fix mean" in hist
        assert "src/util.py" in hist

    def test_load_resume_history_empty_when_no_trace(self, tmp_path):
        from harness.agent_loop import load_resume_history

        assert load_resume_history("nope", tmp_path / "logs") == ""
        assert load_resume_history("", tmp_path / "logs") == ""

    def test_resume_history_reaches_model_as_steering(self, repo, tmp_path):
        from harness import deps
        from harness.agent_loop import run_agent

        seen = []

        class Capturing(Scripted):
            def __call__(self, messages, **kw):
                seen.append([str(m.get("content", "")) for m in messages])
                return super().__call__(messages, **kw)

        deps.set_call_model(Capturing(['{"tool": "done", "answer": "ok"}']))
        try:
            out = run_agent(
                request="continue the fix",
                repo_path=str(repo),
                config={"steering_enabled": False, "plan_with_memory": False},
                log_root=tmp_path / "logs",
                task_id="agent-resume-2",
                resume_history="prior request: fix mean\nfiles touched: src/util.py",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert any("Prior session context" in c for msgs in seen for c in msgs)
        trace = (tmp_path / "logs" / "agent-resume-2" / "trace.jsonl").read_text()
        assert "agent-resume-history" in trace

    def test_resume_same_id_keeps_pristine_and_orig(self, repo, tmp_path):
        from harness import deps
        from harness.agent_loop import load_resume_history, run_agent

        self._edit_once(repo, tmp_path)
        pristine = tmp_path / "logs" / "agent-resume-1" / "pristine"
        orig = tmp_path / "logs" / "agent-resume-1" / "orig" / "src" / "util.py"
        assert pristine.is_dir() and orig.is_file()
        before = orig.read_bytes()
        hist = load_resume_history("agent-resume-1", tmp_path / "logs")
        deps.set_call_model(Scripted(['{"tool": "done", "answer": "still good"}']))
        try:
            out = run_agent(
                request="fix mean in src/util.py",
                repo_path=str(repo),
                config={"steering_enabled": False, "plan_with_memory": False},
                log_root=tmp_path / "logs",
                task_id="agent-resume-1",
                resume_history=hist,
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert pristine.is_dir()  # diff reference intact
        assert orig.read_bytes() == before  # first-edit original kept
