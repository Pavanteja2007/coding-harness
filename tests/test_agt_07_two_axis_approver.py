"""AGT-07 -- two axes of safety, and an approver agent that fails closed.

Host-only for every control-plane proof: no Docker, no model provider, no
network. The two Docker-gated classes are marked and skip themselves with the
daemon's reason, and a skip is BLOCKED coverage, never a pass.

The six required proofs, in the brief's order, are named after the BEHAVIOUR
they pin:

* ``test_an_unparseable_approver_reply_denies``;
* ``test_a_timeout_denies``;
* ``test_the_approver_only_sees_already_privileged_actions``;
* ``test_network_is_off_unless_declared``;
* ``test_dot_git_is_read_only_inside_a_writable_root``;
* ``test_a_subagents_escalation_is_labelled_with_its_origin``.

Around them sit the non-vacuity controls, which matter more than the proofs
for this particular round: an approver that denies everything passes every
one of the first three. ``test_a_well_formed_approval_is_an_approval`` is the
control that stops that from being a pass, and the
``TestTheAxesCannotSubstituteForEachOther`` class is the control for the
two-axis claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from execution import sandbox as sb
from harness import approver as ap
from harness.agent_kernel.contracts import ToolCall
from harness.agent_kernel.policy import (
    ContainmentAxis,
    PolicyEngine,
    SafetyReceipt,
)
from shared.approval import (
    APPROVER_DECISIONS,
    APPROVER_FAILURES,
    ApprovalOrigin,
    ApproverReply,
    ApproverRequest,
    approver_admissible,
    canonical_effect,
    describe_axes,
    parse_approver_reply,
)


def _docker_up() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or b"").strip())


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


MAIN = ApprovalOrigin(thread_id="thread-main")
SUB = ApprovalOrigin(thread_id="thread-main", subagent_id="sub-7", parent_id="planner")


def _effect(command: str = "python -m pytest tests -q") -> object:
    return canonical_effect(
        "shell",
        command.split(),
        working_directory="/workspace",
        side_effect_class="workspace_write",
    )


def _engine() -> PolicyEngine:
    return PolicyEngine(
        [
            {"action": "allow", "tool": "read"},
            {"action": "ask", "tool": "shell"},
        ],
        actor="agent",
    )


class _Recorder:
    """A scripted Boundary-2 model boundary that records what it was asked."""

    def __init__(self, reply, delay_s: float = 0.0, raises: bool = False) -> None:
        self.reply = reply
        self.delay_s = delay_s
        self.raises = raises
        self.prompts = []
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.raises:
            raise RuntimeError("the reviewer provider is down")
        return self.reply


# ---------------------------------------------------------------------------
# Axis (a): technical containment
# ---------------------------------------------------------------------------


class TestTechnicalContainment:
    def test_network_is_off_unless_declared(self, tmp_path):
        """Network is a DECLARATION, and the default argv carries `--network none`."""
        repo = tmp_path / "repo"
        repo.mkdir()

        default = sb.resolve_containment(str(repo))
        assert default.network_enabled is False
        assert default.network_declaration == ""
        args = sb._docker_run_args(
            "img", str(repo), 30, default.network_enabled, None, "1g", 1.0, 512
        )
        assert args[args.index("--network") + 1] == "none"

        declared = sb.resolve_containment(
            str(repo), allow_network=True, network_declaration="task config"
        )
        assert declared.network_enabled is True
        assert declared.network_declaration == "task config"
        on = sb._docker_run_args(
            "img", str(repo), 30, declared.network_enabled, None, "1g", 1.0, 512
        )
        assert "--network" not in on
        # A declared network is reported as a declaration, and a declared
        # network with no allowlist says so instead of implying enforcement.
        assert "not an enforced allowlist" in " ".join(declared.notes)

    def test_dot_git_is_read_only_inside_a_writable_root(self, tmp_path):
        """`.git` is mounted READ-ONLY inside the otherwise writable workspace.

        The workspace mount itself is unchanged (edits must persist to the
        host); the `.git` subtree is re-mounted read-only on top of it. This
        is asserted on the argv, which is the thing the daemon receives.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")

        policy = sb.resolve_containment(str(repo))
        assert ".git" in policy.readonly_subpaths
        overlays = sb.readonly_overlays(policy)
        assert [target for _, target in overlays] == ["/workspace/.git"]

        args = sb._docker_run_args(
            "img", str(repo), 30, False, None, "1g", 1.0, 512, readonly_mounts=overlays
        )
        volumes = [args[i + 1] for i, tok in enumerate(args) if tok == "--volume"]
        workspace = [item for item in volumes if item.endswith(":/workspace")]
        assert workspace, volumes
        assert ":ro" not in workspace[0], "the workspace must stay writable"
        assert f"{sb._win_to_docker(str(repo / '.git'))}:/workspace/.git:ro" in volumes
        # The overlay comes AFTER the workspace mount, so the deeper mount
        # target is the one Docker applies.
        readonly_index = next(
            index
            for index, item in enumerate(volumes)
            if item.endswith("/workspace/.git:ro")
        )
        assert volumes.index(workspace[0]) < readonly_index
        sb.assert_sandbox_argv_isolated(args, str(repo))

        receipt = sb.containment_receipt(policy, applied=overlays)
        assert receipt["readonly_applied"] == [".git"]
        assert receipt["readonly_absent"] == [
            item for item in policy.readonly_subpaths if item != ".git"
        ]

    def test_a_read_only_path_that_is_absent_is_reported_not_created(self, tmp_path):
        """`docker run -v` CREATES a missing host path, so an absent one is skipped."""
        repo = tmp_path / "repo"
        repo.mkdir()
        policy = sb.resolve_containment(str(repo))
        overlays = sb.readonly_overlays(policy)
        receipt = sb.containment_receipt(policy, applied=overlays)
        assert receipt["readonly_applied"] == []
        assert ".git" in receipt["readonly_absent"]
        assert not (repo / ".git").exists()

    def test_declared_writable_roots_make_the_workspace_read_only(self, tmp_path):
        """A declared writable root is a technical fact, not a comment."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
        (repo / "docs").mkdir()

        policy = sb.resolve_containment(str(repo), writable_roots=["src"])
        assert policy.workspace_readonly is True
        assert policy.writable_roots == ("src",)
        args = sb._docker_run_args(
            "img",
            str(repo),
            30,
            False,
            None,
            "1g",
            1.0,
            512,
            workspace_readonly=True,
            writable_mounts=sb.writable_overlays(policy),
        )
        volumes = [args[i + 1] for i, tok in enumerate(args) if tok == "--volume"]
        assert f"{sb._win_to_docker(str(repo))}:/workspace:ro" in volumes
        assert f"{sb._win_to_docker(str(repo / 'src'))}:/workspace/src:rw" in volumes
        assert not any(item.endswith(":/workspace/docs:rw") for item in volumes)
        sb.assert_sandbox_argv_isolated(args, str(repo), writable_paths=["src"])

    def test_a_writable_root_cannot_reopen_a_read_only_path(self, tmp_path):
        """A config typo must not be able to widen containment."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        policy = sb.resolve_containment(str(repo), writable_roots=[".git"])
        assert policy.writable_roots == ()
        assert any("read-only declaration wins" in note for note in policy.notes)

    def test_the_gate_refuses_an_overlay_that_lost_its_read_only_mode(self, tmp_path):
        """The pre-spawn gate is what stops a future refactor from widening it."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        overlays = sb.readonly_overlays(sb.resolve_containment(str(repo)))
        base = sb._docker_run_args(
            "img", str(repo), 30, False, None, "1g", 1.0, 512, readonly_mounts=overlays
        )

        def _with(spec: str):
            args = list(base)
            index = [i for i, tok in enumerate(args) if tok == "--volume"]
            for i in index:
                if args[i + 1].endswith("/workspace/.git:ro"):
                    args[i + 1] = spec
                    return args
            raise AssertionError("the overlay is missing from the argv")

        # docker defaults a volume to READ-WRITE, so a lost `:ro` is a widened
        # container. The gate refuses it rather than shipping it.
        with pytest.raises(sb.SandboxDependencyError):
            sb.assert_sandbox_argv_isolated(
                _with(f"{sb._win_to_docker(str(repo / '.git'))}:/workspace/.git"),
                str(repo),
            )
        with pytest.raises(sb.SandboxDependencyError):
            sb.assert_sandbox_argv_isolated(
                _with(f"{sb._win_to_docker(str(repo / '.git'))}:/workspace/.git:rw"),
                str(repo),
            )
        # The same mount is admitted exactly where a writable root was declared.
        sb.assert_sandbox_argv_isolated(
            _with(f"{sb._win_to_docker(str(repo / '.git'))}:/workspace/.git:rw"),
            str(repo),
            writable_paths=[".git"],
        )

    def test_the_receipt_carries_no_prompting_field(self, tmp_path):
        """The containment receipt must not be able to answer the other axis."""
        repo = tmp_path / "repo"
        repo.mkdir()
        receipt = sb.containment_receipt(sb.resolve_containment(str(repo)))
        assert receipt["axis"] == "containment"
        for key in ("decision", "ask", "approved", "requires_human", "prompt"):
            assert key not in receipt


# ---------------------------------------------------------------------------
# Axis (b) reporting beside axis (a)
# ---------------------------------------------------------------------------


class TestTheAxesCannotSubstituteForEachOther:
    def test_a_decision_alone_cannot_claim_containment(self):
        """The only constructor that omits a receipt says UNKNOWN, not 'fine'."""
        engine = _engine()
        decision = engine.evaluate(
            ToolCall(tool="shell", arguments={"command": "git status"})
        )
        receipt = SafetyReceipt.from_decision_only(decision)
        assert receipt.containment.known is False
        assert receipt.containment.contained is False
        assert "UNKNOWN" in receipt.summary()
        # Passing no containment to the two-axis entry point is the same thing.
        via_engine = engine.evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "git status"})
        )
        assert via_engine.containment.known is False
        assert via_engine.containment.contained is False

    def test_a_contained_run_still_asks_for_a_privileged_action(self, tmp_path):
        """Axis (a) is not a mitigation channel and not a decision input."""
        repo = tmp_path / "repo"
        repo.mkdir()
        containment = sb.containment_receipt(sb.resolve_containment(str(repo)))
        receipt = _engine().evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "rm -rf build"}),
            containment=containment,
        )
        assert receipt.requires_human is True
        assert receipt.decision.action == "ask"
        assert receipt.containment.known is True
        # A contained run is not an allow, and an allow is not a containment.
        assert "decision: ask" in receipt.summary()
        assert "says nothing about containment" in receipt.summary()

    def test_a_containment_receipt_carrying_a_decision_is_refused(self, tmp_path):
        """The conflation is detected, not rendered."""
        repo = tmp_path / "repo"
        repo.mkdir()
        smuggled = sb.containment_receipt(sb.resolve_containment(str(repo)))
        smuggled["approved"] = True
        axis = ContainmentAxis.from_receipt(smuggled)
        receipt = SafetyReceipt(
            containment=axis,
            decision=_engine().evaluate(
                ToolCall(tool="shell", arguments={"command": "ls"})
            ),
        )
        problems = receipt.axes_confused()
        assert problems and "approved" in problems[0]
        assert "AXES CONFLATED" in receipt.summary()

    def test_describe_axes_refuses_to_substitute(self):
        assert "UNKNOWN" in describe_axes(decision="allow")
        assert "says nothing about containment" in describe_axes(decision="allow")
        line = describe_axes(
            containment={"network_enabled": True, "readonly_applied": [".git"]},
            decision="allow",
        )
        assert "network ON" in line and "decision: allow" in line

    def test_a_deny_stays_a_deny_whatever_the_containment(self, tmp_path):
        """Containment cannot launder a protected-path deny."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        containment = sb.containment_receipt(sb.resolve_containment(str(repo)))
        engine = PolicyEngine([{"action": "allow", "tool": "shell"}], actor="agent")
        receipt = engine.evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "cat .git/config"}),
            containment=containment,
        )
        assert receipt.denied is True
        assert receipt.requires_human is False
        assert receipt.containment.contained is True

    def test_the_verifier_gate_is_not_involved(self, tmp_path):
        """Nothing in this round's surface can mint a completion status."""
        source = Path(ap.__file__).read_text(encoding="utf-8")
        assert "completed_unverified" not in source
        assert "completed_verified" not in source
        assert "mint" not in source.lower().split("class approveragent")[0]
        receipt = _engine().evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "ls"}),
            containment=sb.containment_receipt(sb.resolve_containment(str(tmp_path))),
        )
        blob = json.dumps(receipt.to_dict())
        for word in ("success", "verified", "complete"):
            assert word not in blob.casefold()


# ---------------------------------------------------------------------------
# The approver agent
# ---------------------------------------------------------------------------


class TestApproverFailsClosed:
    def test_an_unparseable_approver_reply_denies(self):
        for reply, failure in (
            ("looks fine to me", "unparseable"),
            ("", "empty"),
            ('{"decision": "approve"}', "no_reason"),
            ('{"decision": "perhaps", "reason": "x"}', "malformed"),
            ('{"decision": "approve", "reason": "a", "verdict": "deny"}', "ambiguous"),
            (None, "empty"),
            (object(), "unparseable"),
        ):
            verdict = parse_approver_reply(reply)
            assert verdict.approved is False, reply
            assert verdict.decision == "deny"
            assert verdict.failure == failure, (reply, verdict.failure)

    def test_a_timeout_denies(self):
        recorder = _Recorder('{"decision": "approve", "reason": "fine"}', delay_s=2.0)
        agent = ap.ApproverAgent(call_fn=recorder, timeout_s=0.25, model="cheap-tier")
        started = time.monotonic()
        verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        elapsed = time.monotonic() - started
        assert verdict.approved is False
        assert verdict.failure == "timeout"
        assert verdict.decision == "deny"
        assert elapsed < 1.75, "the wait must be bounded, not merely reported"
        assert recorder.calls == 1, "the reviewer WAS asked; the answer was too slow"

    def test_a_provider_error_denies(self):
        agent = ap.ApproverAgent(call_fn=_Recorder("", raises=True))
        verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert verdict.approved is False
        assert verdict.failure == "model_error"

    def test_a_disabled_agent_denies_without_asking_anything(self):
        recorder = _Recorder('{"decision": "approve", "reason": "fine"}')
        agent = ap.ApproverAgent(call_fn=recorder, enabled=False)
        verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert verdict.approved is False
        assert verdict.failure == "disabled"
        assert recorder.calls == 0
        assert agent.report()["fail_closed"] is True

    def test_a_well_formed_approval_is_an_approval(self):
        """The control: an approver that denies everything is not a pass."""
        recorder = _Recorder('{"decision": "approve", "reason": "reads one file"}')
        agent = ap.ApproverAgent(call_fn=recorder, model="cheap-tier")
        verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert verdict.approved is True
        assert verdict.reason == "reads one file"
        assert verdict.failure == ""
        assert agent.report()["approved"] == 1
        assert agent.report()["denied"] == 0

    def test_a_well_formed_denial_is_a_denial_with_a_reason(self):
        recorder = _Recorder('{"decision": "deny", "reason": "rewrites history"}')
        agent = ap.ApproverAgent(call_fn=recorder)
        verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert verdict.approved is False
        assert verdict.failure == ""
        assert verdict.reason == "rewrites history"
        # A refusal with a reason and a refusal we could not obtain are
        # different artifacts and stay distinguishable in the receipt.
        assert agent.report()["failures"] == []

    def test_the_approver_only_sees_already_privileged_actions(self):
        """An unprivileged action never reaches a model at all."""
        recorder = _Recorder('{"decision": "approve", "reason": "fine"}')
        agent = ap.ApproverAgent(call_fn=recorder)

        unprivileged = agent.review(_effect(), origin=MAIN, requires_human=False)
        assert unprivileged.approved is False
        assert unprivileged.failure == "not_privileged"
        assert recorder.calls == 0, "an unprivileged action must not be reviewed"
        assert (
            "only judges actions that already require a human" in (agent.refusals[-1])
        )

        privileged = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert recorder.calls == 1
        assert privileged.approved is True
        assert agent.report()["not_privileged"] == 1

    def test_the_approver_cannot_approve_a_hard_deny(self, tmp_path):
        """The reviewer is a second opinion on a question, not a new axis."""
        engine = _engine()
        recorder = _Recorder('{"decision": "approve", "reason": "I like it"}')
        agent = ap.ApproverAgent(call_fn=recorder)
        receipt = engine.evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "cat .git/config"})
        )
        assert receipt.denied is True
        # A denied action is not a question for the approver: the call site
        # would never ask, and `requires_human` is False for it.
        assert receipt.requires_human is False
        verdict = agent.review(
            _effect(),
            origin=MAIN,
            requires_human=receipt.requires_human,
        )
        assert verdict.failure == "not_privileged"
        assert recorder.calls == 0

    def test_a_subagents_escalation_is_labelled_with_its_origin(self):
        recorder = _Recorder('{"decision": "approve", "reason": "bounded"}')
        agent = ap.ApproverAgent(call_fn=recorder)
        verdict = agent.review(_effect(), origin=SUB, requires_human=True)
        assert verdict.approved is True
        assert verdict.origin.subagent_id == "sub-7"
        # The MODEL saw the origin, not just the receipt.
        prompt = recorder.prompts[0]
        assert "subagent sub-7" in prompt
        assert "escalating to main thread thread-main" in prompt
        # And so does the human-facing line.
        assert "subagent sub-7" in verdict.summary()
        assert "main thread thread-main" in verdict.summary()
        # A main-thread escalation never claims to be a subagent.
        main_verdict = agent.review(_effect(), origin=MAIN, requires_human=True)
        assert "subagent" not in main_verdict.summary()

    def test_an_untraceable_subagent_escalation_is_refused(self):
        orphan = ApprovalOrigin(subagent_id="sub-9")
        assert orphan.is_subagent is True
        assert orphan.provenance_complete is False
        recorder = _Recorder('{"decision": "approve", "reason": "fine"}')
        agent = ap.ApproverAgent(call_fn=recorder)
        verdict = agent.review(_effect(), origin=orphan, requires_human=True)
        assert verdict.approved is False
        assert verdict.failure == "not_privileged"
        assert recorder.calls == 0
        assert "not fully traceable" in orphan.describe()

    def test_every_decision_is_journalled_with_its_reasoning(self, tmp_path):
        journal = tmp_path / "logs" / "task-1" / "approvals" / "approver.jsonl"
        recorder = _Recorder('{"decision": "deny", "reason": "touches .git"}')
        agent = ap.ApproverAgent(
            call_fn=recorder, journal_path=str(journal), task_id="task-1"
        )
        agent.review(_effect(), origin=SUB, requires_human=True)
        agent.review(_effect(), origin=SUB, requires_human=False)
        rows = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 2
        assert rows[0]["verdict"]["reason"] == "touches .git"
        assert rows[0]["verdict"]["origin"]["subagent_id"] == "sub-7"
        assert rows[0]["effect_digest"]
        assert rows[1]["verdict"]["failure"] == "not_privileged"
        # Every row carries the axes' own vocabulary, so a reader cannot tell
        # a refusal from an approval by shape alone.
        assert {row["verdict"]["decision"] for row in rows} == {"deny"}

    def test_the_journal_row_redacts_a_secret_in_the_command(self, tmp_path):
        journal = tmp_path / "approver.jsonl"
        recorder = _Recorder('{"decision": "approve", "reason": "ok"}')
        agent = ap.ApproverAgent(call_fn=recorder, journal_path=str(journal))
        agent.review(
            canonical_effect("shell", ["deploy", "--token", "AKIAIOSFODNN7EXAMPLE"]),
            origin=MAIN,
            requires_human=True,
        )
        text = journal.read_text(encoding="utf-8")
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        assert "[REDACTED_SECRET]" in text

    def test_the_prompt_carries_the_exact_effect_and_not_a_summary(self):
        prompt = ap.render_approver_prompt(
            _effect("python -m pytest tests/test_a.py -q"),
            origin=SUB,
            containment={"network_enabled": False, "readonly_applied": [".git"]},
            privileged_reason="side effect class workspace_write",
        )
        assert "python -m pytest tests/test_a.py -q" in prompt
        assert "/workspace" in prompt
        assert "workspace_write" in prompt
        assert "subagent sub-7" in prompt
        assert "network off" in prompt
        # A reviewer may not hand out a standing grant.
        for phrase in ("always allow", "approve all", "from now on"):
            assert phrase not in prompt.casefold()

    def test_config_activation_is_key_presence_plus_truthiness(self):
        assert ap.ApproverAgent.from_config({}).enabled is False
        assert ap.ApproverAgent.from_config({"approver_agent": None}).enabled is False
        assert ap.ApproverAgent.from_config({"approver_agent": False}).enabled is False
        assert ap.ApproverAgent.from_config({"approver_agent": True}).enabled is True
        assert ap.ApproverAgent.from_config({"approver_agent": "yes"}).enabled is True
        # Absent means absent: the merged DEFAULTS value must not switch it on.
        from harness.config import DEFAULTS

        assert DEFAULTS[ap.APPROVER_ENABLED_KEY] is None
        assert ap.ApproverAgent.from_config(DEFAULTS).enabled is False

    def test_the_approver_uses_the_cheap_tier_not_a_fixed_model(self):
        seen = {}

        def _capture(messages, difficulty_hint=None, model=None):
            seen["hint"] = difficulty_hint
            seen["model"] = model
            return '{"decision": "approve", "reason": "ok"}'

        agent = ap.ApproverAgent(call_fn=_capture)
        agent.review(_effect(), origin=MAIN, requires_human=True)
        assert seen["hint"] == "easy"
        assert seen["model"] is None, "no model is named unless one is configured"

    def test_a_bare_callable_boundary_still_works(self):
        """A `messages -> str` double, a router, and a keyword-arg router."""
        for boundary in (
            lambda messages: '{"decision": "approve", "reason": "ok"}',
            lambda messages, difficulty_hint=None, model=None: (
                '{"decision": "approve", "reason": "ok"}'
            ),
        ):
            agent = ap.ApproverAgent(call_fn=boundary)
            assert agent.review(_effect(), origin=MAIN, requires_human=True).approved

    def test_the_one_call_form_matches_the_two_step_form(self):
        recorder = _Recorder('{"decision": "deny", "reason": "no"}')
        verdict = ap.review_privileged_action(
            _effect(),
            config={"approver_agent": True},
            origin=MAIN,
            requires_human=True,
            call_fn=recorder,
        )
        assert verdict.approved is False and verdict.reason == "no"

    def test_the_parsers_cannot_be_coerced_into_a_blanket_approval(self):
        """A closed decision set and a closed failure set, both enforced."""
        assert APPROVER_DECISIONS == ("approve", "deny")
        with pytest.raises(ValueError):
            ApproverReply(decision="maybe", reason="x")
        with pytest.raises(ValueError):
            ApproverReply(decision="approve", reason="   ")
        with pytest.raises(ValueError):
            ApproverReply(decision="deny", reason="x", failure="because_i_said_so")
        with pytest.raises(ValueError):
            ApproverReply.failed_verdict("")
        for failure in ("timeout", "unparseable", "ambiguous", "malformed"):
            assert failure in APPROVER_FAILURES
            assert ApproverReply.failed_verdict(failure).approved is False

    def test_admissibility_is_refused_without_a_human_in_the_loop(self):
        effect = _effect()
        ok, why = approver_admissible(
            ApproverRequest(effect=effect, origin=MAIN, requires_human=True)
        )
        assert ok is True and why == ""
        ok, why = approver_admissible(ApproverRequest(effect=effect, origin=MAIN))
        assert ok is False and "already require a human" in why


# ---------------------------------------------------------------------------
# The two axes, end to end through the real policy engine
# ---------------------------------------------------------------------------


class TestBothAxesOnOneCall:
    def test_a_privileged_contained_call_reports_both_and_labels_origin(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        containment = sb.containment_receipt(
            sb.resolve_containment(str(repo)),
            applied=sb.readonly_overlays(sb.resolve_containment(str(repo))),
        )
        engine = _engine()
        receipt = engine.privileged_receipt(
            ToolCall(
                tool="shell",
                arguments={"command": "git commit -am wip"},
                side_effect_class="workspace_write",
            ),
            containment=containment,
        )
        assert receipt.requires_human is True
        assert receipt.axes_confused() == []
        assert receipt.containment.readonly_applied == (".git",)

        seen = {}

        def _capture(messages, **kwargs):
            seen["prompt"] = messages[-1]["content"]
            return '{"decision": "deny", "reason": "commits unreviewed work"}'

        verdict = ap.ApproverAgent(call_fn=_capture, model="cheap-tier").review(
            canonical_effect("shell", ["git", "commit", "-am", "wip"]),
            origin=ApprovalOrigin(thread_id="t", subagent_id="s", parent_id="p"),
            requires_human=receipt.requires_human,
            privileged_reason=receipt.decision.reason,
            containment=receipt.approver_prompt_context(),
        )
        assert verdict.approved is False
        assert verdict.reason == "commits unreviewed work"
        assert "network off" in seen["prompt"]
        assert "subagent s" in seen["prompt"]
        assert (
            "requires a human" in seen["prompt"] or "requires_human" in seen["prompt"]
        )

    def test_the_receipt_serialises_both_axes_and_the_confusion_check(self, tmp_path):
        receipt = _engine().evaluate_axes(
            ToolCall(tool="shell", arguments={"command": "ls"}),
            containment=sb.containment_receipt(sb.resolve_containment(str(tmp_path))),
        )
        record = receipt.to_dict()
        assert record["axes"] == ["containment", "decision"]
        assert record["containment"]["axis"] == "containment"
        assert record["decision"]["action"] == "ask"
        assert record["decision"]["requires_human"] is True
        assert record["axes_confused"] == []
        json.dumps(record)  # must stay JSON-safe


# ---------------------------------------------------------------------------
# Real Docker: the read-only `.git` is a fact, not an argv shape
# ---------------------------------------------------------------------------


@requires_docker
class TestRealContainerContainment:
    def test_dot_git_is_read_only_inside_a_writable_root(self, tmp_path):
        """A REAL container: the workspace writes, `.git` does not.

        This is the proof the argv assertions cannot make: it runs the actual
        `docker run` this round changed and reads the result off the container.
        The write into `.git` must fail with a read-only filesystem, and a
        write into the same workspace one directory up must succeed.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")

        result = sb.execute_sandboxed(
            str(repo),
            "printf 'patched' > app.py; "
            "printf 'x' > .git/config 2>/dev/null; "
            "git --version >/dev/null 2>&1; "
            "echo WROTE=$(cat app.py); "
            "if printf 'forged' > .git/config 2>/dev/null; then echo GIT=WRITABLE; "
            "else echo GIT=READONLY; fi",
            180,
        )
        assert result.exit_code == 0, result.stderr[-2000:]
        assert "WROTE=patched" in result.stdout
        assert "GIT=READONLY" in result.stdout
        assert (repo / "app.py").read_text(encoding="utf-8") == "patched"
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"

    def test_an_explicit_opt_out_really_lets_a_container_write_dot_git(self, tmp_path):
        """The control: `readonly_paths=[]` is a genuine opt-out, not a no-op."""
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        result = sb.execute_sandboxed(
            str(repo),
            "if printf 'forged' > .git/config 2>/dev/null; then echo GIT=WRITABLE; "
            "else echo GIT=READONLY; fi",
            180,
            readonly_paths=[],
        )
        assert result.exit_code == 0, result.stderr[-2000:]
        assert "GIT=WRITABLE" in result.stdout
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == "forged"

    def test_a_container_run_carries_the_containment_receipt(
        self, tmp_path, monkeypatch
    ):
        """The trace overlay records axis (a) on its own field.

        The repo is mounted from a `logs/{task_id}/work` path because that is
        how the task id is derived for the overlay; a run from anywhere else
        emits no per-task row at all, which is the harness's existing
        behaviour and not something this round changes.
        """
        repo = tmp_path / "logs" / "task-1" / "work"
        (repo / ".git").mkdir(parents=True)
        events = []
        from shared import tracing

        monkeypatch.setattr(
            tracing,
            "emit",
            lambda module, event, **fields: events.append((module, event, fields)),
        )
        sb.execute_sandboxed(str(repo), "true", 180)
        calls = [fields for _, event, fields in events if event == "sandbox_call"]
        assert calls, "no sandbox_call event was emitted"
        containment = calls[-1]["containment"]
        assert containment["axis"] == "containment"
        assert containment["readonly_applied"] == [".git"]
        assert containment["network_enabled"] is False
        assert "decision" not in containment
