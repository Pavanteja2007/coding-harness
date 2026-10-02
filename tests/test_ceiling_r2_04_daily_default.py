"""R2-04 — `daily` is the real default engine for `harness.agent_loop.run_agent`.

**The absence of a strategy is now absence.** `run_agent` used to hand the
kernel `explicit="legacy_agent"` whenever the caller named no strategy, which
made the kernel resolver's own `daily` default unreachable and contradicted
`run_agent`'s own docstring 25 lines below the call. These tests pin the
fixed contract in both directions and, more importantly, MEASURE what the
switch actually exposes — the daily-only artifacts a default run now writes
and the compatibility run does not — so the claim cannot be satisfied by a
comment.

Everything here is host-only: a scripted model double, a real kernel, a real
event journal, no Docker, no network, no live provider. No model-quality
claim is made or implied.

Naming: every test is named after the behaviour it pins, not after the line
it exercises.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import deps  # noqa: E402
from harness.agent_loop import (  # noqa: E402
    AGENT_DEFAULT_STRATEGY_KEY,
    resolve_agent_dispatch,
    run_agent,
    run_agent_legacy,
)
from harness.config import DEFAULTS, get_config  # noqa: E402


class Scripted:
    """Queue-driven fake model: one reply per call, no network."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, messages, **kwargs):
        self.prompts.append(
            "\n".join(str(item.get("content", "")) for item in messages)
        )
        assert self.replies, "model called more times than scripted"
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_last_usage(self):
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    return repo


def _run(repo, tmp_path, replies, config=None, task_id="r204-1", runner=run_agent):
    """Drive one real ``run_agent`` call with a scripted model."""
    deps.set_call_model(Scripted(replies))
    try:
        return runner(
            "make the small change",
            str(repo),
            config={"steering_enabled": False, **(config or {})},
            log_root=tmp_path / "logs",
            task_id=task_id,
        )
    finally:
        deps.reset_overrides()


def _journal_rows(log_root, task_id):
    path = Path(log_root) / task_id / "trace.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _payload(row):
    return (
        row.get("data")
        if isinstance(row.get("data"), dict)
        else row.get("payload") or {}
    )


# --------------------------------------------------------------------------
# 1. A default daily run resolves to `daily` with source `default`.
# --------------------------------------------------------------------------


def test_default_run_resolves_to_daily_with_source_default(tmp_path):
    """No strategy named anywhere -> `daily`, and the source says `default`."""
    repo = _repo(tmp_path)
    out = _run(repo, tmp_path, [json.dumps({"tool": "done", "answer": "done"})])
    assert out["agent_strategy"] == "daily"
    assert out["agent_strategy_source"] == "default"
    assert out["agent_strategy_resolver"] == "harness.agent_loop.resolve_agent_dispatch"


def test_absent_strategy_is_dispatched_as_absence_not_a_default_name():
    """The absence is passed through as `None`; the kernel resolver decides."""
    dispatch, name, source = resolve_agent_dispatch({})
    assert dispatch is None, (
        "run_agent must not hand the kernel a name nobody asked for"
    )
    assert (name, source) == ("daily", "default")


def test_absence_beats_a_config_dict_that_merges_defaults(tmp_path):
    """`get_config({})` (what a real task always produces) still means daily."""
    dispatch, name, source = resolve_agent_dispatch(get_config({}))
    assert dispatch is None
    assert (name, source) == ("daily", "default")


def test_unknown_strategy_name_still_fails_closed():
    """Flipping the default must not turn the resolver's fail-closed into a fallback."""
    with pytest.raises(ValueError):
        resolve_agent_dispatch({"agent_strategy": "not-a-strategy"})
    with pytest.raises(ValueError):
        resolve_agent_dispatch({AGENT_DEFAULT_STRATEGY_KEY: "not-a-strategy"})


# --------------------------------------------------------------------------
# 2. Explicit `legacy_agent` still runs the legacy path.
# --------------------------------------------------------------------------


def test_explicit_legacy_agent_still_runs_the_legacy_path(tmp_path):
    """The compatibility surface is fully working and reachable BY NAME."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "src/app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                }
            ),
            json.dumps({"tool": "done", "answer": "legacy edit done"}),
        ],
        config={"agent_strategy": "legacy_agent"},
        task_id="r204-legacy-explicit",
    )
    assert out["agent_strategy"] == "legacy_agent"
    assert out["agent_strategy_source"] == "config"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert "legacy edit done" in out["answer"]


def test_dedicated_compatibility_entry_point_forces_the_legacy_engine(tmp_path):
    """`run_agent_legacy` is the documented way to ask, with no config edit."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [
            json.dumps(
                {
                    "tool": "edit",
                    "path": "src/app.py",
                    "old_string": "value = 1",
                    "new_string": "value = 3",
                }
            ),
            json.dumps({"tool": "done", "answer": "compat entry point"}),
        ],
        task_id="r204-legacy-entry",
        runner=run_agent_legacy,
    )
    assert out["agent_strategy"] == "legacy_agent"
    assert out["agent_strategy_source"] == "config"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 3\n"


def test_compatibility_entry_point_overrides_a_configured_strategy(tmp_path):
    """An explicit request for the compatibility engine is not second-guessed."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [json.dumps({"tool": "done", "answer": "compat wins"})],
        config={"agent_strategy": "research"},
        task_id="r204-legacy-override",
        runner=run_agent_legacy,
    )
    assert out["agent_strategy"] == "legacy_agent"


# --------------------------------------------------------------------------
# 3. The compatibility default config key, and the release-note contract.
# --------------------------------------------------------------------------


def test_compat_default_key_is_a_no_op_when_absent_or_none():
    """Every "off" spelling means off, so a config chain can disable the key."""
    for value in (
        {},
        {AGENT_DEFAULT_STRATEGY_KEY: None},
        {AGENT_DEFAULT_STRATEGY_KEY: ""},
        {AGENT_DEFAULT_STRATEGY_KEY: "none"},
        {AGENT_DEFAULT_STRATEGY_KEY: "default"},
    ):
        assert resolve_agent_dispatch(value) == (None, "daily", "default")


def test_compat_default_key_in_defaults_is_behaviour_neutral():
    """The `DEFAULTS` entry is `None`, so adding it switched no task silently."""
    assert AGENT_DEFAULT_STRATEGY_KEY in DEFAULTS
    assert DEFAULTS[AGENT_DEFAULT_STRATEGY_KEY] is None
    assert resolve_agent_dispatch(get_config({}))[2] == "default"


def test_compat_default_key_restores_the_previous_default_engine(tmp_path):
    """An existing user is not broken silently: the old engine is one key away."""
    assert resolve_agent_dispatch({AGENT_DEFAULT_STRATEGY_KEY: "legacy_agent"}) == (
        "legacy_agent",
        "legacy_agent",
        "compat_default",
    )
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [json.dumps({"tool": "done", "answer": "compat default"})],
        config={AGENT_DEFAULT_STRATEGY_KEY: "legacy_agent"},
        task_id="r204-compat-default",
    )
    assert out["agent_strategy"] == "legacy_agent"
    assert out["agent_strategy_source"] == "compat_default"


def test_compat_default_does_not_shadow_an_explicit_agent_strategy():
    """`agent_strategy` is still the caller's own decision and wins.

    A key whose name says "default" must never be able to overrule an actual
    choice -- the compatibility escape hatch exists for callers who named
    NOTHING.
    """
    assert resolve_agent_dispatch(
        {AGENT_DEFAULT_STRATEGY_KEY: "legacy_agent", "agent_strategy": "question"}
    ) == (None, "question", "config")
    assert resolve_agent_dispatch({"agent_strategy": "question"}) == (
        None,
        "question",
        "config",
    )


# --------------------------------------------------------------------------
# 4. The journal names the engine, and the docstring contradiction is gone.
# --------------------------------------------------------------------------


def test_journal_names_the_engine_and_a_source_for_every_resolution(tmp_path):
    """`run_started` and `strategy_selected` both state the engine, per run."""
    repo = _repo(tmp_path)
    for task_id, config, expected in (
        ("r204-journal-default", {}, "daily"),
        ("r204-journal-legacy", {"agent_strategy": "legacy_agent"}, "legacy_agent"),
    ):
        _run(
            repo,
            tmp_path,
            [json.dumps({"tool": "done", "answer": "journal"})],
            config=config,
            task_id=task_id,
        )
        rows = _journal_rows(tmp_path / "logs", task_id)
        started = [
            r for r in rows if (r.get("event") or r.get("kind")) == "run_started"
        ]
        selected = [
            r for r in rows if (r.get("event") or r.get("kind")) == "strategy_selected"
        ]
        assert len(started) == 1 and len(selected) == 1
        assert _payload(started[0])["strategy"] == expected
        assert str(_payload(started[0])["strategy_source"]).strip()
        assert _payload(selected[0])["strategy"] == expected
        assert str(_payload(selected[0])["source"]).strip()


def test_run_agent_does_not_override_the_kernel_resolver():
    """The exact regression this prompt names, pinned on the source itself."""
    source = inspect.getsource(run_agent)
    assert 'explicit="legacy_agent"' not in source
    assert "explicit=str(requested)" not in source
    assert "resolve_agent_dispatch" in source
    # The kernel is the authority, and the docstring now says so without
    # contradicting the call below it.
    doc = (run_agent.__doc__ or "").lower()
    assert "single authority" in doc
    assert "stays the default here" not in doc


def test_the_dispatch_decision_lives_in_exactly_one_function():
    """One place decides; nothing else in the module re-derives the precedence."""
    module_source = inspect.getsource(sys.modules["harness.agent_loop"])
    callers = [
        line
        for line in module_source.splitlines()
        if "resolve_agent_dispatch(" in line and not line.strip().startswith("#")
    ]
    # one `def` line is the definition; the rest must be the single call site
    # inside run_agent.
    assert len([line for line in callers if line.strip().startswith("def ")]) == 1
    assert len(callers) == 2


# --------------------------------------------------------------------------
# 5. `completed_unverified` is never reported as success.
# --------------------------------------------------------------------------


def test_a_default_run_without_a_verifier_is_never_reported_as_success(tmp_path):
    """The daily path's DONE is a request, not proof — and says so."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [json.dumps({"tool": "done", "answer": "no verifier here"})],
        task_id="r204-unverified",
    )
    assert out["status"] == "completed_unverified"
    assert out["status"] != "success"
    assert out["kernel_status"] == "completed_unverified"
    assert "verification" not in out


def test_a_legacy_run_without_a_verifier_is_never_reported_as_success(tmp_path):
    """The compatibility adapter must not launder unverified into `success`."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [json.dumps({"tool": "done", "answer": "no verifier here"})],
        config={"agent_strategy": "legacy_agent"},
        task_id="r204-unverified-legacy",
    )
    assert out["status"] == "completed_unverified"
    assert out["status"] != "success"
    assert "verification" not in out


def test_every_daily_driver_probe_guards_the_honest_status():
    """The eval's own completion predicate fails closed on a dressed success."""
    from evals.daily_driver import _honest_completion

    assert _honest_completion({"status": "completed_unverified"}) == (True, False)
    assert _honest_completion(
        {"status": "success", "kernel_status": "completed_verified"}
    ) == (True, False)
    # The lie this project refuses: the `success` word with no verified evidence.
    assert _honest_completion(
        {"status": "success", "kernel_status": "completed_unverified"}
    ) == (True, True)
    assert _honest_completion(
        {"status": "completed_unverified", "kernel_status": "success"}
    ) == (True, True)
    # A non-completion is never completed, and is never a false claim either.
    for status in ("failed", "timeout", "cancelled", "blocked", "", None):
        completed, worded = _honest_completion({"status": status})
        assert completed is False
        assert worded is False


# --------------------------------------------------------------------------
# 6. The MEASURED consequence: what the default switch actually exposes.
# --------------------------------------------------------------------------


def test_a_default_run_writes_the_daily_only_artifacts_a_legacy_run_does_not(tmp_path):
    """`daily`-only machinery is no longer unreachable in practice.

    This is the measurement the prompt's "Consequence" paragraph claims:
    `ConversationMemory`'s journal, the per-turn `TurnLedger`, and the
    context-budget meter are written on a default run and are ABSENT on the
    compatibility run, in the same process, from the same scripted model.
    """
    repo = _repo(tmp_path)
    _run(
        repo,
        tmp_path,
        [
            json.dumps({"tool": "read", "path": "src/app.py"}),
            json.dumps({"tool": "done", "answer": "daily artifacts"}),
        ],
        task_id="r204-artifacts-daily",
    )
    _run(
        repo,
        tmp_path,
        [
            json.dumps({"tool": "read", "path": "src/app.py"}),
            json.dumps({"tool": "done", "answer": "legacy artifacts"}),
        ],
        config={"agent_strategy": "legacy_agent"},
        task_id="r204-artifacts-legacy",
    )
    daily = tmp_path / "logs" / "r204-artifacts-daily"
    legacy = tmp_path / "logs" / "r204-artifacts-legacy"
    for name in ("conversation.jsonl", "turns.jsonl", "context.json"):
        assert (daily / name).is_file(), f"default run must write {name}"
        assert not (legacy / name).exists(), f"legacy run must not claim {name}"
    kinds = [
        str(row.get("event") or row.get("kind"))
        for row in _journal_rows(tmp_path / "logs", "r204-artifacts-daily")
    ]
    assert "context_budget" in kinds, (
        "the context budget is daily-only and now reachable"
    )


def test_the_default_engine_is_reported_consistently_by_result_and_journal(tmp_path):
    """The result and the journal cannot disagree about which engine ran."""
    repo = _repo(tmp_path)
    out = _run(
        repo,
        tmp_path,
        [json.dumps({"tool": "done", "answer": "agreement"})],
        task_id="r204-agreement",
    )
    rows = _journal_rows(tmp_path / "logs", "r204-agreement")
    started = next(
        r for r in rows if (r.get("event") or r.get("kind")) == "run_started"
    )
    assert _payload(started)["strategy"] == out["agent_strategy"]
    assert str(_payload(started)["mode"]) == out["agent_strategy"]
