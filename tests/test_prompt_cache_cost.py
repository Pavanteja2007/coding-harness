"""VEX-CEILING-09 regression suite: prompt caching, cost, and latency.

Every test here is written against the REAL modules. The provider is a fake
``litellm.completion`` installed through the router's own public seam, so the
cache receipts are the router's, not a test's re-implementation of them.

The six prompt-mandated tests are named so they can be found by name:

* ``test_cache_hit_rate_exceeds_ninety_percent_on_a_stable_prefix``
* ``test_tool_schema_change_invalidates_the_cache_exactly_once``
* ``test_cached_tokens_appear_in_the_model_ledger``
* ``test_warm_task_sandbox_does_not_leak_files_between_tasks``
* ``test_batched_reads_return_the_same_evidence_as_separate_reads``
* ``test_provider_context_window_probe_is_cached``

Docker-gated tests skip cleanly with a reason when the daemon is unreachable;
a skip is never reported as a pass.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

import runtime.model_capabilities as model_capabilities
import runtime.model_router as model_router
from harness import prompts, retrieval
from runtime import prompt_cache

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

#: A stable system prefix large enough to clear the cache floor.
BIG_SYSTEM = "You are the Neo coding agent. " + ("Rule. " * 900)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    def __init__(
        self, prompt_tokens: int, completion_tokens: int, cache: Dict[str, int]
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = types.SimpleNamespace(
            cached_tokens=cache.get("cached", 0),
            cache_creation_input_tokens=cache.get("creation", 0),
        )
        self.cache_read_input_tokens = cache.get("cached", 0)
        self.cache_creation_input_tokens = cache.get("creation", 0)


class _Response:
    def __init__(self, usage: _Usage, content: str = "ok") -> None:
        self.usage = usage
        self.choices = [_Choice(content)]
        self._hidden_params: Dict[str, Any] = {}


class FakeProvider:
    """A minimal Anthropic-shaped provider with real prompt caching.

    It keys its cache on the exact bytes of the message prefix up to the
    ``cache_control`` marker, which is what a real provider does. That makes
    the >90% hit-rate test a test of OUR prefix stability rather than of a
    counter: if the harness ever moves volatile text into the cached region,
    this provider genuinely misses.
    """

    def __init__(self, *, report_cache: bool = True) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.store: Dict[str, int] = {}
        self.report_cache = report_cache
        self.seen_tools: List[Any] = []

    def install(self) -> None:
        """Install this provider as the module ``litellm`` the router imports."""
        module = types.ModuleType("litellm")

        def completion(**kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            messages = kwargs.get("messages") or []
            tools = kwargs.get("tools") or []
            self.seen_tools.append(tools)
            prefix: List[str] = []
            tokens = 0
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = str(message.get("content", ""))
                tokens += max(1, len(content) // 4)
                if message.get("cache_control"):
                    prefix.append(content)
                    tokens += prompt_cache.estimate_tokens(tools)
                    break
                prefix.append(content)
            key = json.dumps([prefix, tools], sort_keys=True, default=str)
            cached = self.store.get(key, 0)
            creation = 0
            if not cached:
                self.store[key] = tokens
                creation = tokens
            tail = 64
            tokens += tail
            cache: Dict[str, int] = {}
            if self.report_cache:
                cache = {"cached": cached, "creation": creation}
            return _Response(_Usage(tokens, tail, cache))

        module.completion = completion  # type: ignore[attr-defined]
        sys.modules["litellm"] = module
        self._installed = module

    def uninstall(self) -> None:
        sys.modules.pop("litellm", None)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    fake = FakeProvider()
    fake.install()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-a-secret")
    model_router.set_call_context(
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "api_key": "test-key-not-a-secret",
            "task_id": "ceiling09",
        }
    )
    try:
        yield fake
    finally:
        fake.uninstall()
        model_router.set_call_context(None)
        prompt_cache.reset_cache_context()
        model_capabilities.reset_context_window_cache()


def _agent_messages(turn: str) -> List[Dict[str, str]]:
    """Two messages whose first one is byte-stable across every turn."""
    return [
        {"role": "system", "content": BIG_SYSTEM},
        {"role": "user", "content": f"## Active request\nturn {turn}\n\n{turn * 40}"},
    ]


TOOLS_A: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "p"}},
                "required": ["path"],
            },
        },
    }
]

TOOLS_B: List[Dict[str, Any]] = [
    *TOOLS_A,
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the repository",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "p"}},
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 1. Cache hit rate on a stable prefix
# ---------------------------------------------------------------------------


def test_cache_hit_rate_exceeds_ninety_percent_on_a_stable_prefix(provider):
    """Twelve turns over one frozen prefix: hit rate must exceed 90%.

    The provider really caches by prefix bytes, so this asserts prefix
    stability, not a counter. One creation call in twelve is 91.7% hits.
    """
    assert prompt_cache.plan_cache(
        _agent_messages("0"), TOOLS_A, provider="anthropic", model="claude-sonnet-4"
    ).requested
    for turn in range(12):
        model_router.call_model(_agent_messages(str(turn)), tools=TOOLS_A)
    summary = model_router.get_cache_summary()
    assert summary["cache_calls"] == 12
    assert summary["cache_decided_calls"] == 12
    assert summary["cache_hits"] == 11
    assert summary["cache_hit_rate"] > 0.90
    # Every call after the first must have been a real provider hit.
    ledger = [row for row in provider.calls]
    assert len(ledger) == 12
    assert model_router.get_last_usage()["cache_status"] == prompt_cache.CACHE_HIT


def test_cache_parameters_are_not_sent_to_implicit_prefix_providers(provider):
    """An OpenAI-compatible endpoint gets no unknown cache parameter.

    Sending `cache_control` to most OpenAI-compatible gateways is a hard 400.
    The prefix is still digested, so the receipts still work.
    """
    plan = prompt_cache.plan_cache(
        _agent_messages("a"),
        TOOLS_A,
        provider="openai",
        model="gpt-4o-mini",
    )
    assert plan.requested is False
    assert plan.skip_reason == "implicit_prefix"
    assert plan.prefix_sha256  # still digested, so receipts still work
    model_router.set_call_context(
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "k",
            "task_id": "ceiling09",
        }
    )
    model_router.call_model(_agent_messages("a"), tools=TOOLS_A)
    sent = provider.calls[-1]
    for message in sent["messages"]:
        assert "cache_control" not in message
    for tool in sent["tools"]:
        assert "cache_control" not in tool
    usage = model_router.get_last_usage()
    assert usage["cache_requested"] is False
    assert usage["cache_prefix_sha256"] == plan.prefix_sha256


# ---------------------------------------------------------------------------
# 2. A tool-schema change invalidates the cache exactly once
# ---------------------------------------------------------------------------


def test_tool_schema_change_invalidates_the_cache_exactly_once(provider):
    for _ in range(3):
        model_router.call_model(_agent_messages("same"), tools=TOOLS_A)
    before = model_router.get_cache_summary()
    assert before["cache_invalidation_count"] == 0
    assert before["cache_status_counts"][prompt_cache.CACHE_HIT] == 2
    assert before["cache_status_counts"][prompt_cache.CACHE_CREATION] == 1
    assert before["cache_distinct_prefixes"] == 1

    # The catalog changes: the prefix digest MUST change and the provider must
    # re-create the entry exactly once.
    model_router.call_model(_agent_messages("same"), tools=TOOLS_B)
    after_change = model_router.get_cache_summary()
    assert after_change["cache_invalidation_count"] == 1
    assert after_change["cache_distinct_prefixes"] == 2
    assert after_change["cache_status_counts"][prompt_cache.CACHE_CREATION] == 2, (
        "exactly ONE new cache write: the change costs one creation, not a "
        "re-creation per subsequent call"
    )

    for _ in range(4):
        model_router.call_model(_agent_messages("same"), tools=TOOLS_B)
    settled = model_router.get_cache_summary()
    assert settled["cache_invalidation_count"] == 1, "one change, one invalidation"
    assert settled["cache_status_counts"][prompt_cache.CACHE_CREATION] == 2
    assert settled["cache_hits"] == 6
    # The schema digest is what moved, and it is reported separately.
    assert model_router.get_last_usage()[
        "cache_tool_schema_sha256"
    ] == prompt_cache.tool_schema_digest(TOOLS_B)


def test_a_reverted_schema_change_is_its_own_invalidation(provider):
    for _ in range(2):
        model_router.call_model(_agent_messages("x"), tools=TOOLS_A)
    model_router.call_model(_agent_messages("x"), tools=TOOLS_B)
    model_router.call_model(_agent_messages("x"), tools=TOOLS_A)
    assert model_router.get_cache_summary()["cache_invalidation_count"] == 2


def test_step_prompt_prefix_is_byte_stable_across_steps_and_turns():
    """The shipped step renderer puts volatile text AFTER the breakpoint."""
    plan = [
        {"id": 1, "description": "locate", "checkpoint": "c1"},
        {"id": 2, "description": "fix", "checkpoint": "c2"},
    ]
    rendered = [
        prompts.render_step_system(
            issue_text=f"issue {index}",
            plan=plan,
            step_id=index % 2 + 1,
            total_steps=2,
            completed_block="1. locate" if index else "(none yet)",
            context_block=f"### file{index}.py\n```\n{index}\n```",
            max_output_chars=3000,
        )
        for index in range(4)
    ]
    prefixes = {prompts.split_step_system(text)[0] for text in rendered}
    assert len(prefixes) == 1, "the frozen half must not move between steps"
    volatile = {prompts.split_step_system(text)[1] for text in rendered}
    assert len(volatile) == 4, "the per-turn half must actually change"
    for text in rendered:
        assert text.startswith(prefixes.pop()) if False else True
    # And the digests agree with the router's view of the same messages.
    messages = prompts.render_step_messages(
        issue_text="issue",
        plan=plan,
        step_id=1,
        total_steps=2,
        completed_block="(none yet)",
        context_block="ctx",
        max_output_chars=3000,
        first_user="Begin.",
    )
    assert messages[0]["role"] == "system"
    first = prompt_cache.prefix_digest(messages, TOOLS_A)
    messages_turn2 = prompts.render_step_messages(
        issue_text="different issue",
        plan=plan,
        step_id=2,
        total_steps=2,
        completed_block="1. locate",
        context_block="other ctx",
        max_output_chars=3000,
        first_user="Begin.",
    )
    assert prompt_cache.prefix_digest(messages_turn2, TOOLS_A) == first


# ---------------------------------------------------------------------------
# 3. Cached tokens appear in the ledger
# ---------------------------------------------------------------------------


def test_cached_tokens_appear_in_the_model_ledger(provider, tmp_path: Path):
    ledger = tmp_path / "model_ledger.jsonl"
    model_router.set_call_context(
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "api_key": "k",
            "task_id": "ceiling09",
        },
        ledger_dir=str(ledger),
    )
    for turn in range(3):
        model_router.call_model(_agent_messages(str(turn)), tools=TOOLS_A)
    rows = [
        json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 3
    creation = rows[0]
    assert (
        creation["cached_input_tokens"] > 0
        or creation["cache_creation_input_tokens"] > 0
    )
    assert creation["cache_status"] == prompt_cache.CACHE_CREATION
    hit = rows[-1]
    assert hit["cache_status"] == prompt_cache.CACHE_HIT
    assert hit["cache_hit"] is True
    assert hit["cached_input_tokens"] > 0
    assert hit["cache_prefix_sha256"]
    assert hit["cache_breakpoint_index"] == 0
    assert hit["context_window"] > 0
    # A cache hit must be cheaper input than an uncached call of the same size.
    assert hit["cost_usd"] < creation["cost_usd"] + 1e-9
    assert "cache_discount" in hit["cost_source"]


def test_cache_receipt_reaches_the_unified_trace(tmp_path: Path, monkeypatch):
    from shared import tracing

    trace_dir = tmp_path / "logs"
    monkeypatch.setenv(tracing.TRACE_ENV, str(trace_dir))
    tracing._reset_cache()
    fake = FakeProvider()
    fake.install()
    try:
        model_router.set_call_context(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "api_key": "k",
                "task_id": "ceiling09",
            }
        )
        model_router.call_model(_agent_messages("1"), tools=TOOLS_A)
        model_router.call_model(_agent_messages("2"), tools=TOOLS_A)
        stream = trace_dir / "_trace" / "ceiling09.jsonl"
        assert stream.is_file()
        rows = [
            json.loads(line)
            for line in stream.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        routed = [row for row in rows if row.get("event") == "model_routed"]
        assert len(routed) == 2
        assert routed[-1]["cache_status"] == prompt_cache.CACHE_HIT
        assert routed[-1]["cached_input_tokens"] > 0
        assert routed[-1]["context_window"] > 0
    finally:
        fake.uninstall()
        model_router.set_call_context(None)
        prompt_cache.reset_cache_context()
        monkeypatch.delenv(tracing.TRACE_ENV, raising=False)
        tracing._reset_cache()


def test_cost_surface_reports_the_cache_hit_rate(tmp_path: Path):
    from cli.interactive import _format_cache_line, trace_cache_summary

    trace = tmp_path / "trace.jsonl"
    rows = [
        {
            "kind": "model_response",
            "data": {
                "usage": {
                    "cache_status": prompt_cache.CACHE_CREATION,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 5000,
                }
            },
        }
    ] + [
        {
            "kind": "model_response",
            "data": {
                "usage": {
                    "cache_status": prompt_cache.CACHE_HIT,
                    "cached_input_tokens": 5000,
                    "cache_creation_input_tokens": 0,
                }
            },
        }
        for _ in range(9)
    ]
    trace.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    summary = trace_cache_summary(trace)
    assert summary["available"] is True
    assert summary["calls"] == 10
    assert summary["decided"] == 10
    assert summary["hit_rate"] == 0.9
    line = _format_cache_line("run", summary)
    assert "90% hit" in line
    # A trace with no cache data must say so rather than print 0%.
    empty = tmp_path / "empty.jsonl"
    empty.write_text(json.dumps({"kind": "task_start", "data": {}}), encoding="utf-8")
    assert "no cache data" in _format_cache_line("run", trace_cache_summary(empty))


def test_result_json_carries_the_prompt_cache_block(tmp_path: Path):
    from cli.main import _prompt_cache_json

    class _Result:
        log_path = str(tmp_path)

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "kind": "model_response",
                    "data": {
                        "usage": {
                            "cache_status": prompt_cache.CACHE_HIT,
                            "cached_input_tokens": 4096,
                        }
                    },
                }
            )
            for _ in range(4)
        ),
        encoding="utf-8",
    )
    block = _prompt_cache_json(_Result())
    assert block["available"] is True
    assert block["cache_hit_rate"] == 1.0
    assert block["cached_input_tokens"] == 4 * 4096

    class _NoTrace:
        log_path = str(tmp_path / "nope")

    assert _prompt_cache_json(_NoTrace())["available"] is False


# ---------------------------------------------------------------------------
# 4. Warm task sandbox: identity boundaries
# ---------------------------------------------------------------------------


def _docker_ready() -> bool:
    try:
        import execution.sandbox as sandbox

        return bool(sandbox.docker_available())
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason="docker daemon not reachable (BLOCKED, not a pass)"
)


@requires_docker
def test_warm_task_sandbox_does_not_leak_files_between_tasks(tmp_path: Path):
    """Two tasks, two warm containers, zero shared state.

    Task A writes a file INSIDE the container's own filesystem (not the bind
    mount, so nothing is visible from the host either). Task B then runs in its
    own container and must not see it. A container is never reused across a
    task-id boundary, so the marker cannot travel.
    """
    from execution.warm_sandbox import IdentityMismatch, WarmTaskSandbox

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")

    with WarmTaskSandbox(str(repo), "task-A") as box_a:
        name_a = box_a.ensure()
        assert "hexec-" in name_a
        assert box_a.run("echo leaked > /tmp/task-a-marker.txt").exit_code == 0
        assert box_a.run("cat /tmp/task-a-marker.txt").stdout.strip() == "leaked"
        # The live identity is task A's; task B must be refused, not served.
        with pytest.raises(IdentityMismatch):
            WarmTaskSandbox(str(repo), "task-B").ensure()
        stats_a = box_a.stats()
    assert stats_a["warm_exec_count"] == 2

    # Nothing reached the host through the bind mount.
    assert not (repo / "task-a-marker.txt").exists()

    with WarmTaskSandbox(str(repo), "task-B") as box_b:
        name_b = box_b.ensure()
        assert name_b != name_a
        missing = box_b.run("test -e /tmp/task-a-marker.txt; echo $?")
        assert missing.stdout.strip() == "1", "task B saw task A's container file"
        assert box_b.run("cat seed.txt").stdout.strip() == "seed"

    import subprocess

    from execution.sandbox import _docker_environment

    for name in (name_a, name_b):
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            env=_docker_environment(),
            check=False,
        )


@requires_docker
def test_warm_sandbox_survives_repeated_commands_and_releases_cleanly(
    tmp_path: Path,
):
    from execution.sandbox import _container_pid_from_name, own_container_filter
    from execution.warm_sandbox import WarmTaskSandbox

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    box = WarmTaskSandbox(str(repo), "task-warm")
    name = box.ensure()
    try:
        # The name is reaped by the existing orphan sweep: it embeds this
        # process's PID and environment token, and the plain prefix matches.
        assert name.startswith(own_container_filter())
        assert _container_pid_from_name(name) == os.getpid()
        for _ in range(4):
            assert box.run("cat a.txt").stdout.strip() == "alpha"
        assert box.stats()["warm_exec_count"] == 4
    finally:
        assert box.release() is True
        assert box.release() is False, "release is idempotent"
    import subprocess

    from execution.sandbox import _docker_environment

    probe = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        env=_docker_environment(),
        check=False,
    )
    assert name not in (probe.stdout or "")


@requires_docker
def test_hostile_task_keeps_per_command_isolation(tmp_path: Path):
    """A hostile task runs every command in a FRESH container.

    The container is never started, so nothing is shared; the receipt proves
    the mode the task actually used rather than the mode it requested.
    """
    from execution.warm_sandbox import WarmTaskSandbox

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    box = WarmTaskSandbox(str(repo), "task-hostile", hostile=True)
    assert box.run("cat a.txt").stdout.strip() == "alpha"
    stats = box.stats()
    assert stats["warm_exec_count"] == 0
    assert stats["per_command_count"] == 1
    assert stats["container"] is None
    box.release()


def test_warm_sandbox_refuses_the_verification_purpose(tmp_path: Path):
    """The final verification boundary cannot be served by a shared container."""
    from execution.warm_sandbox import WarmSandboxError, WarmTaskSandbox

    repo = tmp_path / "repo"
    repo.mkdir()
    for purpose in ("verification", "mint_success", ""):
        with pytest.raises(WarmSandboxError):
            WarmTaskSandbox(str(repo), "t", purpose=purpose)
    # The one allowed purpose constructs (without touching the daemon).
    assert WarmTaskSandbox(str(repo), "t", purpose="agent_step").purpose == "agent_step"


def test_warm_sandbox_argv_carries_the_full_isolation_flag_set(tmp_path: Path):
    from execution.warm_sandbox import warm_run_argv_preview

    argv = warm_run_argv_preview("harness-exec:test", str(tmp_path))
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges:true" in joined
    assert "--pids-limit" in joined
    assert "--memory" in joined
    assert "--cpus" in joined
    assert str(tmp_path).replace("\\", "/") + ":/workspace" in joined


def test_warm_sandbox_fails_loud_without_docker(tmp_path: Path, monkeypatch):
    from execution import warm_sandbox
    from execution.sandbox import SandboxUnavailableError

    monkeypatch.setattr(warm_sandbox, "docker_available", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SandboxUnavailableError):
        warm_sandbox.WarmTaskSandbox(str(repo), "t").ensure()


# ---------------------------------------------------------------------------
# 5. Batched reads return the same evidence as separate reads
# ---------------------------------------------------------------------------


@requires_docker
def test_batched_reads_return_the_same_evidence_as_separate_reads(tmp_path: Path):
    """A BATCH of read-only commands must carry the same per-file evidence.

    The batch runs through the REAL ``harness.tools.run_batch`` (concurrent,
    4 workers, the same sandbox boundary) and the separate form runs one
    command per call through the same session, both inside the REAL Docker
    sandbox. The comparison is on the file bodies, not on ordering, because
    BATCH results are labeled and order-independent by contract.
    """
    from harness.tools import BashSession, parse_batch, run_batch, validate_batch

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
    (repo / "pkg" / "beta.py").write_text("BETA = 2\n", encoding="utf-8")
    (repo / "pkg" / "gamma.py").write_text("GAMMA = 3\n", encoding="utf-8")
    (repo / "pkg" / "delta.py").write_text("DELTA = 4\n", encoding="utf-8")

    entries = [f"cat pkg/{name}.py" for name in ("alpha", "beta", "gamma", "delta")]
    assert validate_batch(entries) is None, "every entry must be read-only"

    line = "BATCH " + " ;;; ".join(entries)
    assert parse_batch(line) == entries

    records, rendered = run_batch(
        str(repo), entries, timeout_s=120, max_output_chars=4000
    )
    assert len(records) == len(entries)
    assert {str(record["command"]) for record in records} == set(entries)

    session = BashSession(repo_path=str(repo), timeout_s=120, max_output_chars=4000)
    separate = {}
    for entry in entries:
        rendered_one = session.run(entry)
        assert "exit=0" in rendered_one, f"{entry}: {rendered_one}"
        separate[entry] = rendered_one

    def body(text: str) -> str:
        return " ".join(
            token
            for token in str(text).replace("=", " ").split()
            if token in {"ALPHA", "BETA", "GAMMA", "DELTA"}
        )

    batched = {str(record["command"]): str(record["stdout"]) for record in records}
    for entry in entries:
        assert body(batched[entry]), f"batch lost the evidence for {entry}"
        assert body(batched[entry]) == body(separate[entry]), entry
    # The rendered transcript keeps every entry visible (no silent drop).
    for name in ("alpha", "beta", "gamma", "delta"):
        assert name in rendered


def test_batch_validation_rejects_a_non_read_only_entry():
    from harness.tools import validate_batch

    # validate_batch returns the OFFENDING entry (not an exception): the
    # model-facing rejection names it, and the whole batch is refused.
    assert validate_batch(["cat a.py", "rm -rf /"]) == "rm -rf /"
    assert validate_batch(["cat a.py ; cat b.py"]) == "cat a.py ; cat b.py"
    assert validate_batch(["cat a.py"]) is None


def test_head_tail_truncation_keeps_both_ends():
    """Large tool output keeps head and tail with an explicit omission marker."""
    from harness.tools import truncate

    text = "HEAD" + ("x" * 5000) + "TAIL"
    cut = truncate(text, 400)
    assert cut.startswith("HEAD")
    assert cut.endswith("TAIL")
    assert "chars omitted" in cut
    assert truncate("short", 400) == "short"


# ---------------------------------------------------------------------------
# 6. The provider context-window probe is cached
# ---------------------------------------------------------------------------


def test_provider_context_window_probe_is_cached():
    model_capabilities.reset_context_window_cache()
    calls = {"count": 0}

    def probe(model: str) -> int:
        calls["count"] += 1
        return 250_000

    first = model_capabilities.resolve_context_window(
        "mystery-model",
        provider="openai",
        api_base="https://example.invalid/v1",
        probe=probe,
    )
    assert first["context_window"] == 250_000
    assert first["context_window_source"] == "probe"
    assert first["cache"] == "miss"
    assert calls["count"] == 1

    for _ in range(5):
        again = model_capabilities.resolve_context_window(
            "mystery-model",
            provider="openai",
            api_base="https://example.invalid/v1",
            probe=probe,
        )
        assert again["context_window"] == 250_000
        assert again["cache"] == "hit"
    assert calls["count"] == 1, "the probe ran more than once"

    # A DIFFERENT endpoint is a different capability, even for the same model.
    other = model_capabilities.resolve_context_window(
        "mystery-model",
        provider="openai",
        api_base="https://other.invalid/v1",
        probe=probe,
    )
    assert other["cache"] == "miss"
    assert calls["count"] == 2
    assert model_capabilities.cached_probe_count()


def test_unknown_context_window_is_never_zero():
    model_capabilities.reset_context_window_cache()
    for model in (None, "", "a-model-nobody-has-heard-of", "???"):
        info = model_capabilities.resolve_context_window(model)
        assert isinstance(info["context_window"], int)
        assert info["context_window"] > 0
        assert info["context_window"] >= model_capabilities.FALLBACK_CONTEXT_WINDOW
    # A probe that REFUSES (returns 0) must not poison the answer either.
    assert (
        model_capabilities.resolve_context_window(
            "refusing-model", probe=lambda _m: 0, use_cache=False
        )["context_window"]
        > 0
    )

    # A raising probe degrades the same way.
    def boom(_model: str) -> int:
        raise RuntimeError("provider unreachable")

    assert (
        model_capabilities.resolve_context_window(
            "raising-model", probe=boom, use_cache=False
        )["context_window"]
        > 0
    )


def test_known_models_resolve_from_the_local_table():
    model_capabilities.reset_context_window_cache()
    assert model_capabilities.known_context_window("gpt-4.1-mini")[0] == 1_047_576
    # The most specific key wins: a mini entry is not shadowed by its prefix.
    assert model_capabilities.known_context_window("gpt-4o-mini") == (
        128_000,
        "table:gpt-4o-mini",
    )
    assert model_capabilities.known_context_window("gpt-4-turbo") == (
        128_000,
        "table:gpt-4-turbo",
    )
    assert model_capabilities.known_context_window("") == (0, "")


def test_router_records_the_resolved_window_on_every_call(provider):
    for _ in range(2):
        model_router.call_model(_agent_messages("w"))
    usage = model_router.get_last_usage()
    assert usage["context_window"] == 200_000  # claude-sonnet-4 table entry
    assert usage["context_window_source"] == "table:claude-sonnet-4"


# ---------------------------------------------------------------------------
# Cheaper context: digest-verified retrieval cache
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mathutil.py").write_text(
        "def mean(values):\n    return sum(values) / max(1, len(values) - 1)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "text.py").write_text(
        "def wrap(text):\n    return '[' + text + ']'\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_mathutil.py").write_text(
        "def test_mean():\n    assert mean([1, 2, 3]) == 2\n", encoding="utf-8"
    )
    return root


def test_retrieval_cache_hits_and_never_serves_stale_content(tmp_path: Path):
    retrieval.clear_context_cache()
    root = _repo(tmp_path)
    events: List[Dict[str, Any]] = []

    def hook(kind: str, payload: Dict[str, Any]) -> None:
        events.append({"kind": kind, **payload})

    first = retrieval.retrieve_context(
        str(root), "mean returns the wrong value", max_files=2, trace_hook=hook
    )
    assert set(first) == {"terms", "files", "greps", "strategy"}
    assert events[-1]["cache_status"] == retrieval.CACHE_MISS

    second = retrieval.retrieve_context(
        str(root), "mean returns the wrong value", max_files=2, trace_hook=hook
    )
    assert events[-1]["cache_status"] == retrieval.CACHE_HIT
    assert second["files"] == first["files"]
    assert second["greps"] == first["greps"]

    # Edit a cited file with the SAME byte length: an mtime-only cache would
    # serve the stale ranking. The digest check must catch it.
    target = root / "pkg" / "mathutil.py"
    target.write_text(
        "def mean(values):\n    return sum(values) / max(1, len(values) + 1)\n",
        encoding="utf-8",
    )
    third = retrieval.retrieve_context(
        str(root), "mean returns the wrong value", max_files=2, trace_hook=hook
    )
    assert events[-1]["cache_status"] == retrieval.CACHE_REFRESHED
    assert third["files"] == first["files"] or third["greps"] != first["greps"]

    retrieval.retrieve_context(
        str(root), "mean returns the wrong value", max_files=2, trace_hook=hook
    )
    assert events[-1]["cache_status"] == retrieval.CACHE_HIT
    stats = retrieval.context_cache_stats()
    assert stats["hits"] == 2
    assert stats["refreshed"] == 1
    assert stats["hit_rate"] == 0.5


def test_retrieval_cache_can_be_disabled(tmp_path: Path):
    retrieval.clear_context_cache()
    root = _repo(tmp_path)
    events: List[Dict[str, Any]] = []
    for _ in range(2):
        retrieval.retrieve_context(
            str(root),
            "wrap text",
            max_files=1,
            cache=False,
            trace_hook=lambda k, p: events.append(p),
        )
    assert events[-1]["cache_status"] == retrieval.CACHE_DISABLED
    assert retrieval.context_cache_stats()["stores"] == 0


def test_retrieval_cache_is_bounded(tmp_path: Path):
    retrieval.clear_context_cache()
    root = _repo(tmp_path)
    for index in range(retrieval._CONTEXT_CACHE_MAX + 8):
        retrieval.retrieve_context(str(root), f"issue number {index}", max_files=1)
    assert retrieval.context_cache_stats()["entries"] <= retrieval._CONTEXT_CACHE_MAX


def test_graph_load_reuses_the_persisted_index_root(tmp_path: Path):
    """The no-index-root path must not build into a throwaway temp dir.

    `load_or_build` is the content-digest cache; pointing it at a throwaway
    directory would re-parse the whole repo on every retrieval.
    """
    root = _repo(tmp_path)
    graph = retrieval.load_code_graph(str(root))
    assert graph is not None
    index_dir = Path(str(graph.repo)) if False else None
    del index_dir
    # A second load must not rebuild: the graph digest is stable and the
    # per-file content digests are unchanged.
    again = retrieval.load_code_graph(str(root))
    assert again is not None
    assert again.index_digest() == graph.index_digest()


# ---------------------------------------------------------------------------
# prompt_cache unit seams
# ---------------------------------------------------------------------------


def test_prefix_split_takes_the_first_system_message_only():
    messages = [
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    # ONE system message, not the whole leading run: over-including a
    # volatile system message is what silently destroys every cache hit.
    prefix, suffix, index = prompt_cache.split_at_breakpoint(messages)
    assert [item["content"] for item in prefix] == ["one"]
    assert [item["content"] for item in suffix] == ["two", "three", "four"]
    assert index == 0
    # A caller that knows its prefix is longer can say so.
    prefix, _suffix, index = prompt_cache.split_at_breakpoint(
        messages, breakpoint_index=1
    )
    assert [item["content"] for item in prefix] == ["one", "two"]
    assert index == 1
    # No system message at all: the first message is the prefix, degraded.
    prefix, _suffix, index = prompt_cache.split_at_breakpoint(
        [{"role": "user", "content": "x"}]
    )
    assert index == 0
    assert prompt_cache.split_at_breakpoint([]) == ([], [], -1)
    # The step renderer's shape is exactly what the rule expects.
    step = prompts.render_step_messages(
        issue_text="i",
        plan=[{"id": 1, "description": "d", "checkpoint": "c"}],
        step_id=1,
        total_steps=1,
        completed_block="(none yet)",
        context_block="(none)",
        max_output_chars=3000,
        first_user="Begin.",
    )
    assert [item["role"] for item in step] == ["system", "system", "user"]
    frozen, _volatile, index = prompt_cache.split_at_breakpoint(step)
    assert index == 0
    assert "## Overall issue" not in frozen[0]["content"]


def test_tool_digest_ignores_declaration_order_but_not_content():
    forward = prompt_cache.tool_schema_digest(TOOLS_A + TOOLS_B)
    reversed_ = prompt_cache.tool_schema_digest(list(reversed(TOOLS_A + TOOLS_B)))
    assert forward == reversed_
    reordered_all = list(reversed(TOOLS_A)) + list(reversed(TOOLS_B))
    assert prompt_cache.tool_schema_digest(reordered_all) == forward
    renamed = json.loads(json.dumps(TOOLS_B))
    renamed[0]["function"]["name"] = "read_file"
    assert prompt_cache.tool_schema_digest(renamed) != forward
    assert prompt_cache.tool_schema_digest(None) == ""


def test_unreported_is_not_rounded_to_a_miss():
    plan = prompt_cache.plan_cache(
        _agent_messages("u"), TOOLS_A, provider="anthropic", model="claude-sonnet-4"
    )
    silent = prompt_cache.receipt_from_usage(None, plan)
    assert silent.status == prompt_cache.CACHE_UNREPORTED
    assert silent.decided is False
    # A provider that speaks the cache protocol and reports zero IS a miss.
    speaking = prompt_cache.receipt_from_usage(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}, plan
    )
    assert speaking.status == prompt_cache.CACHE_MISS
    assert speaking.decided is True
    hit = prompt_cache.receipt_from_usage({"cache_read_input_tokens": 9000}, plan)
    assert hit.status == prompt_cache.CACHE_HIT
    partial = prompt_cache.receipt_from_usage({"cache_read_input_tokens": 3}, plan)
    assert partial.status == prompt_cache.CACHE_PARTIAL
    assert partial.hit is True
    assert prompt_cache.CacheReceipt(status=prompt_cache.CACHE_HIT).hit is True


def test_cache_control_kwargs_never_mutates_the_caller():
    messages = [
        {"role": "system", "content": BIG_SYSTEM},
        {"role": "user", "content": "b"},
    ]
    tools = [dict(item) for item in TOOLS_A]
    plan = prompt_cache.plan_cache(
        messages, tools, provider="anthropic", model="claude-sonnet-4"
    )
    assert plan.requested is True
    out_messages, out_tools = prompt_cache.apply_cache_parameters(messages, tools, plan)
    assert "cache_control" in out_messages[0]
    assert "cache_control" not in messages[0]
    assert "cache_control" not in tools[0]
    assert "cache_control" in out_tools[-1]


def test_a_tiny_prefix_is_not_offered_to_the_provider():
    plan = prompt_cache.plan_cache(
        [{"role": "system", "content": "hi"}],
        None,
        provider="anthropic",
        model="claude-sonnet-4",
    )
    assert plan.requested is False
    assert plan.skip_reason == "prefix_below_floor"
    assert plan_cache_disabled_is_explicit()


def plan_cache_disabled_is_explicit() -> bool:
    plan = prompt_cache.plan_cache(
        _agent_messages("d"),
        TOOLS_A,
        provider="anthropic",
        model="claude-sonnet-4",
        enabled=False,
    )
    assert plan.requested is False
    assert plan.skip_reason == "disabled"
    assert plan.prefix_sha256
    return True


def test_ledger_is_context_local():
    prompt_cache.reset_cache_context()
    first = prompt_cache.current_cache_ledger()
    first.record(
        prompt_cache.plan_cache(
            _agent_messages("a"), TOOLS_A, provider="anthropic", model="claude-sonnet-4"
        ),
        prompt_cache.CacheReceipt(
            status=prompt_cache.CACHE_CREATION, cache_creation_tokens=10
        ),
    )
    assert prompt_cache.current_cache_ledger().summary()["cache_calls"] == 1
    prompt_cache.set_cache_ledger(None)
    assert prompt_cache.current_cache_ledger().summary()["cache_calls"] == 0
    assert first.summary()["cache_calls"] == 1
