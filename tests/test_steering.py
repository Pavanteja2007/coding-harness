"""Mid-task steering (steering round): unit + integration + e2e tests.

Coverage map:
- SteeringBuffer unit (journal, replay, cap, take/consume, contexts)
- CROSS-INSTANCE transport (the fatal gap this round fixed: an
  injector's buffer and the loop's buffer are different objects; the
  journal file is the only channel, so consumer polls must re-scan)
- parse_steering_line (explicit intents; bare abort/replan defaults)
- TaskStateMachine.record_event (Task B: steering never corrupts the
  trail)
- steer_live_run + the REPL reader's live-run routing (Task A surface)
- E2E through the REAL loop (Docker-gated, same pattern as
  test_self_critique): guide at a turn boundary, abort at a step
  boundary, replan keeping work/, the final-gate guarantee (Task C:
  pending steering blocks success minting), the pre-mint re-check,
  the OFF arm, and resume-after-steering (Task C: journal replay).
"""

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

import harness.steering as steering_mod
from harness.deps import reset_overrides, set_call_model
from harness.state_machine import TaskStateMachine, current_phase, read_transitions
from shared.types import Task
from tests.fake_model import ScriptedModel

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_gate():
    return pytest.mark.skipif(
        os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
        reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
    )


# ---------------------------------------------------------------------------
# parse_steering_line (the CLI-side intent parser)
# ---------------------------------------------------------------------------


class TestParseSteeringLine:
    def test_plain_text_is_guide(self):
        assert steering_mod.parse_steering_line("only touch parser.py") == (
            "guide",
            "only touch parser.py",
        )

    def test_replan_prefix(self):
        assert steering_mod.parse_steering_line("replan: use a cache") == (
            "replan",
            "use a cache",
        )

    def test_replan_space_prefix(self):
        assert steering_mod.parse_steering_line("replan start over carefully") == (
            "replan",
            "start over carefully",
        )

    def test_abort_with_reason(self):
        assert steering_mod.parse_steering_line("abort: wrong approach") == (
            "abort",
            "wrong approach",
        )

    def test_bare_abort_gets_placeholder_not_empty(self):
        """A bare 'abort' is a legitimate instruction — it must survive
        inject()'s empty-text refusal (the first session's parse fed
        inject an empty text, which silently refused the stop)."""
        intent, text = steering_mod.parse_steering_line("abort")
        assert intent == "abort"
        assert text.strip()

    def test_bare_replan_gets_placeholder_not_empty(self):
        intent, text = steering_mod.parse_steering_line("replan")
        assert intent == "replan"
        assert text.strip()

    def test_never_raises(self):
        assert steering_mod.parse_steering_line("") == ("guide", "")
        assert steering_mod.parse_steering_line(None) == ("guide", "")

    def test_case_insensitive_prefix(self):
        assert steering_mod.parse_steering_line("REPLAN: redo it")[0] == "replan"
        assert steering_mod.parse_steering_line("Abort")[0] == "abort"


# ---------------------------------------------------------------------------
# SteeringBuffer unit (one instance, one process)
# ---------------------------------------------------------------------------


class TestSteeringBufferUnit:
    def _buf(self, tmp_path, **kw):
        return steering_mod.SteeringBuffer(tmp_path / "logs" / "t1", "t1", **kw)

    def test_inject_and_pending_roundtrip(self, tmp_path):
        buf = self._buf(tmp_path)
        ev = buf.inject("only touch file X", intent="guide", source="tui")
        assert ev is not None and ev.seq == 1
        pending = buf.pending()
        assert [e.text for e in pending] == ["only touch file X"]
        assert [e.intent for e in pending] == ["guide"]

    def test_empty_text_refused(self, tmp_path):
        assert self._buf(tmp_path).inject("   ") is None

    def test_take_consumes_and_journals(self, tmp_path):
        buf = self._buf(tmp_path)
        buf.inject("one")
        buf.inject("two")
        taken = buf.take("step-1-turn-0")
        assert [e.text for e in taken] == ["one", "two"]
        assert buf.pending() == []
        ops = [o["op"] for o in buf.journal()]
        assert ops == ["inject", "inject", "consume", "consume"]
        assert all(
            o["where"] == "step-1-turn-0" for o in buf.journal() if o["op"] == "consume"
        )

    def test_cap_refuses_beyond_max_pending(self, tmp_path):
        buf = self._buf(tmp_path, max_pending=2)
        assert buf.inject("a") is not None
        assert buf.inject("b") is not None
        assert buf.inject("c") is None  # honest refusal, never silent
        buf.take("gate")
        assert buf.inject("c") is not None  # consumed frees a slot

    def test_has_intent_dispatch_priority_inputs(self, tmp_path):
        buf = self._buf(tmp_path)
        buf.inject("just guidance", intent="guide")
        assert buf.has_intent("guide") and not buf.has_intent("abort")
        buf.inject("stop it", intent="abort")
        assert buf.has_intent("abort") and buf.has_intent("guide")

    def test_normalize_intent_degrades_to_guide(self):
        assert steering_mod.normalize_intent("nonsense") == "guide"
        assert steering_mod.normalize_intent("") == "guide"
        assert steering_mod.normalize_intent(None) == "guide"
        assert steering_mod.normalize_intent("REPLAN") == "replan"

    def test_steering_context_carries_consumed_and_pending(self, tmp_path):
        """The re-plan prompt must see ALL steering (consumed shaped the
        work in work/; pending is why the plan is being replaced)."""
        buf = self._buf(tmp_path)
        buf.inject("first instruction")
        buf.take("step-boundary")
        buf.inject("second instruction")
        ctx = buf.steering_context()
        assert "first instruction" in ctx and "second instruction" in ctx

    def test_replay_rebuilds_pending_from_journal(self, tmp_path):
        """Task C resume: a crashed consumer's unconsumed injects are
        STILL PENDING in a fresh buffer built on the same dir."""
        d = tmp_path / "logs" / "t1"
        first = steering_mod.SteeringBuffer(d, "t1")
        first.inject("survives the crash")
        second = steering_mod.SteeringBuffer(d, "t1")
        assert [e.text for e in second.pending()] == ["survives the crash"]
        # and consumes survive too (a fresh instance never re-applies
        # consumed steering)
        first.take("gate")
        third = steering_mod.SteeringBuffer(d, "t1")
        assert third.pending() == []

    def test_journal_survives_torn_tail(self, tmp_path):
        """A torn final line (crash mid-append) must not corrupt the
        inbox: complete lines fold in, the torn tail is skipped."""
        d = tmp_path / "logs" / "t1"
        d.mkdir(parents=True)
        journal = d / "steering.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "op": "inject",
                    "seq": 1,
                    "ts": 1.0,
                    "text": "ok",
                    "intent": "guide",
                    "source": "t",
                }
            )
            + "\n"
            + '{"op": "inject", "seq": 2, "ts": 2.0, "text": "tor',  # torn
            encoding="utf-8",
        )
        buf = steering_mod.SteeringBuffer(d, "t1")
        assert [e.text for e in buf.pending()] == ["ok"]
        # completing the tail makes it visible on the next poll
        with journal.open("a", encoding="utf-8") as fh:
            fh.write('n", "intent": "guide", "source": "t"}\n')
        assert [e.text for e in buf.pending()] == ["ok", "torn"]

    def test_shrunk_journal_rebuilds(self, tmp_path):
        """A journal that SHRANK below our byte cursor (truncation —
        e.g. a fresh start archived the old dir and a new, smaller
        journal landed) must trigger a full rebuild, not a misread."""
        d = tmp_path / "logs" / "t1"
        d.mkdir(parents=True)
        journal = d / "steering.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "op": "inject",
                    "seq": 5,
                    "ts": 1.0,
                    "text": "old event with a long body so the file is big",
                    "intent": "guide",
                    "source": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        buf = steering_mod.SteeringBuffer(d, "t1")
        assert buf.pending()  # cursor now sits at end-of-file
        # the file shrinks below the cursor (a fresh, smaller journal)
        journal.write_text(
            json.dumps(
                {
                    "op": "inject",
                    "seq": 1,
                    "ts": 9.0,
                    "text": "fresh",
                    "intent": "guide",
                    "source": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        assert [e.text for e in buf.pending()] == ["fresh"]

    def test_replaced_journal_same_size_is_tolerated(self, tmp_path):
        """A journal REPLACED with same-or-bigger content at the same
        path (archive-then-new landing the same size) is not detectable
        by size alone — document what the mechanism does: the bytes
        past the old cursor are still folded correctly (the common
        append case), never a crash or a raise."""
        d = tmp_path / "logs" / "t1"
        d.mkdir(parents=True)
        journal = d / "steering.jsonl"
        journal.write_text(
            json.dumps(
                {
                    "op": "inject",
                    "seq": 1,
                    "ts": 1.0,
                    "text": "one",
                    "intent": "guide",
                    "source": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        buf = steering_mod.SteeringBuffer(d, "t1")
        assert [e.text for e in buf.pending()] == ["one"]
        # a normal append (the real-world shape after any replace: the
        # archive moves the file away and the NEW journal only grows)
        other = steering_mod.SteeringBuffer(d, "t1")
        ev = other.inject("two")
        assert ev.seq == 2  # seq continues from the journal it read
        assert [e.text for e in buf.pending()] == ["one", "two"]

    def test_threaded_inject_and_consume_never_lose_events(self, tmp_path):
        """The reader thread injects while the 'loop' consumes: every
        event must land exactly once in exactly one take()."""
        buf = self._buf(tmp_path, max_pending=512)
        got = []
        stop = threading.Event()

        def injector(n):
            for i in range(40):
                buf.inject(f"thread-{n}-{i}")

        def consumer():
            while not stop.is_set() or buf.pending():
                taken = buf.take("poll")
                got.extend(e.text for e in taken)

        threads = [threading.Thread(target=injector, args=(n,)) for n in range(4)]
        con = threading.Thread(target=consumer, daemon=True)
        for t in threads:
            t.start()
        con.start()
        for t in threads:
            t.join()
        stop.set()
        con.join(timeout=10)
        assert len(got) == 160
        assert len(set(got)) == 160  # no duplicates


# ---------------------------------------------------------------------------
# CROSS-INSTANCE transport (the fatal gap this round closed)
# ---------------------------------------------------------------------------


class TestCrossInstanceTransport:
    def test_other_instances_injects_become_visible(self, tmp_path):
        """THE bug: the loop's buffer and the CLI-side injector buffer
        are different objects. Without the journal re-scan on every
        poll, steering NEVER reached the running task."""
        d = tmp_path / "logs" / "fix-abc"
        loop_buf = steering_mod.SteeringBuffer(d, "fix-abc")
        cli_buf = steering_mod.SteeringBuffer(d, "fix-abc")  # steer_live_run's
        cli_buf.inject("actually only touch file X", source="repl")
        assert [e.text for e in loop_buf.pending()] == ["actually only touch file X"]

    def test_loop_take_visible_to_a_monitoring_instance(self, tmp_path):
        d = tmp_path / "logs" / "fix-abc"
        loop_buf = steering_mod.SteeringBuffer(d, "fix-abc")
        watcher = steering_mod.SteeringBuffer(d, "fix-abc")
        loop_buf.inject("instruction")
        loop_buf.take("step-boundary")
        assert watcher.pending() == []  # consume seen across instances

    def test_seq_no_collision_across_instances(self, tmp_path):
        d = tmp_path / "logs" / "fix-abc"
        a = steering_mod.SteeringBuffer(d, "fix-abc")
        b = steering_mod.SteeringBuffer(d, "fix-abc")
        e1 = a.inject("from A")
        e2 = b.inject("from B")
        assert e1.seq != e2.seq
        # both visible, journal order
        texts = [e.text for e in a.pending()]
        assert texts == ["from A", "from B"]

    def test_cross_process_inject_reaches_consumer(self, tmp_path):
        """A SECOND TERMINAL steers the task: the journal is the
        cross-process channel (any process can inject)."""
        d = tmp_path / "logs" / "fix-abc"
        loop_buf = steering_mod.SteeringBuffer(d, "fix-abc")
        script = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from harness.steering import SteeringBuffer; "
            "b = SteeringBuffer(r'%s', 'fix-abc'); "
            "e = b.inject('steered from another terminal', source='second-terminal'); "
            "print(e.seq)" % (Path.cwd(), d)
        )
        cp = subprocess.run(
            [os.environ.get("VEX_PY", "python"), "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert cp.returncode == 0, cp.stderr
        assert [e.text for e in loop_buf.pending()] == ["steered from another terminal"]
        assert loop_buf.pending()[0].source == "second-terminal"


# ---------------------------------------------------------------------------
# Task B: steering + the state machine
# ---------------------------------------------------------------------------


class TestStateMachineInteraction:
    def test_record_event_leaves_phase_unchanged(self, tmp_path):
        """Guide consumption is NOT a transition: the machine stays in
        its current state, the trail shows WHEN steering was consumed."""
        m = TaskStateMachine(tmp_path)
        m.begin("run start")
        m.transition("editing", "step 1 session")
        m.record_event("steering", {"seqs": [1], "at": "turn-boundary"})
        assert m.phase == "editing"
        trail = read_transitions(tmp_path)
        assert trail[-1].get("event") == "steering"
        assert trail[-1]["phase"] == "editing"
        assert trail[-1]["valid"] is True

    def test_replan_rides_existing_forward_edges(self, tmp_path):
        """replan moves through EXISTING valid edges only:
        editing -> repairing -> planning (never editing -> planning,
        which the table rejects)."""
        m = TaskStateMachine(tmp_path)
        m.begin("run start")
        m.transition("editing", "attempt 1 steps")
        m.transition("repairing", "steering re-plan: attempt dismantled")
        m.transition("planning", "steering re-plan complete")
        assert current_phase(tmp_path) == "planning"
        with pytest.raises(Exception):
            m_invalid = TaskStateMachine(tmp_path)
            m_invalid.begin("other")
            m_invalid.transition("editing", "x")
            m_invalid.transition("planning", "backwards edge must raise")

    def test_abort_lands_failed_from_every_live_phase(self, tmp_path):
        """Every non-terminal phase allows a clean abort landing."""
        for phase_before in ("planning", "editing", "testing", "repairing"):
            m = TaskStateMachine(tmp_path / phase_before)
            m.begin("start", resuming=(phase_before == "repairing"))
            if phase_before == "editing":
                m.transition("editing", "steps")
            m.transition("failed", f"aborted by user steering at {phase_before}")
            assert current_phase(tmp_path / phase_before) == "failed"


# ---------------------------------------------------------------------------
# Task A surface: steer_live_run + REPL reader routing
# ---------------------------------------------------------------------------


class TestSteerLiveRun:
    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        yield
        reset_overrides()

    def _live(self, tmp_path):
        """A LIVE task setup: log dir + the trace file (the loop's first
        artifact — steer_live_run's live-gate key)."""
        import cli.interactive as iv

        d = tmp_path / "logs" / "fix-live01"
        d.mkdir(parents=True)
        (d / "trace.jsonl").write_text(
            '{"kind": "task_start", "data": {}}\n', encoding="utf-8"
        )
        return iv, tmp_path / "logs", "fix-live01"

    def test_guide_ack_and_journal(self, tmp_path, capsys):
        iv, log_root, tid = self._live(tmp_path)
        ack = iv.steer_live_run("only touch parser.py", tid, log_root)
        assert ack == "guide"
        journal = (log_root / tid / "steering.jsonl").read_text(encoding="utf-8")
        assert "parser.py" in journal
        out = capsys.readouterr().out
        assert "steered" in out

    def test_abort_intent_via_prefix(self, tmp_path, capsys):
        iv, log_root, tid = self._live(tmp_path)
        ack = iv.steer_live_run("abort: wrong approach", tid, log_root)
        assert ack == "abort"
        ev = json.loads((log_root / tid / "steering.jsonl").read_text(encoding="utf-8"))
        assert ev["intent"] == "abort" and ev["text"] == "wrong approach"
        assert "abort requested" in capsys.readouterr().out

    def test_conversational_input_never_injected(self, tmp_path, capsys):
        iv, log_root, tid = self._live(tmp_path)
        ack = iv.steer_live_run("hi there", tid, log_root)
        assert ack is None
        assert not (log_root / tid / "steering.jsonl").exists()

    def test_say_hook_routes_ack(self, tmp_path):
        """The TUI passes a transcript renderer; acks must reach it,
        not the captured-away console."""
        iv, log_root, tid = self._live(tmp_path)
        seen = []
        iv.steer_live_run("only touch x.py", tid, log_root, say=seen.append)
        assert any("steered" in s for s in seen)

    def test_pre_loop_window_refused_honestly(self, tmp_path, capsys):
        """Steering typed BEFORE the fix loop is live (e.g. during a
        build's stage-1 authoring) must be refused honestly, never
        written to a journal that _fresh_paths would archive on the
        loop's fresh start (a lost instruction with a lying ack)."""
        import cli.interactive as iv

        d = tmp_path / "logs" / "fix-preloop"
        d.mkdir(parents=True)  # dir exists, trace does NOT (pre-loop)
        ack = iv.steer_live_run("only touch parser.py", "fix-preloop", d.parent)
        assert ack == "starting"
        # nothing was injected (the journal doesn't exist)
        assert not (d / "steering.jsonl").exists()
        assert "still starting" in capsys.readouterr().out

    def test_resumed_run_steerable_immediately(self, tmp_path, capsys):
        """A RESUMED run keeps its prior trace (the dir is never
        archived), so steering right after resume passes the live gate
        and the loop's replaying buffer picks it up."""
        iv, log_root, tid = self._live(tmp_path)  # writes the trace
        ack = iv.steer_live_run("only touch parser.py", tid, log_root)
        assert ack == "guide"
        assert (log_root / tid / "steering.jsonl").is_file()


class TestReplLiveRegistration:
    """_execute_task/_run_one_build register the live run around the
    blocking call (Task A wiring: without the registration the reader
    thread queues steering into dead air)."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        import cli.interactive as iv

        iv._clear_live_run()
        yield
        iv._clear_live_run()
        reset_overrides()

    def test_execute_task_registers_and_clears(self, tmp_path):
        import cli.interactive as iv
        from shared.types import Task

        seen = {}

        class _FakeResult:
            def __init__(self):
                self.status = "success"
                self.attempts = 1
                self.cost_usd = 0.0
                self.model_calls = []
                self.diff = None
                self.verification = None

        def fake_run_task(task, log_root=None):
            seen["live"] = iv._live_run()
            return _FakeResult()

        import cli.deps as deps

        real = deps.get_run_task
        deps.get_run_task = lambda: fake_run_task
        try:
            task = Task(
                task_id="fix-reg01",
                repo_path=str(FIXTURES / "bug02_mean"),
                issue_text="x",
                config={"test_command": "python -m pytest -q"},
            )
            out = iv._execute_task(task, tmp_path / "logs", state={"quiet": True})
            assert out is not None
        finally:
            deps.get_run_task = real
        assert seen["live"]["task_id"] == "fix-reg01"
        assert iv._live_run() is None  # cleared on every exit path

    def test_execute_task_clears_on_crash(self, tmp_path):
        import cli.interactive as iv
        from shared.types import Task

        def exploding(task, log_root=None):
            raise RuntimeError("boom")

        import cli.deps as deps

        real = deps.get_run_task
        deps.get_run_task = lambda: exploding
        try:
            task = Task(
                task_id="fix-reg02",
                repo_path=str(FIXTURES / "bug02_mean"),
                issue_text="x",
                config={"test_command": "python -m pytest -q"},
            )
            assert (
                iv._execute_task(task, tmp_path / "logs", state={"quiet": True}) is None
            )
        finally:
            deps.get_run_task = real
        assert iv._live_run() is None


# ---------------------------------------------------------------------------
# E2E through the REAL loop (Docker-gated) — steering round Tasks A/B/C
# ---------------------------------------------------------------------------

ONE_STEP_PLAN = [
    {"id": 1, "description": "fix mean()", "checkpoint": "target test passes"}
]
FIX = "sed -i 's/len(values) - 1/len(values)/' numlib/mathutil.py"


class _Trace:
    def __init__(self, logs_root, task_id):
        self.events = []
        f = logs_root / task_id / "trace.jsonl"
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.events.append(json.loads(line))

    def kinds(self):
        return [e.get("kind") for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e.get("kind") == kind]


def _make_task(tmp_path, config=None):
    cfg = {
        "test_command": "python -m pytest -q",
        "command_timeout_s": 60,
        "verify_timeout_s": 180,
        "max_step_turns": 8,
    }
    cfg.update(config or {})
    return Task(
        task_id=f"steer-e2e-{abs(hash(str(tmp_path) + str(config))) % 1000000}",
        repo_path=str(FIXTURES / "bug02_mean"),
        issue_text="mean() divides by len-1; should divide by len",
        config=cfg,
    )


class _SteeringModel:
    """ScriptedModel + a steering-injection hook at chosen call points.

    plan/scripts behave exactly like ScriptedModel. Extras:
    - `inject_at`: {call_count: (text, intent)} — AFTER the Nth model
      call returns, the (text, intent) is injected into the task's
      steering journal via a FRESH buffer (exactly what the CLI-side
      injector does — cross-instance, the real transport).
    - `planner_plans`: queued replacement plans; each re-plan planner
      call (detected by the steering section in its user message)
      pops the next entry. The INITIAL planner call serves `plan`.
    - `critique_verdicts`: self-critique replies (True = approve).
    - `planner_prompts`: every planner-call user message (content-
      receipt assertions read these).
    """

    def __init__(
        self,
        plan,
        scripts,
        inject_at=None,
        planner_plans=None,
        critique_verdicts=None,
    ):
        self.inner = ScriptedModel(plan=plan, scripts=scripts)
        self.inject_at = dict(inject_at or {})
        self.planner_plans = [list(p) for p in (planner_plans or [])]
        self.critique_verdicts = list(critique_verdicts or [])
        self.call_count = 0
        self.injected = []
        self.planner_prompts = []
        self._log_dir = None
        self._task_id = None

    def bind(self, log_dir, task_id):
        self._log_dir = Path(log_dir)
        self._task_id = task_id

    def get_last_usage(self):
        return self.inner.get_last_usage()

    def __call__(self, messages, **kwargs):
        self.call_count += 1
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "skeptical code reviewer" in system:
            verdict = self.critique_verdicts.pop(0) if self.critique_verdicts else True
            reply = json.dumps({"addresses_issue": verdict, "reason": ""})
        elif "planning a bug fix" in system:
            self.planner_prompts.append(messages[1]["content"])
            if self.planner_plans and self._is_replan(messages):
                reply = json.dumps(
                    {"analysis": "replanned", "plan": self.planner_plans.pop(0)}
                )
            else:
                reply = self.inner(messages, **kwargs)
        else:
            reply = self.inner(messages, **kwargs)
        fire = self.inject_at.pop(self.call_count, None)
        if fire is not None:
            text, intent = fire
            buf = steering_mod.SteeringBuffer(self._log_dir, self._task_id)
            ev = buf.inject(text, intent=intent, source="test")
            self.injected.append((ev.seq if ev else None, text, intent))
        return reply

    @staticmethod
    def _is_replan(messages):
        return any(
            "User steering (mid-run instructions" in (m.get("content") or "")
            for m in messages
        )


@_docker_gate()
class TestSteeringE2E:
    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_overrides()
        yield
        reset_overrides()

    def test_guide_during_step_session_reaches_model(self, tmp_path):
        """Task A: a guide instruction injected mid-step lands in the
        LIVE session as a USER STEERING message (the model sees it on
        its next turn) — delivery proven via the loop's steering trace
        event + the session continuing normally."""
        model = _SteeringModel(
            ONE_STEP_PLAN,
            {1: [[FIX, "SUBMIT"]]},
            inject_at={1: ("say the word bananas", "guide")},
        )
        set_call_model(model)
        task = _make_task(
            tmp_path, config={"agent_tests": False, "self_critique": False}
        )
        log_dir = tmp_path / "logs" / task.task_id
        model.bind(log_dir, task.task_id)
        result = _run_quietly(tmp_path, task)
        # the steering message was consumed into the live session
        trace = _Trace(tmp_path / "logs", task.task_id)
        steer_events = trace.of("steering")
        assert steer_events, "guide steering must be consumed at a turn boundary"
        assert any("bananas" in json.dumps(e) for e in steer_events)
        # the loop completed honestly (guide never aborts; the fix landed)
        assert result.status == "success"

    def test_abort_at_step_boundary_stops_resumable(self, tmp_path):
        """Task A+B: abort stops cleanly at a step boundary — failed
        status, resumable artifacts on disk (state.json, plan.json,
        work/), machine lands failed, steering journal consumed."""
        # Step 1 fixes the bug but never SUBMITs; abort fires during
        # the second turn's model call -> consumed at the turn boundary
        # -> run_step returns STEER-ABORT -> step-boundary handler stops
        model = _SteeringModel(
            ONE_STEP_PLAN,
            {1: [[FIX, FIX, "SUBMIT"]]},
            inject_at={2: ("stop this is wrong", "abort")},
        )
        set_call_model(model)
        task = _make_task(
            tmp_path, config={"agent_tests": False, "self_critique": False}
        )
        model.bind(tmp_path / "logs" / task.task_id, task.task_id)
        result = _run_quietly(tmp_path, task)
        assert result.status == "failed"
        trace = _Trace(tmp_path / "logs", task.task_id)
        assert any(
            "aborted" in str(e.get("data", {}).get("note", "")).lower()
            for e in trace.of("result")
        )
        d = tmp_path / "logs" / task.task_id
        assert (d / "state.json").is_file() and (d / "plan.json").is_file()
        assert (d / "work").is_dir()
        assert trace.of("steering_abort")
        assert current_phase(d) == "failed"
        # the fix DID land in work/ before the abort (progress kept)
        assert "len(values)" in (d / "work" / "numlib" / "mathutil.py").read_text(
            encoding="utf-8"
        )
        # resumable: state.json records progress, the journal is consumed
        assert model.injected and model.injected[0][0] is not None

    def test_replan_keeps_work_and_replaces_plan(self, tmp_path):
        """Task B: replan at a step boundary dismantles the remaining
        plan, re-plans WITH the steering visible, and KEEPS the work
        already in work/ (the new attempt does not roll back)."""
        two_step = [
            {"id": 1, "description": "fix mean()", "checkpoint": "target passes"},
            {"id": 2, "description": "unrelated cleanup", "checkpoint": "suite green"},
        ]
        replan_plan = [
            {"id": 1, "description": "finish the fix", "checkpoint": "target passes"}
        ]
        model = _SteeringModel(
            two_step,
            {1: [[FIX, "SUBMIT"]], 2: [["SUBMIT"]]},
            inject_at={1: ("focus only on mean", "replan")},
            planner_plans=[replan_plan],
        )
        set_call_model(model)
        task = _make_task(
            tmp_path, config={"agent_tests": False, "self_critique": False}
        )
        model.bind(tmp_path / "logs" / task.task_id, task.task_id)
        result = _run_quietly(tmp_path, task)
        assert result.status == "success"
        trace = _Trace(tmp_path / "logs", task.task_id)
        assert trace.of("steering_replan")
        assert trace.of("plan_replaced")
        # the re-plan prompt carried the steering (content receipt)
        replan_events = trace.of("plan_replaced")
        assert any("focus only on mean" in json.dumps(e) for e in replan_events)
        # work kept: the fix from before the replan is still there
        d = tmp_path / "logs" / task.task_id
        assert "len(values)" in (d / "work" / "numlib" / "mathutil.py").read_text(
            encoding="utf-8"
        )

    def test_final_gate_guide_blocks_success_minting(self, tmp_path):
        """Task C (THE guarantee): steering pending at the pre-mint
        checkpoint BLOCKS success minting — the verifier passed, but
        the user steered after the work; the attempt is poisoned and
        the fix is re-verified after incorporation. The injection fires
        during the self-critique call (the last model call before the
        mint — the exact window the pre-mint re-check closes)."""
        model = _SteeringModel(
            ONE_STEP_PLAN,
            {1: [[FIX, "SUBMIT"]]},
            # call sequence: 1=planner, 2=step turn 0 (FIX), 3=SUBMIT
            # turn, 4=self-critique — fire after 4 so the steering is
            # pending exactly at the pre-mint re-check (post-verify,
            # post-critique, pre-mint: the window the re-check closes)
            inject_at={4: ("also fix the docstring", "guide")},
        )
        # critique approves so the only thing that can block minting is
        # the steering itself
        model.critique_verdicts = [True, True]
        set_call_model(model)
        task = _make_task(tmp_path, config={"agent_tests": False})
        model.bind(tmp_path / "logs" / task.task_id, task.task_id)
        result = _run_quietly(tmp_path, task)
        trace = _Trace(tmp_path / "logs", task.task_id)
        steer_events = trace.of("steering")
        # consumed at the pre-mint checkpoint (the second consume point)
        assert any("pre-mint" in json.dumps(e) for e in steer_events), [
            e.get("data") for e in steer_events
        ]
        # success was NOT minted on the steered attempt: the loop
        # poisoned it and re-verified after incorporation
        assert result.attempts >= 2
        assert result.status == "success"
        # the deferred decision is on the record (state.json carries it)
        d = tmp_path / "logs" / task.task_id
        state_txt = (d / "state.json").read_text(encoding="utf-8")
        assert "success minting deferred" in state_txt

    def test_off_arm_never_polls_the_journal(self, tmp_path):
        """steering_enabled=False is the OFF arm: an injected abort
        does nothing (exactly one code path — the buffer is None), and
        the run finishes honestly as if nothing was typed."""
        model = _SteeringModel(
            ONE_STEP_PLAN,
            {1: [[FIX, "SUBMIT"]]},
            inject_at={2: ("abort everything", "abort")},
        )
        set_call_model(model)
        task = _make_task(
            tmp_path,
            config={
                "steering_enabled": False,
                "agent_tests": False,
                "self_critique": False,
            },
        )
        model.bind(tmp_path / "logs" / task.task_id, task.task_id)
        result = _run_quietly(tmp_path, task)
        assert result.status == "success"  # the abort was never seen
        assert model.injected and model.injected[0][0] is not None
        # the journal sits unconsumed (nobody polled it)
        journal = tmp_path / "logs" / task.task_id / "steering.jsonl"
        ops = [
            json.loads(l)["op"]
            for l in journal.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert "inject" in ops
        assert "consume" not in ops

    def test_resume_after_steering_keeps_instruction_pending(self, tmp_path):
        """Task C (resume): an inject that arrived before a crash is
        STILL PENDING on the resumed run (the journal replays)."""
        d = tmp_path / "logs" / "fix-resume01"
        pre = steering_mod.SteeringBuffer(d, "fix-resume01")
        ev = pre.inject("only touch mathutil.py", intent="guide")
        assert ev is not None
        # a fresh consumer (the resumed run's loop) replays the journal
        resumed_buf = steering_mod.SteeringBuffer(d, "fix-resume01")
        assert [e.text for e in resumed_buf.pending()] == ["only touch mathutil.py"]


def _run_quietly(tmp_path, task):
    """run_task with stderr/stdout noise suppressed (the loop's honest
    trace renders status lines; tests read structured artifacts)."""
    from harness.core import run_task

    return run_task(task, log_root=tmp_path / "logs")
