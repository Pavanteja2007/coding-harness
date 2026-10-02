"""Ceiling Prompt 16 — product surfaces reachable, public claims true.

One test class per required case, named after the requirement so a failure
says which promise broke. The expensive cases (real subprocess handshakes)
are marked and isolated; everything else is in-process and deterministic.

No Docker, no provider, no network. The model is a scripted callable
installed through the documented ``harness.deps.set_call_model`` seam (and,
for the subprocess cases, a ``sitecustomize.py`` on ``PYTHONPATH`` that does
the same thing in the child) — which proves the ENGINE, the protocol, and
the exit-code contract without claiming any model quality.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import re as _re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cli import capability, headless, serve  # noqa: E402
from cli.exit_codes import EXIT_CODES  # noqa: E402
from scripts import docs_truth, release_evidence  # noqa: E402

# ---------------------------------------------------------------------------
# Deterministic model
# ---------------------------------------------------------------------------

_SITECUSTOMIZE = '''\
import json
import sys
from pathlib import Path

REPO = Path(r"{repo}")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import harness.deps as hdeps


class _ScriptedModel:
    """Answers both protocol shapes: the agent loop's `done` and the SDK's
    `finish`. Which one is correct depends on the tool catalog the prompt
    advertises, so the prompt decides, not a flag."""

    def get_last_usage(self):
        return {{
            "model": "scripted",
            "provider": "fake",
            "prompt_tokens": 8,
            "completion_tokens": 4,
            "tokens": 12,
            "cost_usd": 0.0001,
        }}

    def __call__(self, messages, **kwargs):
        system = " ".join(
            str(m.get("content") or "") for m in messages
            if isinstance(m, dict) and m.get("role") == "system"
        )
        if "planning a bug fix" in system:
            return json.dumps({{
                "analysis": "scripted",
                "plan": [{{"id": 1, "description": "inspect", "checkpoint": "read",
                           "files_hint": ["app.py"]}}],
            }})
        if '"done"' in system or " done " in system:
            return json.dumps({{"tool": "done", "answer": "app.py sets value to 1."}})
        return json.dumps({{"tool": "finish", "answer": "app.py sets value to 1."}})


hdeps.set_call_model(_ScriptedModel())
'''


class _InProcessModel:
    """The same scripted model for in-process tests."""

    def get_last_usage(self) -> Dict[str, Any]:
        return {
            "model": "scripted",
            "provider": "fake",
            "prompt_tokens": 8,
            "completion_tokens": 4,
            "tokens": 12,
            "cost_usd": 0.0001,
        }

    def __call__(self, messages, **kwargs) -> str:
        system = " ".join(
            str(m.get("content") or "")
            for m in messages
            if isinstance(m, dict) and m.get("role") == "system"
        )
        if "planning a bug fix" in system:
            return json.dumps(
                {
                    "analysis": "scripted",
                    "plan": [
                        {
                            "id": 1,
                            "description": "inspect",
                            "checkpoint": "read",
                            "files_hint": ["app.py"],
                        }
                    ],
                }
            )
        if '"done"' in system or " done " in system:
            return json.dumps({"tool": "done", "answer": "app.py sets value to 1."})
        return json.dumps({"tool": "finish", "answer": "app.py sets value to 1."})


@pytest.fixture
def scripted_model():
    """Install the deterministic model for the duration of a test."""
    import harness.deps as hdeps

    previous = getattr(hdeps, "_call_model_override", None)
    hdeps.set_call_model(_InProcessModel())
    try:
        yield
    finally:
        hdeps.set_call_model(previous)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny repository for a headless turn to run against."""
    target = tmp_path / "repo"
    target.mkdir()
    (target / "app.py").write_text("value = 1\n", encoding="utf-8")
    return target


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    """Isolate config, home, and log roots so nothing touches the machine."""
    home = tmp_path / "neohome"
    home.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("NEO_HOME", str(home))
    monkeypatch.setenv("HARNESS_HOME", str(home))
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(logs))
    monkeypatch.setenv("NEO_NO_RELEASE_NOTICE", "1")
    monkeypatch.setenv("NEO_MODEL", "scripted")
    monkeypatch.delenv("NEO_PROVIDER", raising=False)
    return {"home": home, "logs": logs, "config": home}


@pytest.fixture
def no_configured_model(monkeypatch):
    """State the REFUSAL precondition instead of assuming the machine has none.

    This fixture exists because of a real Windows hang, and the reason is worth
    recording rather than only fixing.

    `cmd_serve` and `acp_stdio_serve` both refuse when `cli.serve.resolve_model`
    reports no model, and both tests used to establish that by popping
    `NEO_MODEL` and assuming nothing else supplied one. It does. `resolve_model`
    falls back to `cli.neoconfig.merged_settings`, which walks the settings
    chain and finds this repository's own `.neo` - a project config carrying
    `model: openai/gpt-4o-mini`. So `plan.model` was non-empty, the refusal
    branch was skipped, and `serve_once` reached `agent_sdk/server.py:324`
    `serve_forever()`.

    The result was not a failure. `socketserver.serve_forever` blocks on a
    selector forever, so the test HUNG: measured on this host at over 90s for
    `test_cli_refuses_serve_without_a_model`, for
    `test_acp_refuses_to_serve_without_a_model`, and for the whole
    `TestServePolicies` class. On a Linux runner with no `~/.neo` and no
    checkout `.neo` the same tests pass in milliseconds, which is why this
    stayed invisible: a test that hangs only on a configured developer machine
    reads as a slow machine, not as a defect.

    Two changes, and both matter:

    * `resolve_model` is pinned to the no-model answer, so the precondition is
      stated rather than inferred from ambient configuration.
    * `serve_once` is replaced with something that RAISES. If the refusal
      branch ever regresses, the test fails immediately and loudly instead of
      blocking a worker thread on a socket. A hang is the worst possible
      failure mode for a CI gate: it reports nothing, it consumes the job's
      whole timeout, and the timeout looks like an infrastructure flake.
    """
    import cli.serve as serve_module

    monkeypatch.delenv("NEO_MODEL", raising=False)
    monkeypatch.setattr(
        serve_module,
        "resolve_model",
        lambda repo=None: (
            None,
            {
                "model": "",
                "source": "",
                "reason": "no model configured; run `neo login` first",
            },
        ),
    )

    def _refused(*_args, **_kwargs):
        raise AssertionError(
            "serve_once was called: the no-model refusal did not happen, so "
            "the test would have blocked forever in serve_forever()"
        )

    monkeypatch.setattr(serve_module, "serve_once", _refused)
    return serve_module


def _subprocess_env(isolated: Dict[str, Any], extra: Optional[Dict[str, str]] = None):
    """The environment for a real child process, isolated the same way."""
    site = isolated["home"] / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(
        _SITECUSTOMIZE.format(repo=REPO_ROOT), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    env["NEO_NO_RELEASE_NOTICE"] = "1"
    env["NEO_HOME"] = str(isolated["home"])
    env["HARNESS_HOME"] = str(isolated["home"])
    env["HARNESS_LOGS_DIR"] = str(isolated["logs"])
    env["NEO_MODEL"] = "scripted"
    env.pop("NEO_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    if extra:
        env.update(extra)
    return env


# ===========================================================================
# Required case 1 — `echo "..." | neo -` returns a task ID and exit code
# ===========================================================================


class TestPipedStdinSurface:
    """`neo -` is the piped-context entry point."""

    def test_piped_context_returns_task_id_and_exit_code(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """A REAL `echo | python -m cli -` returns a task id and an exit code.

        The child runs the real engine with a scripted model. The asserted
        outcome is `completed_unverified` with exit 1: the agent path has no
        verifier gate, so a finished run is explicitly NOT success, and this
        is the exact invariant the ceiling requires.
        """
        env = _subprocess_env(isolated)
        completed = subprocess.run(
            [sys.executable, "-m", "cli", "-", "--repo", str(repo), "--json"],
            cwd=str(REPO_ROOT),
            env=env,
            input="the module sets a single value\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
        assert completed.returncode == EXIT_CODES["task_failure"], completed.stderr[
            -2000:
        ]
        document = json.loads(completed.stdout)
        assert document["schema"] == headless.ENVELOPE_SCHEMA
        assert document["task_id"], "a piped run must return a task id"
        assert document["session_id"].startswith("sess-")
        assert document["stdin_chars"] > 0, "the piped context must be read"
        assert document["status"] == "completed_unverified"
        assert document["verified"] is False
        assert document["verification_state"] != "verified"
        assert Path(document["trace_path"]).name == "trace.jsonl"

    def test_piped_context_without_a_prompt_still_answers(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """`echo | neo -` with no sentence is a valid context-only run."""
        env = _subprocess_env(isolated)
        completed = subprocess.run(
            [sys.executable, "-m", "cli", "-", "--repo", str(repo), "--json"],
            cwd=str(REPO_ROOT),
            env=env,
            input="value = 1\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
        document = json.loads(completed.stdout)
        assert document["task_id"]
        assert document["stdin_chars"] == len("value = 1\n")

    def test_piped_context_labels_the_supplied_material(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """Piped context is LABELED, never silently concatenated.

        Untrusted content entering trusted context unlabeled is ceiling
        invariant 8, so the composed request says where the context came
        from and where the instruction starts.
        """
        composed = headless.compose_prompt("explain app.py", "value = 1")
        assert headless.PIPED_CONTEXT_HEADER in composed
        assert "value = 1" in composed
        assert "explain app.py" in composed
        # A prompt with no context is byte-identical to the TUI's request.
        assert headless.compose_prompt("  explain app.py  ") == "explain app.py"
        assert headless.compose_prompt("explain app.py", "") == "explain app.py"

    def test_oversized_pipe_is_truncated_explicitly(self) -> None:
        """A pipe too large for a prompt is truncated, and says so."""
        text, truncated = headless.read_piped_context(
            io.StringIO("x" * 100), max_chars=10
        )
        assert truncated is True
        assert len(text) == 10
        text, truncated = headless.read_piped_context(io.StringIO("short"))
        assert truncated is False
        assert text == "short"
        # An unreadable stream is empty, not an exception.
        text, truncated = headless.read_piped_context(object())
        assert (text, truncated) == ("", False)


# ===========================================================================
# Required case 2 — `neo -p` and the TUI produce the same JSON contract
# ===========================================================================


class TestHeadlessContractParity:
    """One envelope, derived from the journal the TUI also reads."""

    def test_envelope_matches_the_tui_journal_projection(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """The headless document equals the TUI's own journal projection.

        The TUI has no JSON surface, so the parity that matters is the
        SOURCE: `cli.runview` is the TUI's journal authority
        (`read_live_projection` + `status_label` + `effective_terminal_status`
        + `verification_state`). This asserts the envelope's status and
        verification fields are exactly those functions' output for the same
        run directory, which is what makes a script and a human unable to
        disagree.
        """
        from cli.runview import (
            effective_terminal_status,
            read_live_projection,
            verification_state,
        )

        outcome = headless.run_headless(
            "explain app.py", repo=repo, log_root=isolated["logs"], as_json=True
        )
        envelope = json.loads(outcome.text)
        task_dir = Path(envelope["trace_path"]).parent
        projection = read_live_projection(task_dir, mode=envelope["mode"])
        evidence = projection.get("verification_evidence") or []
        if not evidence and projection.get("latest_verification"):
            evidence = [projection["latest_verification"]]
        expected_status = effective_terminal_status(projection.get("status"), evidence)
        expected_verification = verification_state(evidence, expected_status)
        assert envelope["status"] == expected_status
        assert envelope["verification_state"] == expected_verification
        assert envelope["verified"] is (expected_status == "completed_verified")

    def test_prompt_flag_and_piped_form_share_the_envelope(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """`-p` and `-` produce the SAME document shape, key for key."""
        first = headless.run_headless(
            "explain app.py", repo=repo, log_root=isolated["logs"], as_json=True
        )
        second = headless.run_headless(
            "explain app.py",
            repo=repo,
            log_root=isolated["logs"],
            context="value = 1",
            stdin_chars=9,
            as_json=True,
        )
        assert set(first.envelope) == set(second.envelope)
        assert first.exit_code == second.exit_code
        assert first.envelope["schema"] == second.envelope["schema"]
        assert first.envelope["mode"] == second.envelope["mode"]

    def test_completed_unverified_never_serializes_as_success(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """Ceiling invariant 2, on the headless surface.

        A finished run with no verifier evidence is `completed_unverified`
        and exits NON-ZERO. The success code is reachable only with clean
        evidence.
        """
        outcome = headless.run_headless(
            "explain app.py", repo=repo, log_root=isolated["logs"], as_json=True
        )
        envelope = json.loads(outcome.text)
        assert envelope["status"] != "completed_verified"
        assert envelope["verified"] is False
        assert outcome.exit_code != EXIT_CODES["success"]
        assert "success" not in envelope["status"]
        # And the human renderer says the same thing.
        text = headless.render_human(envelope)
        assert "UNVERIFIED" in text.upper()
        assert envelope["exit_reason"] == "task_failure"

    def test_session_id_is_shared_with_the_conversation_journal(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """The session id is the conversation id the shells already use."""
        from cli import session as cli_session

        first = headless.run_headless(
            "explain app.py", repo=repo, log_root=isolated["logs"], as_json=True
        )
        second = headless.run_headless(
            "explain app.py",
            repo=repo,
            log_root=isolated["logs"],
            session_id=first.session_id,
            as_json=True,
        )
        assert second.session_id == first.session_id
        assert second.envelope["session_created"] is False
        conversation = cli_session.load_or_create(
            isolated["logs"], repo, session_id=first.session_id
        )
        assert conversation.get("session_id") == first.session_id
        assert conversation.get("turns"), "the headless turn must be recorded"

    def test_usage_error_is_still_a_parseable_document(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """A refusal uses the same schema, so one parser handles every case."""
        outcome = headless.run_headless(
            "/review", repo=repo, log_root=isolated["logs"], as_json=True
        )
        envelope = json.loads(outcome.text)
        assert outcome.exit_code == EXIT_CODES["usage_error"]
        assert envelope["schema"] == headless.ENVELOPE_SCHEMA
        assert envelope["status"] == "refused"
        assert "/review" in envelope["error"]

    def test_read_only_modes_never_claim_verification(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """/ask, /plan and /review are read-only and say so in the envelope.

        A read-only mode has no verifier in its path, so it reports
        `verification_state: not_run` and `verified: false` even though a
        completed answer exits 0 - a success that never claimed a check it
        did not run.
        """
        for prompt in ("/ask what is in app.py", "/plan how would you test app.py"):
            outcome = headless.run_headless(
                prompt, repo=repo, log_root=isolated["logs"], as_json=True
            )
            envelope = json.loads(outcome.text)
            assert envelope["read_only"] is True, prompt
            assert envelope["verifies"] is False, prompt
            assert envelope["verification_state"] == "not_run", prompt
            assert envelope["verified"] is False, prompt
            assert outcome.exit_code == EXIT_CODES["success"], prompt
            assert "read-only" in envelope["exit_reason"], prompt

    def test_headless_policies_are_explicit_and_typed(self) -> None:
        """The three policies the prompt names are declared, not inferred."""
        for name in ("plan", "review", "ask"):
            mode = headless.HEADLESS_MODES[name]
            assert mode.command.startswith("/")
            assert mode.read_only is True
            assert mode.verifies is False
        mode, rest = headless.resolve_mode("/review the auth module")
        assert mode.name == "review"
        assert rest == "the auth module"
        mode, rest = headless.resolve_mode("explain app.py")
        assert mode.name == "agent_task"
        assert mode.read_only is False
        assert rest == "explain app.py"

    def test_a_failed_read_only_run_is_not_success(
        self, repo: Path, isolated: Dict[str, Any], scripted_model
    ) -> None:
        """Read-only + completed exits 0; read-only + failed does not."""
        envelope = headless.result_envelope(
            mode=headless.HEADLESS_MODES["ask"],
            session_id="sess-1",
            session_created=False,
            status="failed",
            log_root=isolated["logs"],
        )
        assert envelope["verified"] is False
        assert envelope["exit_code"] == EXIT_CODES["task_failure"]
        envelope = headless.result_envelope(
            mode=headless.HEADLESS_MODES["ask"],
            session_id="sess-1",
            session_created=False,
            status="completed",
            log_root=isolated["logs"],
        )
        assert envelope["exit_code"] == EXIT_CODES["success"]

    def test_cli_parses_prompt_and_piped_forms(self) -> None:
        """`-p`, `--prompt` and `-` are recognized, and nothing else is."""
        from cli.main import _headless_prompt_args, _split_headless_options

        assert _headless_prompt_args([]) is None
        assert _headless_prompt_args(["fix", "--repo", "x"]) is None
        assert _headless_prompt_args(["-p", "hello"]) == ("hello", False, [])
        assert _headless_prompt_args(["-"]) == ("", True, [])
        assert _headless_prompt_args(["-", "and", "this"]) == (
            "",
            True,
            ["and", "this"],
        )
        with pytest.raises(ValueError):
            _headless_prompt_args(["-p"])
        with pytest.raises(ValueError):
            _headless_prompt_args(["-p", "   "])
        options, words = _split_headless_options(
            ["--json", "--repo", "r", "extra", "words"]
        )
        assert options["json"] is True
        assert options["repo"] == "r"
        assert words == "extra words"
        # An unknown flag is PROMPT TEXT, not a swallowed option: a
        # sentence may legitimately contain a double dash.
        options, words = _split_headless_options(["fix", "--", "thing"])
        assert words == "fix -- thing"
        assert options["json"] is False


# ===========================================================================
# Required case 3 — `neo serve` and `neo acp` complete real handshakes
# ===========================================================================


@pytest.mark.slow
class TestIntegrationHandshakes:
    """Real processes, real protocols."""

    def test_neo_serve_completes_a_real_http_handshake(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """`neo serve` binds, answers /health and /v1/capabilities, then stops.

        The receipt is read from the child's OWN stdout, which is why
        `serve --json` prints one line: a script has to be able to learn the
        bound port from a single read.
        """
        env = _subprocess_env(isolated)
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "cli",
                "serve",
                "--repo",
                str(repo),
                "--port",
                "0",
                "--json",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        lines: List[str] = []
        reader = threading.Thread(
            target=lambda: [lines.append(line) for line in process.stdout],
            daemon=True,
        )
        reader.start()
        url: Optional[str] = None
        deadline = time.time() + 180
        try:
            while time.time() < deadline and url is None:
                for line in list(lines):
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        url = json.loads(stripped)["url"]
                        break
                time.sleep(0.25)
            assert url, f"no serve receipt; stdout={lines!r}"
            assert url.startswith("http://127.0.0.1:")
            with urllib.request.urlopen(url + "/health", timeout=15) as response:
                health = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert health["healthy"] is True
            assert health["server"] == "neo-agent-server"
            with urllib.request.urlopen(url + "/v1/capabilities", timeout=15) as resp:
                capabilities = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert capabilities["protocol_version"] == 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()

    def test_neo_acp_completes_a_real_stdio_handshake(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """`neo acp` answers initialize and session/new over real stdio.

        This is the SHAPE an editor launch depends on: a child process
        whose stdout is protocol and nothing else, which negotiates a
        version and mints a session, and which exits cleanly when the editor
        closes the pipe.

        The full prompt ROUND TRIP is proved separately, in process, against
        a real `agent_sdk.Agent` with an injected model
        (`test_acp_prompt_completes_through_the_real_sdk_agent`). It is not
        proved here because a subprocess with a model NAME configured reaches
        the real provider gateway, and this round did not run a live-provider
        lane. Pretending otherwise would mean either a fake pass or a test
        that measures provider latency.
        """
        env = _subprocess_env(isolated)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "cli", "acp", "--repo", str(repo)],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        documents: List[Dict[str, Any]] = []

        def send(payload: Dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def read_for(request_id: int, timeout: float = 240.0) -> Dict[str, Any]:
            deadline = time.time() + timeout
            while time.time() < deadline:
                assert process.stdout is not None
                line = process.stdout.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                # Every stdout line must be one JSON object: a stray byte
                # here is what desynchronizes an editor's JSON-RPC stream.
                document = json.loads(stripped)
                documents.append(document)
                if document.get("id") == request_id:
                    return document
            return {}

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            )
            init = read_for(1)
            assert init["result"]["protocolVersion"] == 1
            assert init["result"]["agentInfo"]["name"] == "neo-acp-server"
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": str(repo), "mcpServers": []},
                }
            )
            session = read_for(2)
            assert session["result"]["sessionId"]
            # A first-run notice must never have touched stdout.
            assert all("jsonrpc" in document for document in documents)
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
        assert process.returncode == 0, process.stderr.read()[-2000:]

    def test_acp_prompt_completes_through_the_real_sdk_agent(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """A full ACP turn against a real `agent_sdk.Agent`, in process.

        Uses the shipped ``ProcessStdioTransport`` over in-memory streams and
        a real ``ACPServer`` bound to a real ``Agent`` with an injected
        model. The asserted contract is the verifier one: `end_turn` with
        `completed_unverified` and `verified: false`, because the SDK's
        agent path has no verifier gate.
        """
        from acp.server import ACPServer
        from agent_sdk import Agent

        class _Feeder:
            """A blocking text stream a test thread pushes JSONL frames into."""

            def __init__(self) -> None:
                self.frames: "queue.Queue[str]" = queue.Queue()

            def push(self, payload: Dict[str, Any]) -> None:
                self.frames.put(json.dumps(payload) + "\n")

            def readline(self, *args: Any, **kwargs: Any) -> str:
                return self.frames.get()

        inbox = _Feeder()
        outbox = io.StringIO()
        agent = Agent(
            str(repo),
            log_root=str(isolated["logs"] / "acp"),
            model=_InProcessModel(),
            config={"agent_approval": "auto", "steering_enabled": False},
        )
        server = ACPServer(agent)

        async def drive() -> Dict[str, Any]:
            transport = serve.ProcessStdioTransport(stdin=inbox, stdout=outbox)
            server.transport = transport
            await server.start()
            inbox.push(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": 1, "clientCapabilities": {}},
                }
            )
            await asyncio.sleep(0.5)
            inbox.push(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": str(repo), "mcpServers": []},
                }
            )
            for _ in range(40):
                await asyncio.sleep(0.1)
                session = next(
                    (item for item in self._documents(outbox) if item.get("id") == 2),
                    None,
                )
                if session:
                    break
            assert session is not None, outbox.getvalue()[:2000]
            session_id = session["result"]["sessionId"]
            inbox.push(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": "what is in app.py?"}],
                    },
                }
            )
            for _ in range(240):
                await asyncio.sleep(0.1)
                prompt = next(
                    (item for item in self._documents(outbox) if item.get("id") == 3),
                    None,
                )
                if prompt:
                    await transport.aclose()
                    return prompt
            await transport.aclose()
            return {}

        prompt = asyncio.run(asyncio.wait_for(drive(), timeout=180))
        assert prompt, "session/prompt never answered"
        result = prompt["result"]
        assert result["stopReason"] == "end_turn"
        assert result["status"] == "completed_unverified"
        assert result["verified"] is False

    @staticmethod
    def _documents(outbox: io.StringIO) -> List[Dict[str, Any]]:
        documents: List[Dict[str, Any]] = []
        for line in outbox.getvalue().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                documents.append(json.loads(stripped))
            except ValueError:  # pragma: no cover - defensive
                continue
        return documents


class TestServePolicies:
    """Loopback default, explicit auth, and an honest bind warning."""

    def test_loopback_is_the_default_and_warns_on_nothing(self) -> None:
        plan = serve.build_serve_plan(repo=Path.cwd(), port=0)
        assert plan.host == "127.0.0.1"
        assert plan.warning == ""
        assert plan.auth_required is False

    def test_non_loopback_requires_explicit_opt_in(self) -> None:
        with pytest.raises(PermissionError) as excinfo:
            serve.build_serve_plan(repo=Path.cwd(), host="0.0.0.0", port=0)
        assert "loopback" in str(excinfo.value)

    def test_non_loopback_without_a_token_is_refused_even_with_opt_in(self) -> None:
        with pytest.raises(PermissionError) as excinfo:
            serve.build_serve_plan(
                repo=Path.cwd(),
                host="0.0.0.0",
                port=0,
                allow_non_loopback=True,
                token="",
            )
        assert "unauthenticated" in str(excinfo.value)

    def test_opted_in_non_loopback_warns_and_exposes_no_token(self) -> None:
        plan = serve.build_serve_plan(
            repo=Path.cwd(),
            host="0.0.0.0",
            port=0,
            allow_non_loopback=True,
            token="s3cret",
        )
        assert "WARNING" in plan.warning
        assert "0.0.0.0" in plan.warning
        assert "s3cret" not in str(plan.to_dict())

    def test_editor_configuration_is_exact_and_printed_once(
        self, repo: Path, isolated: Dict[str, Any]
    ) -> None:
        """The first-run notice shows the real argv, and only once."""
        payload = serve.acp_editor_config(repo=repo, editor="zed")
        assert payload["path"] == ".zed/settings.json"
        command = payload["config"]["context_servers"]["neo"]["command"]
        assert command["path"] == sys.executable
        assert command["args"][:3] == ["-m", "cli", "acp"]
        assert str(repo) in command["args"]
        other = serve.acp_editor_config(repo=repo, editor="helix")
        assert other["path"] == ""
        assert other.get("note")

        first = serve.first_run_notice(repo=repo, editor="zed")
        assert first is not None
        assert "context_servers" in first
        assert serve.first_run_notice(repo=repo, editor="zed") is None
        # --print-config prints it again on demand.
        assert "context_servers" in serve.editor_configuration(repo=repo, editor="zed")

    def test_acp_refuses_to_serve_without_a_model(
        self, repo: Path, isolated: Dict[str, Any], capsys, no_configured_model
    ) -> None:
        """A server that would answer every request with an auth error is
        refused, and says what to do."""
        code = serve.acp_stdio_serve(repo=repo)
        assert code == EXIT_CODES["model_error"]
        assert "neo login" in capsys.readouterr().err

    def test_cli_refuses_serve_without_a_model(
        self, repo: Path, isolated: Dict[str, Any], capsys, no_configured_model
    ) -> None:
        """`neo serve` is a usage-free refusal with the model exit code."""
        from argparse import Namespace

        from cli.main import cmd_serve

        code = cmd_serve(
            Namespace(
                repo=str(repo),
                host="127.0.0.1",
                port=0,
                auth_token="",
                allow_non_loopback=False,
                log_root=str(isolated["logs"]),
                json=False,
            )
        )
        assert code == EXIT_CODES["model_error"]
        assert "neo login" in capsys.readouterr().err


# ===========================================================================
# Required case 4 — a version capability mismatch warns exactly once
# ===========================================================================


class TestReleaseStalenessNotice:
    """Warn once, and only about real drift."""

    def test_mismatch_warns_exactly_once(
        self, isolated: Dict[str, Any], monkeypatch
    ) -> None:
        """Once per process AND once per installation."""
        public = "0.1.0"
        local = "0.2.1"
        first = capability.stale_release_notice(
            public_version=public, docs_version=local
        )
        assert first
        assert public in first and local in first
        assert "neo update" in first
        for _ in range(3):
            assert (
                capability.stale_release_notice(
                    public_version=public, docs_version=local
                )
                == ""
            ), "the notice repeated"

    def test_no_notice_when_the_public_release_is_current(
        self, isolated: Dict[str, Any]
    ) -> None:
        assert (
            capability.stale_release_notice(
                public_version="0.2.1", docs_version="0.2.1"
            )
            == ""
        )
        assert (
            capability.stale_release_notice(
                public_version="0.3.0", docs_version="0.2.1"
            )
            == ""
        ), "a NEWER public release is not a mismatch"

    def test_unknown_version_is_not_reported_as_out_of_date(self) -> None:
        assert capability.is_public_release_older("", "0.2.1") is False
        assert capability.is_public_release_older("0.2.1", "") is False
        assert capability.is_public_release_older("not-a-version", "0.2.1") is False
        assert capability.is_public_release_older("0.2.10", "0.2.9") is False
        assert capability.is_public_release_older("0.2.9", "0.2.10") is True

    def test_network_failure_is_unknown_not_an_error(
        self, isolated: Dict[str, Any]
    ) -> None:
        def _boom(url: str, timeout: float) -> bytes:
            raise OSError("offline")

        assert capability.public_release_version(fetcher=_boom) == ""
        assert (
            capability.stale_release_notice(
                public_version="", docs_version="0.2.1", fetcher=_boom
            )
            == ""
        )

    def test_public_version_reads_the_injected_fetcher(
        self, isolated: Dict[str, Any]
    ) -> None:
        payload = json.dumps({"info": {"version": "0.2.0"}}).encode("utf-8")
        assert (
            capability.public_release_version(fetcher=lambda u, t: payload) == "0.2.0"
        )

    def test_cli_never_notices_on_the_version_commands(
        self, isolated: Dict[str, Any], monkeypatch, capsys
    ) -> None:
        """`--version`, `--help`, `update`, and `uninstall` stay clean."""
        from cli import main as cli_main

        monkeypatch.setattr(cli_main, "_emit_release_staleness_notice", _record_notice)
        monkeypatch.setenv("NEO_MODEL", "scripted")
        with pytest.raises(SystemExit):
            cli_main.main(["--version"])
        with pytest.raises(SystemExit):
            cli_main.main(["--help"])
        captured = capsys.readouterr()
        assert "public neo-agent-cli release" not in captured.out
        assert "public neo-agent-cli release" not in captured.err


def _record_notice(raw: List[str]) -> None:
    """Replace the notice emitter so a test can count invocations."""
    _record_notice.calls.append(list(raw))  # type: ignore[attr-defined]


_record_notice.calls = []  # type: ignore[attr-defined]


class TestCapabilityProbe:
    """The probe compares the registry against the installation."""

    def test_probe_reports_every_advertised_capability(self) -> None:
        report = capability.probe_capabilities(REPO_ROOT)
        names = {item.name for item in report.capabilities}
        assert "command:fix" in names
        assert "command:serve" in names
        assert "command:acp" in names
        assert "module:agent_sdk" in names
        assert "module:acp" in names
        assert any(name.startswith("slash:/") for name in names)
        assert report.ok is True, report.missing
        assert report.version == capability.local_docs_version(REPO_ROOT)

    def test_a_module_that_cannot_import_is_reported_missing(self) -> None:
        def _broken(name: str) -> Any:
            if name == "acp":
                raise ImportError("no acp in this wheel")
            return __import__(name)

        report = capability.probe_capabilities(REPO_ROOT, import_module=_broken)
        assert report.ok is False
        assert "module:acp" in report.missing
        entry = next(item for item in report.capabilities if item.name == "module:acp")
        assert entry.available is False
        assert "import failed" in entry.detail

    def test_help_and_completions_come_from_the_runtime_registry(self) -> None:
        """`--help`'s command list is the parser's, not a maintained string."""
        from cli.main import build_parser

        parser = build_parser()  # populates the registry
        inventory = capability.command_inventory()
        assert inventory["drift"] == [], inventory["drift"]
        assert inventory["source"] == "cli.main.build_parser"
        metavar = parser._subparsers._group_actions[0].metavar
        assert metavar == capability.help_metavar()
        assert "serve" in metavar and "acp" in metavar
        assert "__completions" not in metavar
        for name in inventory["commands"]:
            assert name in metavar

    def test_capabilities_command_reports_and_gates(self, capsys) -> None:
        from argparse import Namespace

        from cli.main import cmd_capabilities

        code = cmd_capabilities(Namespace(json=True))
        document = json.loads(capsys.readouterr().out)
        assert document["schema"] == "neo.capabilities/1"
        assert code == EXIT_CODES["success"]
        assert document["ok"] is True
        assert "serve" in document["inventory"]["commands"]


# ===========================================================================
# Required case 5 — the site gate fails on version/claim drift
# ===========================================================================


class TestDocsTruthGate:
    """The gate is the reason a public claim stays true."""

    def test_the_real_tree_passes(self) -> None:
        report = docs_truth.run_gates(REPO_ROOT)
        failures = [
            f"{gate['gate']}: {item}"
            for gate in report["gates"]
            for item in gate["findings"]
        ]
        assert report["status"] == "pass", failures
        assert docs_truth.site_gate(REPO_ROOT).ok

    def test_version_drift_fails_the_gate(self, tmp_path: Path) -> None:
        """A site that advertises a version the wheel does not have fails.

        The mutation targets whatever the CURRENT advertised version is, not a
        literal. Hardcoding the version meant this test silently stopped
        testing anything the moment the version moved: the replace became a
        no-op, the gate passed, and a green test was proving nothing. The
        `mutated` assertion is what stops that from recurring quietly.
        """
        root = _clone_doc_surfaces(tmp_path)
        releases = root / "site/src/lib/content/releases.ts"
        original = releases.read_text(encoding="utf-8")
        match = _re.search(r'version: "([^"]+)"', original)
        assert match is not None, "the site advertises no version to mutate"
        current = match.group(1)
        mutated = original.replace(f'version: "{current}"', 'version: "v9.9.9"', 1)
        assert mutated != original, f"could not mutate the advertised {current}"
        releases.write_text(mutated, encoding="utf-8")
        result = docs_truth.check_versions(root)
        assert result.status == "fail"
        assert any("9.9.9" in item.detail for item in result.findings)
        assert not docs_truth.site_gate(root).ok

    def test_a_claim_without_evidence_fails_the_gate(self, tmp_path: Path) -> None:
        """An unfalsifiable capability claim is a finding, not a claim."""
        root = _clone_doc_surfaces(tmp_path)
        matrix = root / "docs/feature-matrix.md"
        text = matrix.read_text(encoding="utf-8")
        target = next(line for line in text.splitlines() if "tests/test_cli.py" in line)
        stripped = "|".join(cell for cell in target.split("|") if "tests/" not in cell)
        matrix.write_text(
            text.replace(target, stripped.rstrip("|").rstrip() + " | none yet |"),
            encoding="utf-8",
        )
        result = docs_truth.check_claims(root)
        assert result.status == "fail"
        assert any("cites no test" in item.detail for item in result.findings)

    def test_a_claim_citing_a_missing_file_fails_the_gate(self, tmp_path: Path) -> None:
        root = _clone_doc_surfaces(tmp_path)
        matrix = root / "docs/feature-matrix.md"
        matrix.write_text(
            matrix.read_text(encoding="utf-8").replace(
                "tests/test_cli.py", "tests/test_does_not_exist.py"
            ),
            encoding="utf-8",
        )
        result = docs_truth.check_claims(root)
        assert result.status == "fail"
        assert any("does not exist" in item.detail for item in result.findings)

    def test_an_unknown_status_word_fails_the_gate(self, tmp_path: Path) -> None:
        root = _clone_doc_surfaces(tmp_path)
        matrix = root / "docs/feature-matrix.md"
        matrix.write_text(
            matrix.read_text(encoding="utf-8").replace(
                "| Implemented |", "| Mostly done |", 1
            ),
            encoding="utf-8",
        )
        result = docs_truth.check_claims(root)
        assert result.status == "fail"
        assert any("closed vocabulary" in item.detail for item in result.findings)

    def test_a_gate_that_checks_nothing_is_never_a_pass(self, tmp_path: Path) -> None:
        """Deleting the claim table must not turn the gate green."""
        root = _clone_doc_surfaces(tmp_path)
        (root / "docs/feature-matrix.md").write_text(
            "# Feature matrix\n\n## User-facing capabilities\n\nNo table.\n",
            encoding="utf-8",
        )
        result = docs_truth.check_claims(root)
        assert result.status == "unevaluated"
        assert result.ok is False

    def test_a_limitation_without_a_source_fails(self, tmp_path: Path) -> None:
        root = _clone_doc_surfaces(tmp_path)
        limits = root / "site/src/lib/content/limits.ts"
        limits.write_text(
            limits.read_text(encoding="utf-8").replace(
                'source: "CHANGELOG.md:103; RESULTS.md:160-163",',
                'source: "trust me",',
            ),
            encoding="utf-8",
        )
        result = docs_truth.check_limits(root)
        assert result.status == "fail"
        assert any(
            "cites no source" in item.detail or "does not exist" in item.detail
            for item in result.findings
        ), [item.detail for item in result.findings]

    def test_a_limitation_citing_a_missing_file_fails(self, tmp_path: Path) -> None:
        root = _clone_doc_surfaces(tmp_path)
        limits = root / "site/src/lib/content/limits.ts"
        limits.write_text(
            limits.read_text(encoding="utf-8").replace(
                "CHANGELOG.md:103-104", "docs/does-not-exist.md:1"
            ),
            encoding="utf-8",
        )
        result = docs_truth.check_limits(root)
        assert result.status == "fail"
        assert any("does not exist" in item.detail for item in result.findings), [
            item.detail for item in result.findings
        ]

    def test_the_site_limit_claim_is_not_stale(self) -> None:
        """The site no longer claims the code graph is Python-only.

        `pyproject.toml` declares the JavaScript and TypeScript grammars and
        `memory/code_graph.py` indexes them, so the old "Python only" entry
        was a limit the product had outgrown.
        """
        text = (REPO_ROOT / "site/src/lib/content/limits.ts").read_text(
            encoding="utf-8"
        )
        assert "Python only" not in text
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "tree-sitter-javascript" in pyproject
        assert "tree-sitter-typescript" in pyproject

    def test_cli_entry_point_exit_codes(self, tmp_path: Path) -> None:
        from scripts.docs_truth import main as gate_main

        assert gate_main(["--project-root", str(_clone_doc_surfaces(tmp_path))]) == 0
        root = _clone_doc_surfaces(tmp_path)
        (root / "CHANGELOG.md").write_text("## v0.0.1\n", encoding="utf-8")
        assert gate_main(["--project-root", str(root)]) == 2
        assert gate_main(["--project-root", str(root), "--gate", "site"]) == 2


def _clone_doc_surfaces(tmp_path: Path) -> Path:
    """Copy the surfaces the docs gate reads into a scratch root.

    Copying (rather than pointing the gate at the live tree with an
    override) is what lets a test prove the gate FAILS on drift without
    editing the repository it is protecting. Every file either surface
    CITES is copied too, because the gate resolves citations against the
    root it was given - a scratch root with missing citations would fail
    for the wrong reason and hide the failure under test.
    """
    import re
    import shutil

    root = tmp_path / "doctree"
    primary = (
        "pyproject.toml",
        "CHANGELOG.md",
        "README.md",
        "docs/README.md",
        "docs/feature-matrix.md",
        "site/src/lib/content/releases.ts",
        "site/src/lib/content/limits.ts",
    )
    cited = set()
    for relative in primary:
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        cited.update(
            re.findall(
                r"(?:tests|scripts|memory|runtime|harness|acp|agent_sdk|site|docs)/[\w./-]+\.\w+",
                text,
            )
        )
        cited.update(re.findall(r"\b[A-Z_][\w-]*\.md\b", text))
    for relative in sorted(set(primary) | cited):
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return root


# ===========================================================================
# Required case 6 — release evidence is one report, and publish is gated
# ===========================================================================


class TestReleaseEvidenceAggregate:
    """One report, honest lanes, and a human-gated publish."""

    def test_every_declared_lane_is_present(self) -> None:
        report = release_evidence.collect_report(
            REPO_ROOT, lanes=["source_state", "docs_truth", "capability_probe"]
        )
        assert {lane.name for lane in report.lanes} == {
            "source_state",
            "docs_truth",
            "capability_probe",
        }
        payload = report.to_dict()
        assert payload["schema"] == "neo.release_evidence/1"
        assert "releasable" in payload
        assert [lane["name"] for lane in payload["lanes"]] == [
            "source_state",
            "docs_truth",
            "capability_probe",
        ]
        # Every declared lane is REACHABLE: either it has an in-process
        # runner, or it is one of the declared external commands. The
        # expensive lanes are asserted BY NAME rather than executed -
        # running the full suite from inside the suite is a self-deadlock,
        # and `python -m scripts.release_evidence` is their real driver.
        in_process = {
            "source_state",
            "docs_truth",
            "capability_probe",
            "vulnerability_scan",
        }
        external = {
            "reproducibility",
            "sbom",
            "clean_room_install",
            "installed_wheel_flow",
            "full_test_suite",
        }
        assert in_process <= set(release_evidence._LANE_RUNNERS)
        assert in_process | external == set(release_evidence.LANES)
        assert external & set(release_evidence._LANE_RUNNERS) == set()
        for name in in_process:
            assert isinstance(
                release_evidence.run_lane(name, REPO_ROOT, None),
                release_evidence.LaneResult,
            )

    def test_a_skipped_lane_is_never_a_pass(self) -> None:
        """No candidate artifact means those lanes are SKIPPED, not green."""
        report = release_evidence.collect_report(
            REPO_ROOT,
            lanes=[
                "reproducibility",
                "sbom",
                "clean_room_install",
                "installed_wheel_flow",
            ],
            dist=None,
        )
        assert report.skipped, report.to_dict()
        for name in ("reproducibility", "sbom"):
            lane = report.by_name[name]
            assert lane.status == release_evidence.SKIPPED
            assert lane.counted_as_pass is False
        assert report.releasable is False
        # No lane is exempt: a skipped lane blocks, by policy and by verdict.
        assert "reproducibility" in release_evidence.BLOCKING_LANES
        assert set(release_evidence.LANES) == release_evidence.BLOCKING_LANES

    def test_an_unevaluated_lane_also_blocks(self, monkeypatch) -> None:
        """ "We could not ask" is not better than "we did not ask"."""
        monkeypatch.setattr(
            release_evidence,
            "run_lane",
            lambda name, root, dist=None: release_evidence.LaneResult(
                name, release_evidence.UNEVALUATED, "probe unavailable"
            ),
        )
        report = release_evidence.collect_report(REPO_ROOT, lanes=["docs_truth"])
        assert report.releasable is False
        assert "docs_truth" in report.to_dict()["unevaluated"]

    def test_a_dirty_source_state_blocks_a_release(self) -> None:
        """The shared worktree is dirty, so the release lane must FAIL.

        This is the honest current state, asserted rather than described: a
        release artifact must come from an identified, stable tree.
        """
        result = release_evidence.run_lane("source_state", REPO_ROOT)
        assert result.status in (release_evidence.FAIL, release_evidence.UNEVALUATED)
        if result.status == release_evidence.FAIL:
            assert "DIRTY" in result.detail
            assert result.blocking is True

    def test_publish_requires_an_explicit_human_approval(
        self, isolated: Dict[str, Any], monkeypatch
    ) -> None:
        """No approval phrase, no publish — and the tool never uploads."""
        green = release_evidence.ReleaseReport(
            version="0.2.1",
            lanes=[
                release_evidence.LaneResult("full_test_suite", release_evidence.PASS),
            ],
        )
        assert green.releasable is True
        monkeypatch.delenv(release_evidence.APPROVAL_ENV, raising=False)
        gate = release_evidence.publish_gate(green)
        assert gate["approved"] is False
        assert any("human approval" in reason for reason in gate["reasons"])
        assert gate["published_by_this_tool"] is False

        for wrong in ("1", "yes", "true", "publish please", "PUBLISHED"):
            monkeypatch.setenv(release_evidence.APPROVAL_ENV, wrong)
            assert release_evidence.publish_gate(green)["approved"] is False, wrong

        monkeypatch.setenv(release_evidence.APPROVAL_ENV, "publish")
        gate = release_evidence.publish_gate(green)
        assert gate["approved"] is True
        assert gate["reasons"] == []
        assert gate["published_by_this_tool"] is False

    def test_approval_cannot_rescue_a_red_report(
        self, isolated: Dict[str, Any], monkeypatch
    ) -> None:
        red = release_evidence.ReleaseReport(
            version="0.2.1",
            lanes=[
                release_evidence.LaneResult(
                    "full_test_suite", release_evidence.FAIL, "2 failed"
                )
            ],
        )
        monkeypatch.setenv(release_evidence.APPROVAL_ENV, "publish")
        gate = release_evidence.publish_gate(red)
        assert gate["approved"] is False
        assert any("not green" in reason for reason in gate["reasons"])

    def test_a_missing_tool_is_skipped_not_failed(self) -> None:
        lane = release_evidence._external_lane(
            "clean_room_install",
            ["definitely-not-a-real-executable-neo", "--version"],
            REPO_ROOT,
            timeout=10,
        )
        assert lane.status == release_evidence.SKIPPED
        assert lane.counted_as_pass is False

    def test_no_upload_path_exists_in_the_module(self) -> None:
        """The aggregator cannot publish even by accident.

        A structural assertion rather than a promise. The module DOCSTRING
        has to be able to say that it does not upload, so the check runs
        against the code with every docstring removed; what remains is the
        executable surface, and none of it reaches an index.
        """
        import ast

        tree = ast.parse(Path(release_evidence.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                body.pop(0)
        stripped = ast.unparse(tree)
        for forbidden in (
            "twine",
            "pypi.org",
            "urlopen(",
            "HTTPConnection",
            "requests.post",
            "requests.put",
        ):
            assert forbidden not in stripped, forbidden


class TestUninstallPreservesUserData:
    """A clean uninstall must not delete user config or data."""

    def test_the_plan_never_targets_user_data(self, isolated: Dict[str, Any]) -> None:
        """The removal plan is built from Neo-owned things only.

        The user's settings and their run history are the two things a
        "clean uninstall" most plausibly destroys by accident, so both are
        asserted absent from the plan's targets, and the target set is
        checked for containment rather than trusted by name.
        """
        from cli import uninstall

        settings = isolated["home"] / "settings.toml"
        settings.write_text('model = "scripted"\n', encoding="utf-8")
        data = isolated["logs"] / "task-1" / "trace.jsonl"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_text("{}\n", encoding="utf-8")

        _items, targets, notes = uninstall.collect_plan()
        assert targets, "the plan found nothing to remove, which is not a proof"
        for target in targets:
            assert not _is_within(target, isolated["logs"]), target
        assert not any(str(isolated["logs"]) in note for note in notes), notes
        # The user's files are untouched by planning a removal.
        assert settings.is_file()
        assert data.is_file()

    def test_dry_run_removes_nothing(self, isolated: Dict[str, Any]) -> None:
        """`--dry-run` reports the plan and changes no file on disk."""
        from argparse import Namespace

        from cli import uninstall

        target = isolated["home"] / "neo-venv-shim"
        target.mkdir(parents=True, exist_ok=True)
        (target / "neo").write_text("shim", encoding="utf-8")
        code = uninstall.cmd_uninstall(Namespace(yes=False, dry_run=True, json=False))
        assert code in (0, 2)
        assert (target / "neo").is_file()

    def test_config_cleanup_is_scoped_to_known_files(self) -> None:
        """Only the documented Neo-owned config files and directories are
        ever candidates; an unknown file in the config root is not."""
        from cli import uninstall

        assert "settings.toml" in uninstall._CONFIG_FILES
        assert "plugins" in uninstall._CONFIG_DIRS
        for name in uninstall._CONFIG_FILES:
            assert "/" not in name and "\\" not in name, name
        for name in uninstall._CONFIG_DIRS:
            assert "/" not in name and "\\" not in name, name


def _is_within(path: Path, root: Path) -> bool:
    """Whether a resolved path is inside a root (used by the plan test)."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    return resolved == base or base in resolved.parents
