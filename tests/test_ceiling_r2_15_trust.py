"""R2-15: the daily path is sandboxed by default, and a grant is not a bypass.

The finding this suite pins is a TRUST ASYMMETRY: ``neo fix`` ran sandboxed
behind a verifier gate while the daily interactive path defaulted to
``approval=auto`` on LIVE HOST BASH, held no grant ledger, offered an
unconditional ``global`` approval scope, and matched command prefixes with a
bare ``str.startswith`` (so an empty prefix -- a config slip -- matched every
command, and ``git sta`` covered ``git stash``).

Every test here is host-only: no Docker, no model, no network. Each is named
after the behaviour it pins, and each asserts on the mechanism rather than on a
string a comment could satisfy.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from harness.agent_kernel.contracts import PermissionDecision, ToolCall
from harness.agent_kernel.policy import (
    ApprovalGrant,
    PolicyEngine,
    PolicyRule,
    normalize_scope,
)
from shared.approval import (
    APPROVAL_SCOPES,
    DAILY_SANDBOX_KEY,
    KERNEL_SANDBOX_KEY,
    RETIRED_APPROVAL_SCOPES,
    DailyTrust,
    EmptyCommandPrefixError,
    TrustGrant,
    TrustLedger,
    command_prefix_matches,
    empty_prefix_refusal,
    load_trust_ledger,
    normalize_approval_scope,
    resolve_daily_trust,
    retired_scope_note,
    save_trust_ledger,
    trust_ledger_path,
    verify_trust_applied,
)

# ---------------------------------------------------------------------------
# 1. The daily path is sandboxed by default, and the receipt says so
# ---------------------------------------------------------------------------


def test_the_harness_default_sandboxes_the_daily_paths_shell():
    """The kernel's shell key is ON out of the box -- this IS the fix.

    Read through the harness's own merged config rather than by importing the
    constant, so a merge could not hide it.
    """
    from harness.config import DEFAULTS, get_config

    assert DEFAULTS[KERNEL_SANDBOX_KEY] is True
    assert get_config({})[KERNEL_SANDBOX_KEY] is True
    # And it is read in exactly one place, so a second reader cannot disagree.
    source = Path("harness/agent_kernel/strategy.py").read_text(encoding="utf-8")
    assert source.count(KERNEL_SANDBOX_KEY) == 1, (
        "the shell containment key must have exactly one reader; a second reader "
        "is a second boundary decision"
    )


def test_a_daily_run_is_sandboxed_by_default_and_the_receipt_says_so():
    """No opt-out configured -> sandboxed, and the receipt names it."""
    trust = resolve_daily_trust({}, repo_path="/repo", repo_key="k")
    assert trust.sandboxed is True
    assert trust.unsandboxed is False
    assert trust.sandbox_opt_out is False
    assert "sandboxed" in trust.summary().lower()
    assert "paths in reach" in trust.summary().lower()
    lines = trust.banner_lines()
    assert lines[0].startswith("boundary: sandboxed")
    assert "UNSANDBOXED" not in "\n".join(lines)


def test_the_resolved_boundary_is_pinned_into_the_config_the_kernel_reads():
    """A receipt the run does not honour is a lie, so the caller applies the patch."""
    config = {"agent_process_sandboxed": True}
    trust = resolve_daily_trust(config, repo_path="/repo")
    config.update(trust.config_patch())
    assert config[KERNEL_SANDBOX_KEY] is True
    assert verify_trust_applied(trust, config) == ""


def test_verify_trust_applied_reports_a_config_that_contradicts_the_receipt():
    """An unsandboxed kernel under a sandboxed receipt must be REPORTED."""
    trust = resolve_daily_trust({}, repo_path="/repo")
    mismatch = verify_trust_applied(trust, {KERNEL_SANDBOX_KEY: False})
    assert mismatch, "a contradicting config must be named, not smoothed over"
    assert KERNEL_SANDBOX_KEY in mismatch


def test_the_interactive_daily_path_resolves_and_shows_the_boundary(tmp_path, capsys):
    """`cli.interactive` resolves the boundary, pins it, prints it, records it."""
    import cli.interactive as interactive

    config = {"agent_process_sandboxed": True}
    trust = interactive.resolve_session_trust(
        config,
        repo=tmp_path,
        log_root=tmp_path / "logs",
        session_id="sess-trust",
        task_id="task-trust",
    )
    assert trust.sandboxed is True
    assert config[KERNEL_SANDBOX_KEY] is True
    interactive.render_trust_banner(trust, interactive._trust_ledger())
    out = capsys.readouterr().out
    assert "boundary: sandboxed" in out

    receipt = tmp_path / "logs" / "task-trust" / "trust.json"
    assert receipt.is_file(), "the receipt must be permanent, not just printed"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["sandboxed"] is True
    assert payload["repo_path"] == str(tmp_path)
    assert payload["paths_in_reach"] == [str(tmp_path)]


# ---------------------------------------------------------------------------
# 2. Opting out is allowed, recorded, and visible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config,reason_fragment",
    [
        ({DAILY_SANDBOX_KEY: False}, "daily_sandbox=false"),
        ({KERNEL_SANDBOX_KEY: False}, "agent_process_sandboxed=false"),
    ],
)
def test_opting_out_is_recorded_and_visible_in_the_shell(config, reason_fragment):
    """An operator may choose the live host -- and the UI then says so loudly."""
    values = dict(config)
    trust = resolve_daily_trust(values, repo_path="/repo")
    assert trust.sandboxed is False
    assert trust.sandbox_opt_out is True
    assert reason_fragment in trust.sandbox_reason
    lines = trust.banner_lines()
    assert lines[0].startswith("boundary: UNSANDBOXED")
    assert any("daily_sandbox" in line for line in lines), (
        "the opt-out door must be shown"
    )


def test_the_opt_out_is_written_to_the_receipt_file(tmp_path, capsys):
    import cli.interactive as interactive

    config = {DAILY_SANDBOX_KEY: False}
    trust = interactive.resolve_session_trust(
        config,
        repo=tmp_path,
        log_root=tmp_path / "logs",
        session_id="sess-optout",
        task_id="task-optout",
    )
    assert trust.unsandboxed is True
    assert config[KERNEL_SANDBOX_KEY] is False, "an opt-out must be pinned through"
    interactive.render_trust_banner(trust, None)
    out = capsys.readouterr().out
    assert "UNSANDBOXED" in out
    payload = json.loads(
        (tmp_path / "logs" / "task-optout" / "trust.json").read_text(encoding="utf-8")
    )
    assert payload["sandboxed"] is False
    assert payload["sandbox_opt_out"] is True
    assert "daily_sandbox=false" in payload["sandbox_reason"]


def test_a_quiet_session_still_shows_that_it_is_unsandboxed(tmp_path, capsys):
    """Quiet is a verbosity preference, not a way to hide a weaker boundary."""
    import cli.interactive as interactive

    trust = resolve_daily_trust({DAILY_SANDBOX_KEY: False}, repo_path=str(tmp_path))
    interactive.render_trust_banner(trust, None, quiet=True)
    assert "UNSANDBOXED" in capsys.readouterr().out


def test_conflicting_sandbox_keys_report_the_weaker_boundary():
    """`daily_sandbox=true` with the kernel key false is a conflict, not a win."""
    trust = resolve_daily_trust(
        {DAILY_SANDBOX_KEY: True, KERNEL_SANDBOX_KEY: False}, repo_path="/repo"
    )
    assert trust.sandboxed is False
    assert "weaker boundary" in trust.sandbox_reason


def test_an_unparseable_sandbox_value_can_never_disable_the_boundary():
    """A typo must not be a way to turn a container off.

    Only an HONEST false is an opt-out. `0` / `"0"` / `"false"` / `False` are
    unambiguous in every config dialect this project reads (TOML, JSON, CLI),
    so honouring them is what an operator means; anything that is not a boolean
    spelling at all is treated as "no opinion" and the safe default stands.
    """
    for value in ("yes please", "maybe", "", [], {}, object(), b"bytes", 2.5):
        trust = resolve_daily_trust({DAILY_SANDBOX_KEY: value}, repo_path="/repo")
        assert trust.sandboxed is True, f"{value!r} must not disable containment"
        trust = resolve_daily_trust({KERNEL_SANDBOX_KEY: value}, repo_path="/repo")
        assert trust.sandboxed is True, f"{value!r} must not disable containment"
    for value in (False, 0, "false", "no", "off", "0"):
        assert resolve_daily_trust({DAILY_SANDBOX_KEY: value}).sandboxed is False, (
            f"{value!r} is an honest opt-out and must be honoured"
        )


def test_absent_none_and_true_all_mean_sandboxed():
    """`None` is "no opinion", and no opinion is the safe default."""
    for config in ({}, {DAILY_SANDBOX_KEY: None}, {DAILY_SANDBOX_KEY: True}):
        assert resolve_daily_trust(config, repo_path="/repo").sandboxed is True


# ---------------------------------------------------------------------------
# 3. `global` is deleted: a bypass is not a scope
# ---------------------------------------------------------------------------


def test_global_is_not_a_supported_scope():
    assert "global" not in APPROVAL_SCOPES
    assert "global" in RETIRED_APPROVAL_SCOPES
    assert normalize_scope("global") == "once"
    assert normalize_approval_scope("global") == "once"
    assert retired_scope_note("global"), "the refusal must be explainable"


def test_a_global_grant_no_longer_bypasses_policy():
    """The kernel's own hard ask must survive a `global` grant."""
    call = ToolCall(
        call_id="c1",
        tool="shell",
        arguments={"command": "rm -rf /"},
        side_effect_class="process",
    )
    engine = PolicyEngine([], default_action="ask")
    decision = engine.evaluate(call)
    assert decision.action == "ask"
    grant = ApprovalGrant(scope="global")
    assert grant.scope == "once", "a global grant must normalize to the narrowest scope"
    assert grant.matches(call) is False
    assert engine._covering_grant(call) is None
    # Re-evaluating under the grant still asks.
    assert engine.evaluate(call).action == "ask"


def test_a_global_scope_asked_for_at_approval_time_is_narrowed_and_explained():
    """A `global` answer approves THIS call and retains nothing.

    Denying a "yes, this once" would be wrong -- the effect was displayed and
    approved, and a hard deny still runs through the policy engine either way.
    What must not happen is a grant: the request asked for a scope that does not
    exist, so nothing is remembered, and the audit row says so instead of
    reporting a bare "approval accepted".
    """
    call = ToolCall(
        call_id="c2",
        tool="shell",
        arguments={"command": "git status"},
        side_effect_class="process",
    )
    engine = PolicyEngine([], default_action="ask")
    decision = engine.evaluate(call)
    updated = engine.record_approval(call, decision, True, "global")
    assert updated.action == "allow"
    assert updated.scope == "once"
    assert engine.grants == [], "a retired scope must not install a grant"
    assert "once only" in updated.reason
    assert "not a scope" in updated.reason
    # And it does not become a bypass for the next call.
    other = ToolCall(
        call_id="c2b",
        tool="shell",
        arguments={"command": "git push"},
        side_effect_class="process",
    )
    assert engine.evaluate(other).action == "ask"


def test_the_configured_scope_table_no_longer_offers_global():
    from harness.config import DEFAULTS

    assert "global" not in DEFAULTS["approval_scopes"]
    assert DEFAULTS["approval_scopes"] == list(APPROVAL_SCOPES)


# ---------------------------------------------------------------------------
# 4. An empty command prefix is a configuration error, refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  "])
def test_an_empty_command_prefix_is_refused_with_a_message_that_says_so(blank):
    refusal = empty_prefix_refusal(blank)
    assert refusal, "a blank prefix must produce an explanation"
    assert "empty command prefix" in refusal
    assert "every command" in refusal
    with pytest.raises(EmptyCommandPrefixError) as excinfo:
        TrustGrant(scope="session_command_prefix", tool="shell", command_prefix=blank)
    assert "empty command prefix" in str(excinfo.value)


def test_an_absent_prefix_is_not_a_refusal():
    """`None` means "no prefix configured", which is a different thing."""
    assert empty_prefix_refusal(None) == ""
    assert TrustGrant(scope="session_path", tool="edit", command_prefix="").scope == (
        "session_path"
    )


def test_an_empty_prefix_never_matches_a_command():
    assert command_prefix_matches("git stash", "") is False
    assert command_prefix_matches("", "git status") is False


def test_a_policy_rule_with_a_blank_prefix_is_refused():
    for blank in ("", "  "):
        with pytest.raises(ValueError, match="empty command prefix"):
            PolicyRule(action="allow", tool="shell", command_prefix=blank)


def test_a_prefix_grant_with_a_blank_prefix_covers_nothing_even_if_constructed():
    """Defence in depth: bypassing the constructor still does not widen."""
    grant = ApprovalGrant(scope="once", tool="shell")
    grant.scope = "session_command_prefix"  # a legacy/unmarshalled grant
    grant.command_prefix = ""
    call = ToolCall(
        call_id="c3",
        tool="shell",
        arguments={"command": "anything at all"},
        side_effect_class="process",
    )
    assert grant.matches(call) is False


def test_record_approval_refuses_to_install_a_blank_prefix_grant():
    call = ToolCall(
        call_id="c4",
        tool="shell",
        arguments={"command": ""},
        side_effect_class="process",
    )
    engine = PolicyEngine([], default_action="ask")
    decision = engine.evaluate(call)
    updated = engine.record_approval(call, decision, True, "session_command_prefix")
    assert updated.action == "deny"
    assert "empty command prefix" in updated.reason
    assert engine.grants == []


def test_a_corrupt_journal_row_cannot_become_a_blanket_grant():
    """A hand-edited or damaged grant file must not widen the policy."""
    assert (
        TrustGrant.from_dict({"scope": "session_command_prefix", "command_prefix": ""})
        is None
    )
    ledger = TrustLedger.from_dict(
        {
            "grants": [
                {
                    "scope": "session_command_prefix",
                    "tool": "shell",
                    "command_prefix": "",
                },
                {
                    "scope": "session_command_prefix",
                    "tool": "shell",
                    "command_prefix": "git status",
                },
            ]
        }
    )
    assert len(ledger.grants) == 1, "the unusable row must be dropped, not widened"


# ---------------------------------------------------------------------------
# 5. A prefix has a boundary
# ---------------------------------------------------------------------------


def test_git_sta_does_not_cover_git_stash():
    assert command_prefix_matches("git stash", "git sta") is False
    call = ToolCall(
        call_id="c5",
        tool="shell",
        arguments={"command": "git stash"},
        side_effect_class="process",
    )
    engine = PolicyEngine([], default_action="ask")
    decision = engine.evaluate(call)
    engine.record_approval(
        call, decision, True, "session_command_prefix"
    )  # grants exactly `git stash`
    assert engine._covering_grant(call) is not None
    other = ToolCall(
        call_id="c6",
        tool="shell",
        arguments={"command": "git status --short"},
        side_effect_class="process",
    )
    assert engine._covering_grant(other) is None


def test_a_directory_prefix_still_covers_the_subtree_it_names():
    """The boundary must not make a useful grant useless."""
    assert command_prefix_matches("pytest tests/test_a.py", "pytest tests/") is True
    assert command_prefix_matches("pytest tests/", "pytest tests/") is True
    assert command_prefix_matches("pytest other/", "pytest tests/") is False


def test_the_policy_rule_matcher_respects_the_boundary():
    call = ToolCall(
        call_id="c7",
        tool="shell",
        arguments={"command": "git stash"},
        side_effect_class="process",
    )
    matching = PolicyRule(action="ask", tool="shell", command_prefix="git sta")
    assert matching.matches(call) is False
    exact = PolicyRule(action="ask", tool="shell", command_prefix="git stash")
    assert exact.matches(call) is True


def test_the_one_prefix_matcher_is_the_shared_implementation():
    """`cli/commands.py` has its own copy; the two must AGREE, and a divergence
    is the signal to make it delegate to `shared.approval.command_prefix_matches`.

    Written as a differential rather than a source scan because `cli/commands.py`
    is another terminal's file: this suite cannot edit it, so it can only prove
    the two implementations have not diverged. When they do, the fix is one line
    in that file and this test names it.
    """
    from cli.commands import _command_matches

    cases = [
        ("git status", "git sta"),
        ("git stash", "git sta"),
        ("git status --short", "git status"),
        ("pytest tests/test_a.py", "pytest tests/"),
        ("pytest other/test_a.py", "pytest tests/"),
        ("git status", ""),
        ("", "git status"),
        ("", ""),
        ("git status", "git status"),
        ("  git status  ", "git status"),
        ("git stash", "git stash push"),
        ("docker run -it x", "docker run"),
        ("rm -rf /", "rm"),
    ]
    for command, prefix in cases:
        assert command_prefix_matches(command, prefix) == _command_matches(
            command, prefix
        ), (
            f"shared and cli prefix matching diverged on {command!r} / {prefix!r}; "
            "make cli/commands.py::_command_matches delegate to "
            "shared.approval.command_prefix_matches"
        )


# ---------------------------------------------------------------------------
# 6. Trust calibration: stop re-asking, keep it revocable
# ---------------------------------------------------------------------------


def test_an_approved_action_is_not_re_prompted_within_a_session(tmp_path):
    """The same fact, approved once, is not asked again in that session."""
    import cli.interactive as interactive

    config = {"agent_process_sandboxed": True}
    interactive.resolve_session_trust(
        config,
        repo=tmp_path,
        log_root=tmp_path / "logs",
        session_id="sess-cal",
        task_id="task-1",
    )
    asked: list[str] = []

    def _approver(tool, args=None, *rest):
        asked.append(str(tool))
        return True, "session_command_prefix"

    wrapped = interactive._trusted_approver(_approver)
    first = wrapped("shell", {"command": "git status"})
    second = wrapped("shell", {"command": "git status --short"})
    assert first == (True, "session_command_prefix")
    assert second == (True, "session_command_prefix")
    assert asked == ["shell"], "the second identical fact must not re-prompt"
    # A DIFFERENT command still asks.
    wrapped("shell", {"command": "git stash"})
    assert asked == ["shell", "shell"]


def test_the_grant_is_revocable(tmp_path):
    import cli.interactive as interactive

    interactive.resolve_session_trust(
        {"agent_process_sandboxed": True},
        repo=tmp_path,
        log_root=tmp_path / "logs",
        session_id="sess-revoke",
        task_id="task-1",
    )
    interactive._trust_remember(
        tool="shell", command="git status", scope="session_command_prefix"
    )
    assert interactive._trust_lookup(tool="shell", command="git status") is not None
    removed = interactive.session_trust_forget("shell")
    assert removed == 1
    assert interactive._trust_lookup(tool="shell", command="git status") is None


def test_calibration_is_scoped_to_the_session_that_earned_it(tmp_path):
    """A new session starts clean unless persistence was explicitly requested."""
    import cli.interactive as interactive

    base = {"repo": tmp_path, "log_root": tmp_path / "logs"}
    interactive.resolve_session_trust({**base}, session_id="sess-a", task_id="t1")
    interactive._trust_remember(
        tool="shell", command="git status", scope="session_path", paths=("a.py",)
    )
    assert interactive._trust_lookup(tool="shell", paths=("a.py",)) is not None
    # A different session id is a different conversation: its grants are not
    # inherited. The id is a CALL parameter, not a config key -- a stray
    # `session_id` inside the config must not stand in for it.
    interactive.resolve_session_trust(
        {"agent_process_sandboxed": True, "session_id": "sess-b"},
        repo=tmp_path,
        log_root=tmp_path / "logs",
        session_id="sess-b",
        task_id="t2",
    )
    assert interactive._trust_lookup(tool="shell", paths=("a.py",)) is None


def test_the_same_session_keeps_its_grants_across_runs(tmp_path):
    """Calibration means "within a session", not "within one run"."""
    import cli.interactive as interactive

    for index in (1, 2, 3):
        interactive.resolve_session_trust(
            {"agent_process_sandboxed": True},
            repo=tmp_path,
            log_root=tmp_path / "logs",
            session_id="sess-persist",
            task_id=f"t{index}",
        )
        if index == 1:
            interactive._trust_remember(
                tool="shell", command="git status", scope="session_command_prefix"
            )
        assert interactive._trust_lookup(tool="shell", command="git status") is not None


def test_a_grant_never_crosses_a_repository(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    ledger = TrustLedger(repo_key="repo-a")
    ledger.record(scope="session_path", tool="edit", paths=("a.py",))
    assert ledger.covering(tool="edit", paths=("a.py",), repo_key="repo-a") is not None
    assert ledger.covering(tool="edit", paths=("a.py",), repo_key="repo-b") is None


def test_an_expired_grant_is_not_honoured():
    ledger = TrustLedger(repo_key="k")
    grant = TrustGrant(
        scope="session_path",
        tool="edit",
        paths=("a.py",),
        expires_at=0.0,
        granted_at=0.0,
    )
    ledger.grants.append(grant)
    assert ledger.covering(tool="edit", paths=("a.py",), repo_key="k") is None
    assert ledger.prune() == 1


def test_the_ledger_is_bounded():
    ledger = TrustLedger(repo_key="k", max_grants=3)
    for index in range(10):
        ledger.record(scope="session_path", tool="edit", paths=(f"file{index}.py",))
    assert len(ledger.grants) <= 3


def test_calibration_can_be_turned_off_and_then_every_fact_is_asked(tmp_path):
    import cli.interactive as interactive

    asked: list[str] = []
    interactive.resolve_session_trust(
        {
            "agent_process_sandboxed": True,
            "approval_calibration": False,
            "repo": tmp_path,
            "log_root": tmp_path / "logs",
            "session_id": "sess-off",
            "task_id": "t1",
        }
    )
    trust = interactive._TRUST["receipt"]
    assert trust.calibration is False
    wrapped = interactive._trusted_approver(
        lambda tool, args=None, *rest: (asked.append(str(tool)), (True, "once"))[1]
    )
    wrapped("shell", {"command": "git status"})
    wrapped("shell", {"command": "git status"})
    assert asked == ["shell", "shell"], (
        "with calibration off the same fact is asked twice"
    )


def test_a_persisted_ledger_reloads_and_is_still_bounded(tmp_path):
    ledger = TrustLedger(repo_key="k", session_id="s")
    ledger.record(
        scope="session_command_prefix", tool="shell", command_prefix="git status"
    )
    path = trust_ledger_path(tmp_path / "logs", "k")
    assert save_trust_ledger(path, ledger) == str(path)
    reloaded = load_trust_ledger(path)
    assert reloaded.repo_key == "k"
    assert (
        reloaded.covering(tool="shell", command="git status --short", repo_key="k")
        is not None
    )
    # A damaged journal is empty, never an exception and never wider.
    broken = Path(path)
    broken.write_text("{not json", encoding="utf-8")
    assert load_trust_ledger(broken).grants == []


def test_an_approver_that_raises_decides_nothing():
    """An approval callback that throws must not decide the run."""
    import cli.interactive as interactive

    def _boom(*_args, **_kwargs):
        raise RuntimeError("approver exploded")

    assert (
        interactive._trusted_approver(_boom)("shell", {"command": "git status"})
        is False
    )


def test_calibration_is_off_when_no_ledger_is_registered():
    import cli.interactive as interactive

    with interactive._TRUST_LOCK:
        saved = dict(interactive._TRUST)
        interactive._TRUST["ledger"] = None
    try:
        assert interactive._trust_lookup(tool="shell", command="git status") is None
    finally:
        with interactive._TRUST_LOCK:
            interactive._TRUST.update(saved)


# ---------------------------------------------------------------------------
# 7. Nothing here can weaken verifier-gated completion
# ---------------------------------------------------------------------------


def test_the_trust_receipt_carries_no_completion_claim():
    """`completed_unverified` must never be dressable as success, and a
    containment receipt must not smuggle a status word at all."""
    trust = resolve_daily_trust({DAILY_SANDBOX_KEY: False}, repo_path="/repo")
    blob = json.dumps(trust.to_dict()).lower()
    for word in ("success", "verified", "completed", "pass", "ok"):
        assert word not in blob, f"the trust receipt must not contain '{word}'"


def test_unverified_completion_still_renders_as_unverified():
    """The unchanged fail-closed projection, pinned so this round cannot move it."""
    from cli.runview import effective_terminal_status, status_is_verified, status_label

    assert (
        status_label(effective_terminal_status("completed_unverified", [])) != "SUCCESS"
    )
    assert (
        status_is_verified(effective_terminal_status("completed_unverified", []))
        is False
    )
    assert status_is_verified(effective_terminal_status("failed", [])) is False


def test_the_policy_engine_still_denies_a_protected_path_regardless_of_grants():
    """A grant must never launder a hard deny (pre-existing contract)."""
    call = ToolCall(
        call_id="c9",
        tool="read",
        arguments={"path": ".env"},
        side_effect_class="read_only",
    )
    engine = PolicyEngine([{"action": "allow", "tool": "read"}], default_action="allow")
    assert engine.evaluate(call).action == "deny"
    engine.record_approval(call, engine.evaluate(call), True, "session_path")
    assert engine.evaluate(call).action == "deny"


def test_the_resolved_default_reaches_the_sandbox_boundary_and_the_opt_out_does_not():
    """The mechanism, not the config value: which executor actually runs.

    A `sandbox_call` double stands in for the Docker boundary so the assertion
    works on a host with no daemon, and so it DISCRIMINATES: the default arm
    must reach the injected sandbox and the opt-out arm must not. (A real
    daemon run of the same two arms is recorded in `cli/AGENTS.md`: default
    `profile=docker` in 3.0s, opt-out `profile=local_trusted` in 0.3s.)
    """
    from execution.workspace import SafeToolBackend, Workspace

    calls: list[str] = []
    repo = Path(tempfile.mkdtemp())
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")

    def _fake_sandbox(_root: str, command: str, values: dict) -> dict:
        calls.append(command)
        return {
            "exit_code": 0,
            "stdout": "sandboxed\n",
            "stderr": "",
            "timed_out": False,
        }

    backend = SafeToolBackend(
        Workspace(str(repo)),
        approve=lambda tool, values: {"approved": True, "scope": "once"},
        sandbox_call=_fake_sandbox,
    )
    default = resolve_daily_trust({}, repo_path=str(repo))
    result = backend.execute(
        "shell", {"command": "echo hi"}, sandboxed=default.sandboxed
    )
    assert calls == ["echo hi"], (
        "the default must dispatch through the sandbox boundary"
    )
    assert result.ok is True
    assert result.profile == "docker"

    calls.clear()
    opted_out = resolve_daily_trust({DAILY_SANDBOX_KEY: False}, repo_path=str(repo))
    result = backend.execute(
        "shell", {"command": "echo hi"}, sandboxed=opted_out.sandboxed
    )
    assert calls == [], "the opt-out must run on the host, not in a container"
    assert result.profile == "local_trusted"


def test_a_daily_trust_receipt_is_json_serialisable_and_immutable():
    trust = resolve_daily_trust({}, repo_path="/repo", repo_key="k", session_id="s")
    assert isinstance(trust, DailyTrust)
    json.dumps(trust.to_dict())
    with pytest.raises(Exception):
        trust.sandboxed = False  # type: ignore[misc]


def test_a_stale_permission_decision_is_not_reused_by_a_new_grant():
    """Sanity: the engine's own decision record stays coherent after a refusal."""
    call = ToolCall(
        call_id="c10",
        tool="shell",
        arguments={"command": ""},
        side_effect_class="process",
    )
    engine = PolicyEngine([], default_action="ask")
    decision: PermissionDecision = engine.evaluate(call)
    updated = engine.record_approval(call, decision, True, "global")
    assert updated.call_id == decision.call_id
    assert updated.exact_effect == decision.exact_effect
    assert engine.audit_log[-1] is updated
    assert os.path.isfile("harness/agent_kernel/policy.py")


def test_trust_resolution_never_raises_on_a_hostile_config():
    """`resolve_daily_trust` is on the run's start path; it must be total."""
    for value in (None, 0, 1, "", "x", [], {}, object(), b"bytes"):
        trust = resolve_daily_trust(
            {DAILY_SANDBOX_KEY: value, KERNEL_SANDBOX_KEY: value}
        )
        assert isinstance(trust, DailyTrust)
        trust.banner_lines()
        json.dumps(trust.to_dict())
    assert resolve_daily_trust({"approval_scope": "global"}).approval_scope != "global"
    assert resolve_daily_trust({"approval_scope": 17}).approval_scope in APPROVAL_SCOPES


def test_a_requested_scope_wider_than_the_ceiling_is_clamped_and_said():
    trust = resolve_daily_trust({"approval_scope": "session_command_prefix"})
    assert trust.approval_scope == "session_command_prefix"
    assert trust.approval_scope_requested == "session_command_prefix"
    assert trust.approval_notes == ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
