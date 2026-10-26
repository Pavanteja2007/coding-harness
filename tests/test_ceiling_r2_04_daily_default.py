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
# 2. `legacy_agent` IS GONE, and staying gone is pinned.
# --------------------------------------------------------------------------
#
# REWRITTEN 2026-10-03 (P2/W2, T5). This section used to hold three tests
# asserting the OPPOSITE: that an explicit `legacy_agent` still ran the legacy
# path, that `run_agent_legacy` was "the documented way to ask", and that the
# compatibility entry point overrode a configured strategy.
#
# `harness/agent_kernel/legacy.py` is DELETED — the one deleted file in the
# Phase 2 diff — and `run_agent_legacy` no longer exists. Those tests could not
# be "repointed at `agent_step`": they assert a contract that has been
# deliberately withdrawn, so keeping them would keep pins for a feature nobody
# wants back, and deleting them silently would lose the coverage entirely.
#
# The honest successor is an INVERTED pin: the tests now assert the ABSENCE,
# and they FAIL the day somebody reinstates the engine. That is the direction
# this repository's inverted pins exist for, and it is why the suite can keep
# the section at all.


class TestTheLegacyEngineStaysGone:
    """Every one of these FAILS if `legacy_agent` is reinstated."""

    def test_the_strategy_is_not_registered(self):
        from harness.agent_kernel.kernel import STRATEGY_NAMES

        assert "legacy_agent" not in STRATEGY_NAMES, (
            "`legacy_agent` is back in STRATEGY_NAMES. Phase 2 deleted it; if "
            "that was deliberate, delete this pin in the same change and "
            "record why in INTERFACES.md."
        )
        assert "agent_step" in STRATEGY_NAMES, (
            "the single general-agent engine is missing from STRATEGY_NAMES"
        )

    def test_the_strategy_module_is_deleted(self):
        import importlib.util

        assert importlib.util.find_spec("harness.agent_kernel.legacy") is None, (
            "harness/agent_kernel/legacy.py exists again. It is the file Phase 2 "
            "deleted to collapse three engines into one."
        )

    def test_the_compatibility_entry_point_is_not_importable(self):
        with pytest.raises(ImportError):
            from harness.agent_loop import run_agent_legacy  # noqa: F401

    def test_an_explicit_legacy_agent_now_REFUSES_rather_than_running(self):
        """The old spelling is a hard error, not a silent fallback.

        A config still naming `legacy_agent` must get a refusal it can act on.
        A resolver that quietly substituted `daily` would report a run as
        healthy while executing a different engine than the operator asked for
        — which is the exact class of lie this repository exists to prevent.
        """
        with pytest.raises(ValueError) as excinfo:
            resolve_agent_dispatch({"agent_strategy": "legacy_agent"})
        assert "legacy_agent" in str(excinfo.value)

    def test_the_compat_default_key_cannot_resurrect_it_either(self):
        """The compatibility escape hatch is closed, not merely unused.

        `agent_default_strategy` was documented as "the old engine is one key
        away". That sentence is now false, so the key must REFUSE the dead name
        rather than resolve it -- otherwise a stale user config silently gets a
        different engine than it names.
        """
        with pytest.raises(ValueError) as excinfo:
            resolve_agent_dispatch({AGENT_DEFAULT_STRATEGY_KEY: "legacy_agent"})
        assert "legacy_agent" in str(excinfo.value)

    def test_the_compat_default_key_still_names_a_live_engine(self):
        """A closed hatch is still a hatch: the key is not inert."""
        live = [
            name
            for name in ("daily", "verified_fix", "planning", "question", "research", "agent_step")
            if resolve_agent_dispatch({AGENT_DEFAULT_STRATEGY_KEY: name})[1] == name
        ]
        assert live, (
            "`agent_default_strategy` can no longer select ANY registered "
            "engine. The key should be retired with a Change Log entry, not "
            "left accepting input it cannot honour."
        )

    def test_no_production_source_still_reaches_the_stub_sandbox(self):
        """The unsandboxed-bash defect must not return through this path."""
        import ast

        source = (ROOT / "harness" / "agent_loop.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        reaches_stub = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("_stubs")
        ]
        assert not reaches_stub, (
            "harness/agent_loop.py imports a stub again. Every `_stubs` "
            "occurrence in that file should be prose; an import is the "
            "unsandboxed-bash defect returning."
        )


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
    """REWRITTEN 2026-10-03 (P2/W2, T5) — the restore target is GONE.

    This test used to read "An existing user is not broken silently: the old
    engine is one key away" and drive a real `legacy_agent` run. Phase 2
    deleted that engine, so there is nothing to restore and the promise the
    test made cannot be kept.

    What replaces it is the honest version of the same concern: a user whose
    config still names `legacy_agent` must be **refused a sentence they can
    act on**, not silently given a different engine. `TestTheLegacyEngineStaysGone`
    pins the refusal; this test pins the reason string is not a bare
    traceback.
    """
    with pytest.raises(ValueError) as excinfo:
        resolve_agent_dispatch({AGENT_DEFAULT_STRATEGY_KEY: "legacy_agent"})
    message = str(excinfo.value)
    assert message.strip(), "the refusal must carry a reason"
    assert "legacy_agent" in message
    # It must not be an AttributeError-shaped traceback fragment.
    assert "Traceback" not in message


def test_compat_default_does_not_shadow_an_explicit_agent_strategy():
    """`agent_strategy` is still the caller's own decision and wins.

    A key whose name says "default" must never be able to overrule an actual
    choice -- the compatibility escape hatch exists for callers who named
    NOTHING.
    """
    assert resolve_agent_dispatch(
        {AGENT_DEFAULT_STRATEGY_KEY: "daily", "agent_strategy": "question"}
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
        ("r204-journal-question", {"agent_strategy": "question"}, "question"),
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
    """REWRITTEN 2026-10-03 (P2/W2, T5) — pinned on the SOURCE, not a driver.

    Two things changed and both are worth stating rather than working around.

    **One engine is gone.** This test used to prove the *compatibility adapter*
    could not launder an unverified run into `success`. That adapter is
    deleted, so the property has one engine to hold on, and the twin test
    above now carries it.

    **And the surviving engine will not take a legacy-shaped scripted reply.**
    The second arm of this rewrite drove `agent_step` with a `{"tool": "done"}`
    reply and measured `status == "failed"` — because `agent_step` speaks the
    TYPED tool catalog, not the legacy `{"tool": ...}` JSON protocol. Inventing
    a protocol for it here would be writing `harness/`'s test from the wrong
    side of the boundary, so the arm is not attempted.

    What is pinned instead is the thing that actually produces the lie: the
    status MAPPING. `success` is reachable only behind
    `completed_verified`, and that is a source-level fact about one dict in
    `harness/core.py`, checkable without guessing a reply format.
    """
    from shared.agent_contracts import RUN_STATUSES

    # The vocabulary contains no bare "the run worked" word, so a dressed
    # status has nothing to dress itself as.
    assert "success" not in RUN_STATUSES
    assert {"completed_verified", "completed_unverified"} <= set(RUN_STATUSES)

    import ast

    core = (ROOT / "harness" / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(core)

    # (1) The kernel-status -> TaskResult-status mapping.
    mappings = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and "COMPLETED_VERIFIED" in ast.dump(node)
        and any(
            isinstance(v, ast.Constant) and v.value == "success"
            for v in node.values
        )
    ]
    assert mappings, (
        "harness/core.py no longer maps COMPLETED_VERIFIED to `success`. The "
        "mapping is how a kernel status becomes TaskResult.status; if it moved, "
        "re-read where `success` can now come from before trusting this pin."
    )

    # (2) Every site that passes "success" as a STATUS. A structural scan over
    # dict literals is not enough and was wrong first: `{"status": "success"}`
    # at core.py:714 is a TRACE ROW, not a status producer, and a naive scan
    # reads it as one. The status producers are the `_result(...)` calls.
    #
    # Named rather than proved, because proving dominance means proving each
    # site's verifier condition, which is `harness/`'s business and not this
    # file's. An inventory a reader can check is the honest version.
    producers = []
    vocabulary = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = ast.dump(node.func)
        if "id='_result'" not in func and "id='run_task'" not in func:
            continue
        for arg in list(node.args)[1:3]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                vocabulary.add(arg.value)
                if arg.value == "success":
                    producers.append(node.lineno)
    assert producers, (
        "no `_result(task, \"success\", ...)` call site remains in "
        "harness/core.py. Either the completion path moved or it was "
        "rewired; re-read before trusting this pin."
    )
    assert "success" in vocabulary
    others = {
        v for v in vocabulary if v not in {"success", "failed", "error", "timeout"}
    }
    assert not others, (
        f"_result is called with unexpected status vocabularies: {others}. An "
        f"unknown status reaching TaskResult.status is the vocabulary-drift "
        f"failure this repository records four separate times."
    )


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


def test_a_default_run_writes_the_daily_only_artifacts(tmp_path):
    """`daily`-only machinery is reachable on a default run.

    REWRITTEN 2026-10-03 (P2/W2, T5). This test used to be a DIFFERENTIAL:
    it ran the same fixture twice, once on `daily` and once on `legacy_agent`,
    and asserted the three daily-only artifacts appeared in one run directory
    and not the other. That differential was the measurement — it proved the
    artifacts came from the ENGINE and not from the fixture.

    The second arm is gone with the engine, and a differential with one arm is
    not a differential: asserting "daily writes conversation.jsonl" passes on a
    tree where EVERY engine writes it. So the surviving property is stated as
    the positive claim plus the event that is daily-only, which is what the
    differential was actually detecting.

    The lost half — "a non-daily engine does not write these" — cannot be
    re-measured while there is one engine, and is recorded here rather than
    quietly dropped.
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
    daily = tmp_path / "logs" / "r204-artifacts-daily"
    for name in ("conversation.jsonl", "turns.jsonl", "context.json"):
        assert (daily / name).is_file(), f"default run must write {name}"
    kinds = [
        str(row.get("event") or row.get("kind"))
        for row in _journal_rows(tmp_path / "logs", "r204-artifacts-daily")
    ]
    assert "context_budget" in kinds, (
        "the context budget is daily-only and now reachable"
    )
    # The control arm: at least one kind IS daily-only, or the assertion above
    # proves nothing about provenance.
    assert set(kinds) - {"context_budget"}, (
        "the run emitted ONLY context_budget, so its presence cannot be "
        "attributed to the engine rather than to the fixture"
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
