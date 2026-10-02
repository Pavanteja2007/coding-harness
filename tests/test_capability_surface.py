"""AGT-01 - one capability surface for every strategy.

The defect this closes: the kernel declared FOUR tool lists
(``_DAILY_TOOLS`` 45, ``_PLANNING_TOOLS`` 11, ``_QUESTION_TOOLS`` 9,
``_RESEARCH_TOOLS`` 10) and three of the four had no retrieval at all, so
"can u tell me the best mouse under 1000 rupees in india" could not be
answered by the strategy that answers questions. The degradation was also
invisible: nothing in a run's receipt said what had been taken away.

Every test here is named after a BEHAVIOUR and measures it:

1. every strategy inherits from the ONE surface, and a tool added to the
   canonical catalog is reachable everywhere without a strategy "remembering"
   it;
2. ``question`` and ``planning`` can reach ``web_fetch`` and ``web_search`` -
   proven by a real dispatch through the kernel and by the schemas actually
   handed to the model, not by set membership alone;
3. withheld capabilities are reported, with a reason, in both the journal and
   the result receipt - and an operator narrowing can only SUBTRACT;
4. ``question`` still cannot mutate or shell, end to end through a real run;
5. a run receipt names the effective surface, and carries no completion
   vocabulary (a capability receipt must not be able to imply that anything
   was verified).

The last one matters: this file is not permitted to weaken the verifier gate.
Nothing here can turn ``completed_unverified`` into success, and
``test_the_capability_receipt_cannot_claim_anything_was_verified`` pins that.

Host-only: no Docker, no provider, no network. The retrieval proof drives the
real ``harness.webfetch`` entry point with only its socket layer replaced, so
the egress / SSRF / cap / untrusted-review path is the production one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent_kernel import (
    STRATEGY_WITHHELD,
    UNCLASSIFIED_CAPABILITY,
    AgentKernel,
    CompletionStatus,
    RunEventJournal,
    RunSpec,
    ToolRegistry,
    capability_receipt,
    capability_surface,
    render_capability_note,
)
from harness.agent_kernel.gateway import ModelGateway
from harness.agent_kernel.kernel import _allowed_tools, _canonical_tool
from harness.agent_kernel.strategy import build_default_handlers
from harness.tools import typed_tool_specs

#: The exact sentence from the defect report. If a future change makes this
#: unreachable again, the tests that use it fail for the right reason.
FAILED_SENTENCE = "can u tell me the best mouse under 1000 rupees in india"

RETRIEVAL_TOOLS = ("web_fetch", "web_search")
READ_ONLY_STRATEGIES = ("planning", "question", "research")
MUTATING_TOOLS = (
    "edit",
    "write",
    "apply_patch",
    "rename",
    "delete",
    "undo",
    "rename_symbol",
    "update_signature",
)
COMMAND_TOOLS = ("shell", "process", "build", "lint", "typecheck")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """A callable model double that replays a fixed list of replies.

    It records the ``tools`` keyword the kernel handed it, which is the only
    honest way to ask "what was this model actually told it could call?".
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = []
        self.advertised: list = []

    def __call__(self, messages, **kwargs):
        self.messages.append([dict(message) for message in messages])
        self.advertised.append(list(kwargs.get("tools") or []))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_last_usage(self):
        return {
            "model": "scripted-capability",
            "provider": "fake",
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "tokens": 20,
            "cost_usd": 0.0,
        }

    def advertised_names(self) -> set:
        return {
            str(entry.get("function", {}).get("name") or "")
            for entry in (self.advertised[0] if self.advertised else [])
        }


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _spec(repo: Path, run_id: str, **kwargs) -> RunSpec:
    return RunSpec(
        session_id=f"session-{run_id}",
        run_id=run_id,
        request=kwargs.pop("request", "explain the code"),
        repository_identity=str(repo),
        **kwargs,
    )


def _kernel(repo: Path, tmp_path: Path, model, **config) -> AgentKernel:
    values = {"agent_approval": "auto", "steering_enabled": False}
    values.update(config)
    return AgentKernel(
        repo_path=str(repo),
        log_root=tmp_path / "logs",
        model_gateway=ModelGateway(call_fn=model),
        config=values,
    )


def _events(tmp_path: Path, run_id: str, event_type: str) -> list:
    journal = RunEventJournal(tmp_path / "logs" / run_id / "trace.jsonl")
    return [event for event in journal.read_events() if event.event_type == event_type]


def _refusals(tmp_path: Path, run_id: str, tool: str) -> list:
    """Return the reasons the run reported for refusing a withheld tool.

    A withheld tool is refused at the PROTOCOL boundary - the call never
    reaches a handler - so the refusal surfaces as a ``tool_recovery`` reason
    naming the tool, and is fed back to the model so it can correct itself.
    """
    found = []
    for event_type in ("tool_recovery", "tool_validation_error"):
        for event in _events(tmp_path, run_id, event_type):
            blob = json.dumps(event.payload)
            if f"unknown tool: {tool}" in blob:
                found.append(event)
    return found


def _install_fake_fetch(
    monkeypatch, *, text="Logitech B100 wired mouse - Rs 450"
) -> dict:
    """Replace only the socket layer of the real web reader.

    ``harness.webfetch.fetch_webpage`` is the production entry point: it
    validates the scheme, resolves and blocks the host, checks the egress
    allowlist, caps the read, and runs the untrusted-content review. Only the
    socket is replaced, so every one of those guards still runs.
    """
    import harness.webfetch as webfetch

    captured: dict = {}

    def _fake_open(req, timeout_s):
        captured["url"] = str(getattr(req, "full_url", ""))
        captured["timeout_s"] = timeout_s
        body = ("<html><body><main>" + text + "</main></body></html>").encode("utf-8")
        served = {"done": False}

        class _Response:
            status = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def read(self, _size=-1):
                # The real reader loops until the socket is drained, so the
                # double has to actually end. A read that always returned
                # content would trip the size cap and report ``too_large``.
                if served["done"]:
                    return b""
                served["done"] = True
                return body

            def geturl(self):
                return captured["url"]

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return _Response()

    monkeypatch.setattr(webfetch, "_open_response", _fake_open)
    return captured


# ---------------------------------------------------------------------------
# 1. every strategy inherits from ONE surface
# ---------------------------------------------------------------------------


def test_every_catalog_tool_belongs_to_exactly_one_capability():
    """The surface is total, so no tool can be invisible to it."""
    surface = capability_surface()
    names = [tool for tools in surface.values() for tool in tools]
    assert sorted(names) == sorted(spec.name for spec in typed_tool_specs())
    assert len(names) == len(set(names))


def test_every_strategy_inherits_from_the_one_surface():
    """No strategy declares a tool list; each withholds capabilities only."""
    surface = capability_surface()
    for strategy in STRATEGY_WITHHELD:
        receipt = capability_receipt(strategy)
        granted = set(receipt["capabilities"])
        withheld = set(receipt["withheld"])
        assert granted | withheld == set(surface), strategy
        assert not granted & withheld, strategy
        for capability in withheld:
            assert receipt["withheld"][capability].strip(), (
                f"{strategy} withheld {capability} without stating why"
            )


def test_a_tool_added_to_the_catalog_is_reachable_without_a_strategy_remembering_it():
    """The surface is DERIVED from the one catalog, so it cannot go stale.

    The probe carries an effect class this module has never seen. It must land
    in a capability that no strategy withholds, and therefore be granted
    everywhere: an unfamiliar capability is reported, never silently dropped.
    """
    from harness.agent_kernel import kernel as kernel_module

    class _Probe:
        name = "agt01_probe_tool"
        side_effect_class = "agt01_unseen_effect"
        aliases = ()

    original = kernel_module.builtin_tool_specs
    kernel_module.builtin_tool_specs = lambda: (*original(), _Probe())
    try:
        surface = kernel_module.capability_surface()
        assert UNCLASSIFIED_CAPABILITY in surface
        assert "agt01_probe_tool" in surface[UNCLASSIFIED_CAPABILITY]
        for strategy in STRATEGY_WITHHELD:
            granted = kernel_module.capability_receipt(strategy)["allowed_tools"]
            assert "agt01_probe_tool" in granted, strategy
    finally:
        kernel_module.builtin_tool_specs = original


def test_daily_inherits_the_whole_surface_so_the_widest_run_is_unchanged():
    """The default strategy keeps every tool, and adds no model-facing line."""
    receipt = capability_receipt("daily")
    assert receipt["withheld"] == {}
    assert (
        receipt["allowed_tool_count"]
        == receipt["surface_tool_count"]
        == len(typed_tool_specs())
    )
    assert render_capability_note(receipt) == ""


# ---------------------------------------------------------------------------
# 2. question / research / planning can reach retrieval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", READ_ONLY_STRATEGIES)
def test_question_and_planning_can_reach_web_fetch_and_web_search(strategy):
    allowed = _allowed_tools(strategy)
    for tool in RETRIEVAL_TOOLS:
        assert tool in allowed, f"{strategy} cannot reach {tool}"
    receipt = capability_receipt(strategy)
    assert "network" in receipt["capabilities"]
    assert "network" not in receipt["withheld"]


@pytest.mark.parametrize("strategy", READ_ONLY_STRATEGIES)
def test_retrieval_is_advertised_to_the_model_on_a_read_only_run(tmp_path, strategy):
    """Reachable means ADVERTISED: the schemas are what the model can see."""
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})])
    _kernel(repo, tmp_path, model).run(
        _spec(repo, f"schemas-{strategy}", request=FAILED_SENTENCE), strategy=strategy
    )
    assert set(RETRIEVAL_TOOLS) <= model.advertised_names()


def test_the_failed_sentence_reaches_real_retrieval_through_a_real_dispatch(
    tmp_path, monkeypatch
):
    """The exact sentence from the defect report, end to end.

    A scripted model asks for a web search on the question strategy. The call
    is validated, policy-checked, dispatched to the installed handler, and
    returns retrieved text - not a protocol error and not ``no_runtime``, which
    is what the old 9-tool surface produced.
    """
    repo = _repo(tmp_path)
    captured = _install_fake_fetch(monkeypatch)
    model = ScriptedModel(
        [
            json.dumps({"tool": "web_search", "query": "best mouse under 1000 rupees"}),
            json.dumps(
                {
                    "tool": "finish",
                    "answer": "A Logitech B100 wired mouse costs about Rs 450.",
                }
            ),
        ]
    )
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-mouse", request=FAILED_SENTENCE), strategy="question"
    )

    assert result.status == CompletionStatus.COMPLETED_UNVERIFIED
    assert _events(tmp_path, "agt01-mouse", "tool_validation_error") == []
    results = _events(tmp_path, "agt01-mouse", "tool_result")
    search = next(row for row in results if row.payload["tool"] == "web_search")
    assert search.payload["ok"] is True
    assert "Logitech B100" in search.payload["output"]
    assert "duckduckgo.com" in captured["url"]
    # The real reader's untrusted-content boundary still ran: the rendered text
    # carries the taint banner rather than speaking in the harness's voice.
    assert "untrusted" in search.payload["output"].lower()


def test_web_fetch_is_dispatchable_on_a_question_run_with_no_execution_backend(
    tmp_path, monkeypatch
):
    """``web_fetch`` must not depend on the mutation backend existing.

    The kernel only builds its safe execution backend for ``daily``, so a
    read-only strategy has no backend at all. Retrieval therefore goes through
    ``harness.webfetch`` on the host, under the shared egress and SSRF guards.
    """
    repo = _repo(tmp_path)
    _install_fake_fetch(monkeypatch, text="A documentation page about mice.")
    model = ScriptedModel(
        [
            json.dumps(
                {"tool": "web_fetch", "url": "https://pypi.org/project/logitech/"}
            ),
            json.dumps({"tool": "finish", "answer": "read the page"}),
        ]
    )
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-fetch", request="what does the logitech page say?"),
        strategy="question",
    )
    assert result.status == CompletionStatus.COMPLETED_UNVERIFIED
    results = _events(tmp_path, "agt01-fetch", "tool_result")
    fetched = next(row for row in results if row.payload["tool"] == "web_fetch")
    assert fetched.payload["ok"] is True
    assert "mice" in fetched.payload["output"]


def test_a_denied_egress_is_reported_rather_than_being_silently_empty(
    tmp_path, monkeypatch
):
    """An empty allowlist is the honest OFF arm, and it says so."""
    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps({"tool": "web_fetch", "url": "https://example.com/mice"}),
            json.dumps({"tool": "finish", "answer": "I could not check."}),
        ]
    )
    result = _kernel(repo, tmp_path, model, webfetch_allowed_hosts=[]).run(
        _spec(repo, "agt01-denied", request=FAILED_SENTENCE), strategy="question"
    )
    assert result.status == CompletionStatus.COMPLETED_UNVERIFIED
    results = _events(tmp_path, "agt01-denied", "tool_result")
    fetched = next(row for row in results if row.payload["tool"] == "web_fetch")
    assert fetched.payload["ok"] is False
    assert "webfetch_allowed_hosts" in fetched.payload["output"]


def test_a_disabled_retrieval_capability_reports_that_it_was_never_attempted(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    model = ScriptedModel(
        [
            json.dumps({"tool": "web_search", "query": "best mouse"}),
            json.dumps({"tool": "finish", "answer": "I could not check."}),
        ]
    )
    _kernel(repo, tmp_path, model, web_fetch_enabled=False).run(
        _spec(repo, "agt01-off", request=FAILED_SENTENCE), strategy="question"
    )
    results = _events(tmp_path, "agt01-off", "tool_result")
    searched = next(row for row in results if row.payload["tool"] == "web_search")
    assert searched.payload["ok"] is False
    assert "web_fetch_enabled" in searched.payload["output"]


# ---------------------------------------------------------------------------
# 3. withheld capabilities are reported
# ---------------------------------------------------------------------------


def test_withheld_capabilities_are_reported_with_a_reason():
    receipt = capability_receipt("question")
    assert set(receipt["withheld"]) == {"mutate", "shell", "subagent"}
    for capability, reason in receipt["withheld"].items():
        assert reason.strip(), capability
    assert "edit" in receipt["withheld_tools"]
    assert "shell" in receipt["withheld_tools"]


def test_a_run_receipt_names_the_effective_surface(tmp_path):
    """The journal row AND the result both name what this run could do."""
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})])
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-receipt", request=FAILED_SENTENCE), strategy="question"
    )

    rows = _events(tmp_path, "agt01-receipt", "capability_surface")
    assert len(rows) == 1
    journalled = rows[0].payload
    on_result = result.metadata["capability_surface"]
    assert journalled == on_result
    assert on_result["strategy"] == "question"
    assert on_result["allowed_tool_count"] < on_result["surface_tool_count"]
    assert set(RETRIEVAL_TOOLS) <= set(on_result["allowed_tools"])
    assert on_result["withheld"]

    # The receipt is emitted BEFORE the strategy runs, next to the strategy
    # that selected it, so "it did not search" is answerable from the trace
    # alone even for a run that crashed afterwards.
    order = [
        event.event_type
        for event in RunEventJournal(
            tmp_path / "logs" / "agt01-receipt" / "trace.jsonl"
        ).read_events()
    ]
    assert order.index("strategy_selected") < order.index("capability_surface")
    assert order.index("capability_surface") < order.index("run_finished")


def test_the_capability_receipt_cannot_claim_anything_was_verified(tmp_path):
    """A capability receipt must not be able to imply a completion.

    Same discipline as the trust receipt: the serialized receipt carries no
    completion vocabulary at all, so no consumer can read "it had tools" as
    "it did the work".
    """
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})])
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-honest"), strategy="question"
    )
    assert result.status == CompletionStatus.COMPLETED_UNVERIFIED
    blob = json.dumps(result.metadata["capability_surface"]).lower()
    for word in ("success", "verified", "completed", "passed"):
        assert word not in blob, word


def test_the_model_is_told_what_this_run_may_not_do(tmp_path):
    """The narrowing is visible to the model, not only to the receipt."""
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})])
    _kernel(repo, tmp_path, model).run(_spec(repo, "agt01-note"), strategy="question")
    sent = json.dumps(model.messages[0])
    assert "may NOT do" in sent
    assert "mutate" in sent and "shell" in sent


def test_an_operator_narrowing_can_only_subtract_and_is_reported(tmp_path):
    repo = _repo(tmp_path)
    model = ScriptedModel([json.dumps({"tool": "finish", "answer": "done"})])
    result = _kernel(
        repo,
        tmp_path,
        model,
        capability_withheld={"network": "operator has no egress for this run"},
    ).run(_spec(repo, "agt01-operator", request=FAILED_SENTENCE), strategy="question")

    receipt = result.metadata["capability_surface"]
    assert "network" in receipt["withheld"]
    assert "operator has no egress" in receipt["withheld"]["network"]
    assert "web_search" in receipt["withheld_tools"]
    assert "web_search" not in receipt["allowed_tools"]
    # A tool name narrows exactly that tool's capability, and where the
    # strategy already withheld that capability the strategy's own reason is
    # preserved beside the operator's rather than replaced by it.
    by_tool = capability_receipt(
        "question",
        {"capability_withheld": [{"tool": "search", "reason": "no search"}]},
    )
    assert "network" in by_tool["withheld"]
    assert "no search" in by_tool["withheld"]["network"]
    planning = capability_receipt("planning", {"capability_withheld": ["mutate"]})
    assert "the strategy already withheld it" in planning["withheld"]["mutate"]
    assert "may not change the repository" in planning["withheld"]["mutate"]


def test_an_operator_narrowing_that_names_nothing_is_reported_not_ignored():
    """A typo in a narrowing key must not read as "nothing was withheld"."""
    receipt = capability_receipt("daily", {"capability_withheld": ["nonsense"]})
    assert any(key.startswith("unknown:") for key in receipt["withheld"])
    assert receipt["allowed_tool_count"] == receipt["surface_tool_count"]


# ---------------------------------------------------------------------------
# 4. question still cannot mutate or shell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", MUTATING_TOOLS + COMMAND_TOOLS)
def test_question_still_cannot_mutate_or_shell_in_the_surface(tool):
    assert tool not in _allowed_tools("question"), tool


def test_question_cannot_mutate_the_repository_end_to_end(tmp_path):
    """A real run, a real refusal, a byte-identical file afterwards."""
    repo = _repo(tmp_path)
    before = (repo / "app.py").read_bytes()
    model = ScriptedModel(
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "app.py",
                    "old_string": "1",
                    "new_string": "2",
                }
            ),
            json.dumps({"tool": "finish", "answer": "read-only answer"}),
        ]
    )
    result = _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-readonly"), strategy="question"
    )
    assert result.status == CompletionStatus.COMPLETED_UNVERIFIED
    assert (repo / "app.py").read_bytes() == before
    refusals = _refusals(tmp_path, "agt01-readonly", "edit")
    assert refusals, "the mutation call must be refused, not silently dropped"
    results = _events(tmp_path, "agt01-readonly", "tool_result")
    assert not [row for row in results if row.payload["tool"] == "edit"]


def test_question_cannot_shell_end_to_end(tmp_path):
    repo = _repo(tmp_path)
    before = (repo / "app.py").read_bytes()
    model = ScriptedModel(
        [
            json.dumps({"tool": "shell", "command": "echo pwned > pwned.txt"}),
            json.dumps({"tool": "finish", "answer": "read-only answer"}),
        ]
    )
    _kernel(repo, tmp_path, model).run(
        _spec(repo, "agt01-noshell"), strategy="question"
    )
    assert (repo / "app.py").read_bytes() == before
    assert not (repo / "pwned.txt").exists()
    assert _refusals(tmp_path, "agt01-noshell", "shell")
    results = _events(tmp_path, "agt01-noshell", "tool_result")
    assert not [row for row in results if row.payload["tool"] == "shell"]


# ---------------------------------------------------------------------------
# the handlers the surface promises are actually installed
# ---------------------------------------------------------------------------


def test_every_granted_capability_has_a_working_retrieval_handler():
    """A capability in the receipt must be callable, not merely listed."""
    registry = ToolRegistry()
    registry.restrict(_allowed_tools("question"))
    build_default_handlers(registry, repo_path=".", config={})
    for tool in RETRIEVAL_TOOLS:
        assert registry.handler_for(tool) is not None, tool
        # The alias spelling resolves to the same installed handler.
        for spec in typed_tool_specs():
            if spec.name == tool:
                for alias in spec.aliases:
                    assert registry.handler_for(alias) is not None, alias


def test_the_catalog_alias_resolution_still_drives_the_surface():
    """``_canonical_tool`` is what makes the surface rename-proof."""
    assert _canonical_tool("fetch") == "web_fetch"
    assert _canonical_tool("search") == "web_search"
    assert _canonical_tool("question") == "ask"
    assert "ask" in _allowed_tools("question")
    assert "finish" in _allowed_tools("question")
