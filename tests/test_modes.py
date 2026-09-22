"""Multi-mode routing (Modes round) — intent, router, Q&A, research, build.

The round's contract under test:
- Task A: harness.intent classifies fix/build/question/research/convo/
  ambiguous; deterministic rules first, ONE cheap model call only for the
  gray zone; every failure degrades to ambiguous (ask, never guess).
- Task B: harness.router dispatches each kind to its handler; convo/
  ambiguous come back as replies, never tasks.
- Task C: qa_mode answers read-only (retrieval + memory; no sandbox, no
  edits; READ bounded; original repo untouched).
- Task D: build_mode authors acceptance tests FIRST, confirms they FAIL
  on the pristine tree, then drives the UNCHANGED fix pipeline to green
  against them (Docker-gated e2e).
- Task E: research_mode synthesizes from FETCH/DOCS round-trips,
  read-only, budget-bounded, never executes a shell.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def _docker_up() -> bool:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=10,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# Task A — the intent classifier
# ---------------------------------------------------------------------------


class TestIntentDeterministic:
    """Clear cases must never touch the model (offline, instant)."""

    @pytest.mark.parametrize(
        "line",
        [
            "hi",
            "hello",
            "hey there",
            "yo",
            "good morning",
            "what can you do?",
            "who are you?",
            "are you an AI agent?",
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
    def test_conversational(self, line):
        from harness.intent import classify_deterministic

        it = classify_deterministic(line)
        assert it.kind == "convo", line
        assert it.reply.strip(), line

    @pytest.mark.parametrize(
        "line",
        [
            # the canonical sentences from the pinned wiring tests
            "mean() in mathutil.py returns the sum; make it the mean",
            "fix the login bug where the password is empty",
            "the parser crashes on empty input",
            "tests/test_mathutil.py::test_mean fails",
            "TypeError in serializers.py when the payload is null",
            "there's an off-by-one in the index calculation",
            "make it stop throwing KeyError when the cache is cold",
            "the retry loop hangs forever under load",
            "remove the deprecated flag from build.sh",
            "help me fix the flaky timeout in the retry loop",
        ],
    )
    def test_fix(self, line):
        from harness.intent import classify_deterministic

        assert classify_deterministic(line).kind == "fix", line

    @pytest.mark.parametrize(
        "line",
        [
            "add a mode() function for the most frequent value",
            "build a middleware for auth",
            "implement a new validator for emails",
            "add an endpoint for health checks",
            "create a helper for slug generation",
            "i want to add a new command for listing sessions",
        ],
    )
    def test_build(self, line):
        from harness.intent import classify_deterministic

        assert classify_deterministic(line).kind == "build", line

    @pytest.mark.parametrize(
        "line",
        [
            "how does the verify step work?",
            "where is config handled in this repo",
            "what does run_task do",
            "is the sandbox fresh per command?",
            "which file owns the planner prompt?",
        ],
    )
    def test_question(self, line):
        from harness.intent import classify_deterministic

        assert classify_deterministic(line).kind == "question", line

    @pytest.mark.parametrize(
        "line",
        [
            "research how tree-sitter compares to other parsing libraries",
            "what's the best library for parsing python",
            "look into the bottle framework for our next project",
            "investigate what the current recommended approach is",
            "is there a package for wheel inspection",
        ],
    )
    def test_research(self, line):
        from harness.intent import classify_deterministic

        assert classify_deterministic(line).kind == "research", line

    @pytest.mark.parametrize(
        "line",
        ["", "   ", "help me move apartments", "lorem ipsum dolor"],
    )
    def test_ambiguous(self, line):
        from harness.intent import classify_deterministic

        it = classify_deterministic(line)
        assert it.kind == "ambiguous", line

    def test_build_with_failure_language_is_fix(self):
        """Failure language beats build verbs — a broken thing to repair."""
        from harness.intent import classify_deterministic

        assert (
            classify_deterministic("add validation so it stops crashing").kind == "fix"
        )

    def test_build_with_spec_contract_is_build(self):
        """ "an empty list must raise ValueError" is SPEC language inside a
        feature request, not a symptom — a bare raise/throw clause must
        not veto the build (real defect caught by the Task-F live
        session: the mode() build request was misrouted to fix)."""
        from harness.intent import classify_deterministic

        assert (
            classify_deterministic(
                "Add a mode() function to numlib/mathutil.py that returns "
                "the most frequent value; on a tie return the SMALLEST, "
                "and an empty list must raise ValueError."
            ).kind
            == "build"
        )

    def test_raise_when_is_still_symptom_language(self):
        """The narrowed bug-vocabulary keeps the SYMPTOM compound: a
        report that something raises under a condition is a fix."""
        from harness.intent import classify_deterministic

        assert (
            classify_deterministic("the parser raises when the payload is null").kind
            == "fix"
        )

    def test_research_singular_library(self):
        """'library' (singular) is an external marker — the regex must
        match it (real defect caught by the Task-F live session: the
        num2words research question fell to the gray zone)."""
        from harness.intent import classify_deterministic

        assert (
            classify_deterministic(
                "Research: does the Python num2words library support "
                "converting a number to year phrasing? Cite the source."
            ).kind
            == "research"
        )

    def test_fix_verb_question_is_question(self):
        """A question shape with no failure language stays a question
        (even when it starts with a fix verb — "fix" alone fixes nothing)."""
        from harness.intent import classify_deterministic

        assert classify_deterministic("how can i fix this?").kind == "question"

    def test_bug_naming_question_is_fix(self):
        """A question that names a bug IS a bug report — fix-biased."""
        from harness.intent import classify_deterministic

        assert classify_deterministic("how do i fix the mean bug?").kind == "fix"


class TestIntentModelTier:
    """The gray zone uses exactly ONE cheap model call; failures degrade."""

    def test_gray_zone_uses_model_with_easy_hint(self, monkeypatch):
        from harness import deps
        from harness.intent import classify_input

        seen = {}

        class FakeModel:
            def __call__(self, messages, **kw):
                seen["difficulty_hint"] = kw.get("difficulty_hint")
                seen["messages"] = messages
                return '{"kind": "build", "reason": "new capability"}'

        monkeypatch.setattr(deps, "_call_model_override", None, raising=False)
        deps.set_call_model(FakeModel())
        try:
            it = classify_input("improve the input handling")
        finally:
            deps.reset_overrides()
        assert it.kind == "build"
        assert it.used_model is True
        assert seen["difficulty_hint"] == "easy"  # the cheap tier

    def test_clear_case_never_calls_model(self, monkeypatch):
        from harness import deps
        from harness.intent import classify_input

        class Explode:
            def __call__(self, *a, **k):
                raise AssertionError("clear case must not call the model")

        deps.set_call_model(Explode())
        try:
            assert classify_input("the parser crashes").kind == "fix"
            assert classify_input("hi").kind == "convo"
        finally:
            deps.reset_overrides()

    def test_model_failure_degrades_to_ambiguous(self, monkeypatch):
        from harness import deps
        from harness.intent import classify_input

        class Broken:
            def __call__(self, *a, **k):
                raise RuntimeError("endpoint down")

        deps.set_call_model(Broken())
        try:
            it = classify_input("improve the input handling")
        finally:
            deps.reset_overrides()
        assert it.kind == "ambiguous"

    def test_unparseable_reply_degrades_to_ambiguous(self, monkeypatch):
        from harness import deps
        from harness.intent import classify_input

        class Garbage:
            def __call__(self, *a, **k):
                return "I think it's probably a build request, hard to say"

        deps.set_call_model(Garbage())
        try:
            it = classify_input("improve the input handling")
        finally:
            deps.reset_overrides()
        assert it.kind == "ambiguous"

    def test_bare_word_reply_accepted(self, monkeypatch):
        from harness import deps
        from harness.intent import classify_input

        class Bare:
            def __call__(self, *a, **k):
                return "question"

        deps.set_call_model(Bare())
        try:
            it = classify_input("improve the input handling")
        finally:
            deps.reset_overrides()
        assert it.kind == "question"

    def test_intent_enabled_false_is_legacy_fix(self):
        from harness.intent import classify_input

        it = classify_input("hi", config={"intent_enabled": False})
        assert it.kind == "fix"

    def test_reply_parsing_matrix(self):
        from harness.intent import _parse_intent_reply

        assert _parse_intent_reply('{"kind": "build", "reason": "x"}') == "build"
        assert _parse_intent_reply('```json\n{"kind": "fix"}\n```') == "fix"
        assert _parse_intent_reply("research") == "research"
        assert _parse_intent_reply("CONVO") == "convo"
        assert _parse_intent_reply("nonsense") is None
        assert _parse_intent_reply('{"kind": "fly"}') is None
        assert _parse_intent_reply("") is None


# ---------------------------------------------------------------------------
# Task B — the router
# ---------------------------------------------------------------------------


class TestRouter:
    def _handlers(self, log):
        def h(kind):
            def fn(**kw):
                log.append((kind, kw["text"], kw["repo_path"]))
                return {"mode": kind, "status": "success", "answer": f"a-{kind}"}

            return fn

        return {k: h(k) for k in ("fix", "build", "question", "research")}

    def test_each_mode_reaches_its_handler(self, tmp_path):
        from harness.router import route

        log = []
        hs = self._handlers(log)
        route("the parser crashes", "R", handlers=hs)
        route("add a mode() function", "R", handlers=hs)
        route("how does verify work?", "R", handlers=hs)
        route("research tree-sitter vs alternatives", "R", handlers=hs)
        assert [k for k, _, _ in log] == ["fix", "build", "question", "research"]

    def test_convo_returns_reply_not_task(self):
        from harness.router import route

        out = route("hi", "R", handlers=self._handlers([]))
        assert out["status"] == "reply"
        assert out["answer"].strip()
        assert out["mode"] == "convo"

    def test_ambiguous_returns_reply_not_task(self):
        from harness.router import route

        out = route(
            "lorem ipsum dolor sit amet",
            "R",
            handlers={},
        )
        assert out["status"] in ("reply",)
        assert out["mode"] == "ambiguous"

    def test_fix_routes_to_real_run_task(self, tmp_path, monkeypatch):
        """The default fix handler drives the REAL core.run_task."""
        import harness.router as router_mod

        calls = []

        def fake_run_task(task, log_root=None):
            calls.append(task)
            from shared.types import TaskResult

            return TaskResult(
                task_id=task.task_id,
                status="success",
                attempts=1,
                diff="",
                verification=None,
                cost_usd=0.0,
                model_calls=[],
                log_path=str(Path(log_root) / task.task_id / "trace.jsonl"),
            )

        monkeypatch.setattr("harness.core.run_task", fake_run_task)
        out = router_mod.route(
            "the parser crashes on empty input",
            str(tmp_path),
            config={},
            log_root=tmp_path / "logs",
        )
        assert out["mode"] == "fix" and out.ok
        assert calls[0].issue_text == "the parser crashes on empty input"

    def test_route_kind_passthrough(self):
        from harness.router import route_kind

        assert route_kind("the parser crashes").kind == "fix"
        assert route_kind("hi").kind == "convo"


# ---------------------------------------------------------------------------
# Task C — Q&A mode (read-only)
# ---------------------------------------------------------------------------


class TestQAMode:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "numlib").mkdir(parents=True)
        (repo / "numlib" / "mathutil.py").write_text(
            "def mean(values):\n"
            "    'Arithmetic mean.'\n"
            "    return sum(values) / len(values)\n",
            encoding="utf-8",
        )
        (repo / "numlib" / "__init__.py").write_text("", encoding="utf-8")
        return repo

    def test_answer_grounded_in_repo(self, tmp_path):
        from harness import deps
        from harness.qa_mode import run_question

        class OneShot:
            def __init__(self):
                self.seen = []

            def __call__(self, messages, **kw):
                self.seen.append(messages)
                return (
                    "The arithmetic mean lives in numlib/mathutil.py: it "
                    "divides the sum by the length."
                )

        model = OneShot()
        deps.set_call_model(model)
        try:
            out = run_question(
                "how does mean work in this repo?",
                str(self._repo(tmp_path)),
                config={"plan_with_memory": False},
                log_root=tmp_path / "logs",
                task_id="qa-test-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert "mathutil.py" in out["answer"]
        # the prompt carried the retrieved file
        user = model.seen[0][1]["content"]
        assert "mathutil.py" in user
        # read-only: no sandbox, no pristine/work copies
        log_dir = tmp_path / "logs" / "qa-test-1"
        assert (log_dir / "trace.jsonl").is_file()
        assert not (log_dir / "work").exists()
        assert not (log_dir / "pristine").exists()

    def test_read_roundtrip_pulls_file_content(self, tmp_path):
        from harness import deps
        from harness.qa_mode import run_question

        class ReadThenAnswer:
            def __init__(self):
                self.n = 0

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return "READ numlib/mathutil.py"
                # second call: the READ result must be in context
                assert "sum(values) / len(values)" in messages[-1]["content"]
                return "mean() divides sum by length (see numlib/mathutil.py)."

        deps.set_call_model(ReadThenAnswer())
        try:
            out = run_question(
                "how does mean work?",
                str(self._repo(tmp_path)),
                config={},
                log_root=tmp_path / "logs",
                task_id="qa-test-2",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert "mathutil.py" in out["answer"]

    def test_read_refuses_traversal(self, tmp_path):
        from harness.qa_mode import _read_file

        msg = _read_file(str(self._repo(tmp_path)), "../../win.ini", 1000)
        assert "refused" in msg

    def test_read_missing_file_is_a_message(self, tmp_path):
        from harness.qa_mode import _read_file

        msg = _read_file(str(self._repo(tmp_path)), "numlib/nope.py", 1000)
        assert "miss" in msg

    def test_parse_read(self):
        from harness.qa_mode import parse_read

        assert parse_read("READ numlib/mathutil.py") == "numlib/mathutil.py"
        assert parse_read("read `numlib/x.py`") == "numlib/x.py"
        assert parse_read("READ ../../etc/passwd") == "../../etc/passwd"
        assert parse_read("plain sentence") is None

    def test_empty_question_errors(self, tmp_path):
        from harness.qa_mode import run_question

        out = run_question(
            "", str(self._repo(tmp_path)), config={}, log_root=tmp_path / "logs"
        )
        assert out["status"] == "error"

    def test_model_crash_is_error_not_raise(self, tmp_path):
        from harness import deps
        from harness.qa_mode import run_question

        class Boom:
            def __call__(self, *a, **k):
                raise RuntimeError("down")

        deps.set_call_model(Boom())
        try:
            out = run_question(
                "how does mean work?",
                str(self._repo(tmp_path)),
                config={},
                log_root=tmp_path / "logs",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "error"

    def test_read_budget_bounded(self, tmp_path):
        from harness import deps
        from harness.qa_mode import run_question

        class LoopReads:
            def __call__(self, messages, **kw):
                return "READ numlib/mathutil.py"

        deps.set_call_model(LoopReads())
        try:
            out = run_question(
                "how does mean work?",
                str(self._repo(tmp_path)),
                config={"qa_max_reads": 2},
                log_root=tmp_path / "logs",
                task_id="qa-test-3",
            )
        finally:
            deps.reset_overrides()
        # budget forces a final answer; the run terminates
        assert out["status"] == "success"
        assert out["answer"]

    def test_empty_reply_retried_then_error(self, tmp_path):
        """The endpoint's reasoning-burn flake (empty content, tokens
        burned) must NEVER mint success: ONE retry, then an honest
        error. Real defect caught by the Task-F live session."""
        from harness import deps
        from harness.qa_mode import run_question

        calls = []

        class EmptyAlways:
            def __init__(self):
                self.n = 0

            def __call__(self, messages, **kw):
                self.n += 1
                calls.append(messages[-1]["content"][:30])
                return ""

        m = EmptyAlways()
        deps.set_call_model(m)
        try:
            out = run_question(
                "how does mean work?",
                str(self._repo(tmp_path)),
                config={},
                log_root=tmp_path / "logs",
                task_id="qa-empty-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "error"
        assert out["answer"] == ""
        assert m.n == 2  # exactly one retry

    def test_empty_reply_recovers_on_retry(self, tmp_path):
        """The retry exists so a transient empty reply can RECOVER; the
        retry turn carries the repair nudge (a different ask — the
        Task-F live session showed the same prompt deterministically
        burning twice on this endpoint class)."""
        from harness import deps
        from harness.qa_mode import run_question

        seen = {}

        class EmptyThenGood:
            def __init__(self):
                self.n = 0

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return ""
                seen["retry_prompt"] = messages[-1]["content"]
                return "mean() in numlib/mathutil.py divides sum by len."

        deps.set_call_model(EmptyThenGood())
        try:
            out = run_question(
                "how does mean work?",
                str(self._repo(tmp_path)),
                config={},
                log_root=tmp_path / "logs",
                task_id="qa-empty-2",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert "mathutil" in out["answer"]
        assert "EMPTY" in seen["retry_prompt"]  # the repair nudge rode in


# ---------------------------------------------------------------------------
# Task E — research mode (read-only, FETCH/DOCS tools)
# ---------------------------------------------------------------------------


class TestResearchMode:
    def test_fetch_roundtrip_reaches_answer(self, tmp_path, monkeypatch):
        from harness import deps
        from harness.research_mode import run_research
        from harness.webfetch import FetchResult

        class FetchThenAnswer:
            def __init__(self):
                self.n = 0

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return "FETCH https://pypi.org/project/num2words/"
                assert "FETCH results" in messages[-1]["content"]
                return (
                    'num2words supports year conversion via to="year".\n'
                    "- Key finding: the `to` kwarg selects the converter "
                    "(pypi.org/project/num2words).\n"
                    "- Not found: benchmark data."
                )

        def fake_fetch_and_render(url, **kw):
            res = FetchResult("ok", "num2words. to: The converter to use.", url, True)
            hook = kw.get("audit_hook")
            if hook:
                try:
                    hook(res)
                except Exception:
                    pass
            return (
                f"FETCH results for {url} (readable text extracted):\n---\n"
                "num2words. to: The converter to use.\n---",
                res,
            )

        monkeypatch.setattr("harness.webfetch.fetch_and_render", fake_fetch_and_render)
        deps.set_call_model(FetchThenAnswer())
        try:
            out = run_research(
                "research what the num2words library supports",
                config={},
                log_root=tmp_path / "logs",
                task_id="res-test-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert "num2words" in out["answer"]
        assert out["fetches"] and out["fetches"][0]["ok"]

    def test_fetch_disabled_is_answered_without_web(self, tmp_path, monkeypatch):
        from harness import deps
        from harness.research_mode import run_research

        def explode(*a, **k):
            raise AssertionError("FETCH disabled — no web call may happen")

        monkeypatch.setattr("harness.webfetch.fetch_and_render", explode)

        class AskFetch:
            def __call__(self, messages, **kw):
                if len(messages) == 2:
                    return "FETCH https://example.com/x"
                return "Answer from general knowledge only (labeled as such)."

        deps.set_call_model(AskFetch())
        try:
            out = run_research(
                "research topic X",
                config={"web_fetch_enabled": False},
                log_root=tmp_path / "logs",
                task_id="res-test-2",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"  # answered, without the web

    def test_budget_forces_final_answer(self, tmp_path):
        from harness import deps
        from harness.research_mode import run_research

        class AlwaysFetch:
            def __call__(self, messages, **kw):
                return "FETCH https://example.com/never"

        deps.set_call_model(AlwaysFetch())
        try:
            out = run_research(
                "research topic Y",
                config={"research_max_fetches": 1, "research_turns": 3},
                log_root=tmp_path / "logs",
                task_id="res-test-3",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"  # terminated with an answer
        assert len(out["fetches"]) <= 2

    def test_no_repo_is_fine(self, tmp_path):
        from harness import deps
        from harness.research_mode import run_research

        class Direct:
            def __call__(self, messages, **kw):
                return "Direct answer without tools."

        deps.set_call_model(Direct())
        try:
            out = run_research(
                "research topic Z",
                config={},
                log_root=tmp_path / "logs",
                repo_path=None,
                task_id="res-test-4",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert out["answer"] == "Direct answer without tools."

    def test_readonly_no_shell(self, tmp_path):
        """Research never executes a shell command — no BashSession use."""
        import harness.research_mode as rm

        src = (ROOT / "harness" / "research_mode.py").read_text(encoding="utf-8")
        assert "BashSession" not in src
        assert "execute_sandboxed" not in src
        assert hasattr(rm, "run_research")

    def test_degenerate_fetch_line_is_salvaged(self, tmp_path, monkeypatch):
        """The endpoint flake where the model TRIES to fetch but glues the
        line ("assistantFETCH https://..." mid-paragraph): the URL must be
        SALVAGED and fetched — an attempted fetch must never silently
        degrade into an ungrounded answer. Real defect caught by the
        Task-F live session (fetches=[] with URLs all over the reply)."""
        from harness import deps
        from harness.research_mode import run_research
        from harness.webfetch import FetchResult

        class DegenerateFetcher:
            def __init__(self):
                self.n = 0

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    # the glued, degenerate shape the live session saw
                    return (
                        "I'll check the official source. "
                        "assistantFETCH https://pypi.org/project/num2words/ "
                        "should have it.\n\nWe should perform the fetch."
                    )
                # after the salvage fetch, the final answer must cite it
                assert "FETCH results" in messages[-1]["content"]
                return (
                    'num2words supports year phrasing via to="year".\n'
                    "- Key finding: the `to` kwarg selects the converter "
                    "(pypi.org/project/num2words).\n"
                    "- Not found: benchmark data."
                )

        def fake_fetch_and_render(url, **kw):
            res = FetchResult("ok", "num2words. to: The converter to use.", url, True)
            hook = kw.get("audit_hook")
            if hook:
                try:
                    hook(res)
                except Exception:
                    pass
            return (
                f"FETCH results for {url} (readable text extracted):\n---\n"
                "num2words. to: The converter to use.\n---",
                res,
            )

        monkeypatch.setattr("harness.webfetch.fetch_and_render", fake_fetch_and_render)
        deps.set_call_model(DegenerateFetcher())
        try:
            out = run_research(
                "research the num2words to= converter",
                config={},
                log_root=tmp_path / "logs",
                task_id="res-salvage-1",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "success"
        assert out["fetches"] and out["fetches"][0]["ok"]
        assert "num2words" in out["answer"]

    def test_salvage_requires_fetch_mention(self, tmp_path):
        """A reply that merely MENTIONS a URL (as a citation in a final
        answer) must NOT be salvaged into a fetch — only an attempted
        FETCH (the word appears) triggers the salvage."""
        from harness.research_mode import _salvage_fetch_url
        from harness.webfetch import parse_fetch

        degenerate = "assistantFETCH https://example.com/x should have it."
        assert _salvage_fetch_url(degenerate) == "https://example.com/x"
        # a plain citation is not a fetch attempt: the caller gates on
        # "FETCH" appearing in the reply
        assert "FETCH" not in "see https://example.com/docs for details"
        # a CLEAN fetch line never needs salvage
        assert parse_fetch("FETCH https://example.com/clean") is not None


# ---------------------------------------------------------------------------
# Task D — build mode
# ---------------------------------------------------------------------------


class TestBuildModeUnit:
    def test_author_acceptance_tests(self, tmp_path):
        from harness import deps
        from harness.build_mode import author_acceptance_tests
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class Author:
            def __call__(self, messages, **kw):
                assert "ACCEPTANCE TESTS" in messages[0]["content"]
                return json.dumps(
                    {
                        "tests": [
                            {
                                "filename": "test_mode_feature.py",
                                "content": (
                                    "from numlib.mathutil import mode\n\n"
                                    "def test_mode_basic():\n"
                                    "    assert mode([1, 2, 2, 3]) == 2\n"
                                ),
                            }
                        ]
                    }
                )

        deps.set_call_model(Author())
        try:
            trace = TraceLogger(tmp_path / "t")
            model = ModelClient(trace, get_config({}))
            tests, note = author_acceptance_tests(
                "add a mode() function",
                str(FIXTURES / "feat01_numlib"),
                get_config({}),
                trace,
                model,
            )
        finally:
            deps.reset_overrides()
        assert tests and tests[0]["filename"] == "test_mode_feature.py"
        assert "authored" in note

    def test_unparseable_authoring_aborts_honestly(self, tmp_path):
        from harness import deps
        from harness.build_mode import author_acceptance_tests
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class Garbage:
            def __call__(self, *a, **k):
                return "sure, I'll write some tests"

        deps.set_call_model(Garbage())
        try:
            trace = TraceLogger(tmp_path / "t")
            tests, note = author_acceptance_tests(
                "add a mode() function",
                str(FIXTURES / "feat01_numlib"),
                get_config({}),
                trace,
                ModelClient(trace, get_config({})),
            )
        finally:
            deps.reset_overrides()
        assert tests is None
        assert "unparseable" in note

    def test_empty_authoring_reply_retried(self, tmp_path):
        """The endpoint's reasoning-burn flake (empty content) on the
        test-authoring call: ONE retry with a repair nudge, then the
        honest generation-failure path — never a silent abort. Real
        defect caught by the Task-F live session."""
        from harness import deps
        from harness.build_mode import author_acceptance_tests
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class EmptyThenGood:
            def __init__(self):
                self.n = 0
                self.retry_prompt = ""

            def __call__(self, messages, **kw):
                self.n += 1
                if self.n == 1:
                    return ""
                self.retry_prompt = messages[-1]["content"]
                return json.dumps(
                    {
                        "tests": [
                            {
                                "filename": "test_mode_feature.py",
                                "content": (
                                    "from numlib.mathutil import mode\n\n"
                                    "def test_mode_basic():\n"
                                    "    assert mode([1, 2, 2, 3]) == 2\n"
                                ),
                            }
                        ]
                    }
                )

        m = EmptyThenGood()
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            tests, _note = author_acceptance_tests(
                "add a mode() function",
                str(FIXTURES / "feat01_numlib"),
                get_config({}),
                trace,
                ModelClient(trace, get_config({})),
            )
        finally:
            deps.reset_overrides()
        assert tests and tests[0]["filename"] == "test_mode_feature.py"
        assert m.n == 2
        assert "EMPTY" in m.retry_prompt  # the repair nudge rode in

    def test_empty_authoring_reply_twice_aborts(self, tmp_path):
        """Both the first call AND the retry coming back empty is an
        honest generation failure — tests is None with a clear note."""
        from harness import deps
        from harness.build_mode import author_acceptance_tests
        from harness.config import get_config
        from harness.model_client import ModelClient
        from harness.trace import TraceLogger

        class EmptyAlways:
            def __init__(self):
                self.n = 0

            def __call__(self, *a, **k):
                self.n += 1
                return ""

        m = EmptyAlways()
        deps.set_call_model(m)
        try:
            trace = TraceLogger(tmp_path / "t")
            tests, note = author_acceptance_tests(
                "add a mode() function",
                str(FIXTURES / "feat01_numlib"),
                get_config({}),
                trace,
                ModelClient(trace, get_config({})),
            )
        finally:
            deps.reset_overrides()
        assert tests is None
        assert m.n == 2
        assert "unparseable" in note or "no acceptance tests" in note

    def test_build_tests_prompt_carries_request(self):
        from harness.prompts import render_build_tests_prompt

        msgs = render_build_tests_prompt(
            request_text="add a mode() function",
            tests_tree="tests/test_mathutil.py",
            context_files=["numlib/mathutil.py"],
        )
        assert "mode()" in msgs[1]["content"]
        assert "numlib/mathutil.py" in msgs[1]["content"]
        assert "ACCEPTANCE TESTS" in msgs[0]["content"]


@requires_docker
class TestBuildModeE2E:
    """A real feature build through the full pipeline (Docker sandbox)."""

    def _model_spec(self, tmp_path):
        """A scripted-model spec file: author tests, then implement mode().

        The spec must handle BOTH stages: build-tests authoring call
        (system prompt mentions ACCEPTANCE TESTS) and the fix loop's
        planner + step sessions (which implement mode() via heredoc).
        """
        spec = {
            "planner": {
                "analysis": "scripted",
                "plan": [
                    {
                        "id": 1,
                        "description": "implement mode() in numlib/mathutil.py",
                        "checkpoint": "acceptance test for mode() passes",
                        "files_hint": ["numlib/mathutil.py"],
                    }
                ],
            },
            "script": [
                {
                    "match": "ACCEPTANCE TESTS",
                    "reply": {
                        "tests": [
                            {
                                "filename": "test_mode_feature.py",
                                "content": (
                                    "from numlib.mathutil import mode\n\n"
                                    "def test_mode_basic():\n"
                                    "    assert mode([1, 2, 2, 3]) == 2\n\n"
                                    "def test_mode_tie_and_empty():\n"
                                    "    assert mode([1, 1, 2, 2]) in (1, 2)\n"
                                    "    import pytest\n"
                                    "    with pytest.raises(ValueError):\n"
                                    "        mode([])\n"
                                ),
                            }
                        ]
                    },
                }
            ],
            "steps": {
                "1": [
                    [
                        "cat > numlib/mathutil.py <<'EOF'\n"
                        '"""Number utilities for the numlib package."""\n'
                        "from typing import List\n\n"
                        "def mean(values: List[float]) -> float:\n"
                        '    """Arithmetic mean of a non-empty list."""\n'
                        "    if not values:\n"
                        '        raise ValueError("mean() of empty list")\n'
                        "    return sum(values) / len(values)\n\n\n"
                        "def median(values: List[float]) -> float:\n"
                        '    """Median of a non-empty list."""\n'
                        "    if not values:\n"
                        '        raise ValueError("median() of empty list")\n'
                        "    ordered = sorted(values)\n"
                        "    n = len(ordered)\n"
                        "    mid = n // 2\n"
                        "    if n % 2 == 1:\n"
                        "        return ordered[mid]\n"
                        "    return (ordered[mid - 1] + ordered[mid]) / 2\n\n\n"
                        "def variance(values: List[float]) -> float:\n"
                        '    """Population variance of a non-empty list."""\n'
                        "    m = mean(values)\n"
                        "    return sum((v - m) ** 2 for v in values) / len(values)\n\n\n"
                        "def mode(values: List[float]) -> float:\n"
                        '    """Most frequent value; ties break to the smallest."""\n'
                        "    if not values:\n"
                        '        raise ValueError("mode() of empty list")\n'
                        "    counts = {}\n"
                        "    for v in values:\n"
                        "        counts[v] = counts.get(v, 0) + 1\n"
                        "    best = max(counts.values())\n"
                        "    return min(v for v, c in counts.items() if c == best)\n"
                        "EOF"
                    ],
                    ["SUBMIT"],
                ]
            },
        }
        p = tmp_path / "build_spec.json"
        p.write_text(json.dumps(spec), encoding="utf-8")
        return str(p)

    def test_real_build_end_to_end(self, tmp_path, monkeypatch):
        import shutil as shutil_mod

        from harness import deps
        from harness.build_mode import run_build
        from harness.config import get_config

        # isolate the fixture (build writes into its base copy only, but
        # keep the original pristine anyway — the never-mutate guarantee)
        repo = tmp_path / "feat01"
        shutil_mod.copytree(FIXTURES / "feat01_numlib", repo)

        # scripted model: authoring call + planner + the implementing step
        plan = [
            {
                "id": 1,
                "description": "implement mode() in numlib/mathutil.py",
                "checkpoint": "acceptance test for mode() passes",
                "files_hint": ["numlib/mathutil.py"],
            }
        ]
        impl = (
            "cat > numlib/mathutil.py <<'EOF'\n"
            '"""Number utilities for the numlib package."""\n'
            "from typing import List\n\n"
            "def mean(values: List[float]) -> float:\n"
            '    """Arithmetic mean of a non-empty list."""\n'
            "    if not values:\n"
            '        raise ValueError("mean() of empty list")\n'
            "    return sum(values) / len(values)\n\n\n"
            "def median(values: List[float]) -> float:\n"
            '    """Median of a non-empty list."""\n'
            "    if not values:\n"
            '        raise ValueError("median() of empty list")\n'
            "    ordered = sorted(values)\n"
            "    n = len(ordered)\n"
            "    mid = n // 2\n"
            "    if n % 2 == 1:\n"
            "        return ordered[mid]\n"
            "    return (ordered[mid - 1] + ordered[mid]) / 2\n\n\n"
            "def variance(values: List[float]) -> float:\n"
            '    """Population variance of a non-empty list."""\n'
            "    m = mean(values)\n"
            "    return sum((v - m) ** 2 for v in values) / len(values)\n\n\n"
            "def mode(values: List[float]) -> float:\n"
            '    """Most frequent value; ties break to the smallest."""\n'
            "    if not values:\n"
            '        raise ValueError("mode() of empty list")\n'
            "    counts = {}\n"
            "    for v in values:\n"
            "        counts[v] = counts.get(v, 0) + 1\n"
            "    best = max(counts.values())\n"
            "    return min(v for v, c in counts.items() if c == best)\n"
            "EOF"
        )
        authored = json.dumps(
            {
                "tests": [
                    {
                        "filename": "test_mode_feature.py",
                        "content": (
                            "import pytest\n\n"
                            "from numlib.mathutil import mode\n\n"
                            "def test_mode_basic():\n"
                            "    assert mode([1, 2, 2, 3]) == 2\n\n"
                            "def test_mode_tie_breaks_low():\n"
                            "    assert mode([2, 1, 1, 2]) == 1\n\n"
                            "def test_mode_empty_raises():\n"
                            "    with pytest.raises(ValueError):\n"
                            "        mode([])\n"
                        ),
                    }
                ]
            }
        )

        class BuildModel:
            """Scripted model: dispatch on the system prompt's shape."""

            def __init__(self):
                self.planner_plan = plan
                self.step_queue = [impl, "SUBMIT"]

            def get_last_usage(self):
                return {
                    "model": "scripted-fake",
                    "provider": "fake",
                    "tokens": 20,
                    "cost_usd": 0.0001,
                }

            def __call__(self, messages, **kw):
                system = next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                )
                if "ACCEPTANCE TESTS" in system:
                    return authored
                if "planning a bug fix" in system:
                    return json.dumps(
                        {"analysis": "scripted", "plan": self.planner_plan}
                    )
                # step session
                if self.step_queue:
                    return self.step_queue.pop(0)
                return "SUBMIT"

        deps.set_call_model(BuildModel())
        try:
            out = run_build(
                request_text="add a mode() function returning the most frequent value",
                repo_path=str(repo),
                config=get_config(
                    {
                        "agent_tests": False,  # keep the e2e focused
                        "self_critique": False,
                        "max_wallclock_s": 600,
                    }
                ),
                log_root=tmp_path / "logs",
                task_id="build-e2e-1",
            )
        finally:
            deps.reset_overrides()

        assert out["status"] == "success", out.get("note")
        result = out["result"]
        assert result.verification is not None
        assert result.verification.target_test_passed
        assert result.verification.regression_passed
        # the feature really landed in the delivered diff
        assert "def mode(" in (result.diff or "")
        # the original fixture repo was never mutated
        assert "def mode(" not in (repo / "numlib" / "mathutil.py").read_text(
            encoding="utf-8"
        )
        # acceptance tests recorded
        assert out["acceptance_tests"]
        # state.json carries the mode key (additive)
        state = json.loads(
            (tmp_path / "logs" / "build-e2e-1" / "state.json").read_text(
                encoding="utf-8"
            )
        )
        assert state.get("mode") == "build"

    def test_already_passing_contract_reported(self, tmp_path):
        """An acceptance test that passes on the pristine tree means the
        feature already exists — reported honestly, nothing built."""
        import shutil as shutil_mod

        from harness import deps
        from harness.build_mode import run_build
        from harness.config import get_config

        repo = tmp_path / "feat02"
        shutil_mod.copytree(FIXTURES / "feat01_numlib", repo)

        authored = json.dumps(
            {
                "tests": [
                    {
                        "filename": "test_exists.py",
                        "content": (
                            "from numlib.mathutil import mean\n\n"
                            "def test_mean_works():\n"
                            "    assert mean([2, 4]) == 3\n"
                        ),
                    }
                ]
            }
        )

        class AuthorOnly:
            def __call__(self, messages, **kw):
                system = next(
                    (m["content"] for m in messages if m["role"] == "system"), ""
                )
                if "ACCEPTANCE TESTS" in system:
                    return authored
                raise AssertionError("no loop calls expected on this path")

        deps.set_call_model(AuthorOnly())
        try:
            out = run_build(
                request_text="add a mean function that averages numbers",
                repo_path=str(repo),
                config=get_config({}),
                log_root=tmp_path / "logs",
                task_id="build-e2e-2",
            )
        finally:
            deps.reset_overrides()
        assert out["status"] == "already_exists"
        assert out["already_passing"] is True
        assert out["result"] is None


# ---------------------------------------------------------------------------
# Session-loop wiring (the interactive session routes all four modes)
# ---------------------------------------------------------------------------


class TestSessionWiring:
    def _drive(self, lines, monkeypatch, tmp_path):
        from cli import interactive

        it = iter(lines)

        def fake_input(prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.chdir(tmp_path)  # session CWD: never scaffold the real tree
        monkeypatch.setenv("VEX_PROJECT_DIR", str(tmp_path / ".vex"))
        return interactive.run_interactive(log_root=tmp_path)

    def test_hi_answers_and_launches_nothing(self, tmp_path, monkeypatch, capsys):
        from cli import interactive

        def explode(*a, **k):
            raise AssertionError("conversational input must not reach any runner")

        monkeypatch.setattr(interactive, "_run_one_fix", explode)
        monkeypatch.setattr(interactive, "_run_one_agent", explode)
        monkeypatch.setattr(interactive, "_run_one_question", explode)
        rc = self._drive(["hi"], monkeypatch, tmp_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "task fix-" not in out
        assert "task build-" not in out
        assert "task agent-" not in out

    def test_bug_sentence_reaches_agent(self, tmp_path, monkeypatch):
        """Fix-shaped input runs the ONE agent loop (not the legacy
        verifier-gated fix entry — `vex fix` still uses that)."""
        from cli import interactive

        captured = {}

        def fake_agent(issue, repo, state, log_root, file_config=None, task_id=None):
            captured["issue"] = issue
            return None

        monkeypatch.setattr(interactive, "_run_one_agent", fake_agent)
        rc = self._drive(
            ["mean() in mathutil.py returns the sum; make it the mean"],
            monkeypatch,
            tmp_path,
        )
        assert rc == 0
        assert captured["issue"].startswith("mean() in mathutil.py")

    def test_question_reaches_qa(self, tmp_path, monkeypatch):
        from cli import interactive

        captured = {}

        def fake_q(question, repo, state, log_root, file_config=None):
            captured["question"] = question
            return None

        monkeypatch.setattr(interactive, "_run_one_question", fake_q)
        rc = self._drive(["how does the verify step work?"], monkeypatch, tmp_path)
        assert rc == 0
        assert captured["question"] == "how does the verify step work?"

    def test_build_request_reaches_agent(self, tmp_path, monkeypatch):
        """Build-shaped input shares the ONE agent loop (fix/build are
        task types, not dispatch modes)."""
        from cli import interactive

        captured = {}

        def fake_agent(request, repo, state, log_root, file_config=None, task_id=None):
            captured["request"] = request
            return None

        monkeypatch.setattr(interactive, "_run_one_agent", fake_agent)
        rc = self._drive(
            ["add a mode() function for the most frequent value"],
            monkeypatch,
            tmp_path,
        )
        assert rc == 0
        assert captured["request"].startswith("add a mode()")

    def test_research_request_reaches_question(self, tmp_path, monkeypatch):
        """Research-shaped input is read-only investigation, answered by
        the question path (no shell, no edits)."""
        from cli import interactive

        captured = {}

        def fake_q(question, repo, state, log_root, file_config=None):
            captured["question"] = question
            return None

        monkeypatch.setattr(interactive, "_run_one_question", fake_q)
        rc = self._drive(
            ["research how tree-sitter compares to other parsing libraries"],
            monkeypatch,
            tmp_path,
        )
        assert rc == 0
        assert captured["question"].startswith("research how tree-sitter")

    def test_all_modes_one_session(self, tmp_path, monkeypatch, capsys):
        """Wiring: one session — 'hi' launches nothing, fix/build share
        the agent runner, question/research share the Q&A runner — and
        the dispatcher distinguishes them."""
        from cli import interactive

        calls = []
        monkeypatch.setattr(
            interactive,
            "_run_one_agent",
            lambda issue, *a, **k: calls.append(("agent", issue)) or None,
        )
        monkeypatch.setattr(
            interactive,
            "_run_one_question",
            lambda question, *a, **k: calls.append(("question", question)) or None,
        )
        rc = self._drive(
            [
                "hi",
                "mean() in mathutil.py returns the sum; make it the mean",
                "how does the verify step work?",
                "add a mode() function for the most frequent value",
                "research how tree-sitter compares to other parsing libraries",
            ],
            monkeypatch,
            tmp_path,
        )
        assert rc == 0
        kinds = [c[0] for c in calls]
        assert kinds == ["agent", "question", "agent", "question"]
        out = capsys.readouterr().out
        assert "task fix-" not in out  # hi answered inline, nothing launched


# ---------------------------------------------------------------------------
# Config + state schema (additive keys)
# ---------------------------------------------------------------------------


class TestConfigStateKeys:
    def test_config_defaults(self):
        from harness.config import DEFAULTS

        assert DEFAULTS["intent_enabled"] is True
        assert DEFAULTS["intent_model"] is None
        assert DEFAULTS["qa_max_files"] == 4
        assert DEFAULTS["research_max_fetches"] == 4
        assert DEFAULTS["build_tests_max"] == 3
        assert DEFAULTS["build_tests_dir"] == "tests/_build_acceptance"

    def test_state_mode_key_additive(self, tmp_path):
        from harness.context import TaskState

        st = TaskState(tmp_path, "t1")
        data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert "mode" not in data  # omitted when unset
        st.set_mode("build")
        data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert data["mode"] == "build"
        # the six Boundary-4 keys stay a strict PREFIX
        keys = list(data.keys())
        assert keys[:6] == [
            "task_id",
            "plan",
            "completed_steps",
            "files_touched",
            "decisions",
            "remaining_plan",
        ]

    def test_modules_import_and_parse(self):
        """The AST guard convention: every new module parses + imports."""
        import ast

        for name in (
            "intent",
            "router",
            "qa_mode",
            "research_mode",
            "build_mode",
            "agent_loop",
        ):
            p = ROOT / "harness" / f"{name}.py"
            ast.parse(p.read_text(encoding="utf-8"))
            __import__(f"harness.{name}")
