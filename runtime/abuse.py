"""Round 6, Terminal 3, Task B — ADVERSARIAL cost/resource-abuse suite.

Tests what happens when a task MISBEHAVES, deliberately triggering the
worst case for every budget/limit the runtime owns — not "the config
exists" but "the cap actually stops the run" measured against:

  1. retry-loop model    — every model call 429s forever; the router's
                           backoff must stay BOUNDED (config: rate_limit_
                           retries/backoff) and the task must end within
                           the wall-clock cap, not hang forever.
  2. cost overshoot      — the harness checks budget BETWEEN attempts,
                           so ONE expensive call can overshoot budget_cap_
                           usd. Measures the overshoot RATIO (worst single
                           call vs the cap) — the honest bound is
                           cap + one call, never unbounded.
  3. escalation storm    — adversarial conversation tails (stack traces,
                           failing-test output) engineered to maximize the
                           difficulty predictor's struggle score; counts
                           hard-tier calls under adaptive routing. Bounded
                           false escalation = the ON-arm safety property.
  4. runaway command     — a step bash command that runs forever
                           (sleep 9900); command_timeout_s + the harness
                           deadline + the scheduler wall-clock cap must
                           each hold their layer.
  5. crash-loop worker   — a worker that hard-dies instantly, over and
                           over; crash_retries must exhaust into a
                           terminal result, never an infinite respawn.
  6. pre-state hang      — worker hung BEFORE any state.json write (the
                           hang check's blind spot): only the wall-clock
                           kill applies — verify it fires and the run
                           still terminates.

Design (mirrors runtime/stress.py conventions):
  - Drives the REAL harness.core.run_task in REAL scheduler workers via
    Task.config["use_mock_provider"] + mock_script (the worker-process
    path; callables can't cross process boundaries). Real Docker bash +
    real pytest verify — only the MODEL is hostile/deterministic.
  - Scenarios run SEQUENTIALLY (each is a full load profile against the
    shared Docker VM — same reasoning as stress.py).
  - Verdicts are measured: wall-clock-vs-cap, cost-vs-cap, call counts
    from the per-task model_ledger.jsonl, kill events from the scheduler
    journal — then asserted. A report lands under logs/abuse/<ts>/.
  - NOT part of the pytest suite (spawns real workers + Docker; several
    scenarios deliberately take minutes to hit their caps — that is the
    measurement).

Usage: python -m runtime.abuse [--scenario all|retryloop|overshoot|
        escalate|runaway|crashloop|prestate] [--out logs/abuse/<ts>]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from runtime.scheduler import run as scheduler_run
from shared.types import Task

REPO_ROOT = Path(__file__).resolve().parents[1]

# The smoke fixture: a tiny buggy repo whose suite is instant (no Docker
# image builds beyond the shared base; pytest is baked in). Same shape as
# tests/fixtures/bug02_mean but synthesized locally so the abuse suite
# owns its content (run-state, not T1's test surface — ablation_tasks
# convention).
_ABUSE_REPO_FILES = {
    "pyproject.toml": (
        "[project]\n"
        'name = "abuselib"\n'
        'version = "0.1.0"\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    ),
    "abuselib/__init__.py": "",
    "abuselib/mathutil.py": (
        "def mean(values):\n"
        '    """Arithmetic mean."""\n'
        "    if not values:\n"
        "        return 0\n"
        "    return sum(values) / (len(values) - 1)\n"  # the bug
    ),
    "tests/test_mathutil.py": (
        "from abuselib.mathutil import mean\n"
        "\n"
        "\n"
        "def test_mean_two():\n"
        "    assert mean([2, 4]) == 3\n"
    ),
}


def _build_repo(root: Path) -> Path:
    """Materialize the tiny buggy repo; returns its path (idempotent)."""
    repo = root / "abuselib"
    if not repo.exists():
        for rel, content in _ABUSE_REPO_FILES.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return repo


# ---------------------------------------------------------------------------
# Scenario task builders. All use the REAL harness + real Docker sandbox +
# real pytest verify; mock_script drives the model deterministically
# (worker-process safe — runtime.mock_provider.install_script).
# ---------------------------------------------------------------------------


def _base_cfg(logs_root: Path, **over: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "use_mock_provider": True,
        "test_command": "python -m pytest -q",
        "target_test": "tests/test_mathutil.py::test_mean_two",
        "verify_timeout_s": 120,
        "command_timeout_s": 60,
        "max_step_turns": 6,
        "max_retries": 2,
        "budget_cap_usd": 3.0,
        "crash_retries": 1,
        "resume": True,
        "hang_heartbeat_stale_s": 90.0,
        "log_root": str(logs_root),
    }
    cfg.update(over)
    return cfg


def _plan() -> List[Dict[str, Any]]:
    return [
        {
            "id": 1,
            "description": "fix the mean function",
            "checkpoint": "target test passes",
            "files_hint": [],
        }
    ]


def _scripted_task(
    task_id: str, repo: Path, logs_root: Path, step_cmds: List[str], **cfg_over: Any
) -> Task:
    """One REAL-harness task whose model is scripted with `step_cmds`."""
    script = {"plan": _plan(), "scripts": {1: list(step_cmds)}}
    cfg = _base_cfg(logs_root, mock_script=script, **cfg_over)
    return Task(
        task_id=task_id,
        repo_path=str(repo),
        issue_text="mean() divides by len-1; should divide by len",
        config=cfg,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def _run_one(
    task: Task, out: Path, run_id: str, concurrency: int = 1
) -> Dict[str, Any]:
    """Run one task through the real scheduler; returns measured outcome
    (wall, result status, ledger cost, call count, journal events).

    Isolation: any stale .runtime dir from a PRIOR invocation of the
    same task_id (same --out) is wiped first — the model ledger APPENDS
    across runs, and a stale ledger would poison this run's cost
    measurement (found live: the second overshoot run's ledger carried
    the first run's $3.36). Also wipes the harness-side logs/{task_id}
    dir so _fresh_paths never archives stale runs mid-suite.
    """
    import shutil

    for stale in (out / f"{task.task_id}.runtime", out / task.task_id):
        if stale.exists():
            shutil.rmtree(stale, onerror=lambda f, p, e: None)
    t0 = time.time()
    results = scheduler_run(
        [task], concurrency=concurrency, logs_root=str(out), run_id=run_id
    )
    wall = round(time.time() - t0, 1)
    res = results.get(task.task_id)
    ledger_path = out / f"{task.task_id}.runtime" / "model_ledger.jsonl"
    ledger: List[Dict[str, Any]] = []
    if ledger_path.exists():
        ledger = [
            json.loads(l)
            for l in ledger_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    journal_path = out / run_id / "events.jsonl"
    events: List[str] = []
    if journal_path.exists():
        events = [
            json.loads(l).get("event", "")
            for l in journal_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    return {
        "wall_s": wall,
        "status": getattr(res, "status", None),
        "attempts": getattr(res, "attempts", None),
        "ledger_calls": len(ledger),
        "ledger_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in ledger), 6),
        "ledger_models": sorted({r.get("model") for r in ledger}),
        "events": events,
        "result_cost_usd": getattr(res, "cost_usd", None),
    }


def scenario_retryloop(out: Path, repo: Path) -> Dict[str, Any]:
    """A model endpoint that 429s FOREVER. The router must exhaust its
    bounded backoff (rate_limit_retries=2, base 2s -> 2s+4s then give up)
    and the task must terminate with a model-error status — quickly,
    not after unbounded sleeping. Uses REAL network calls against a
    localhost TCP endpoint that always returns 429 JSON (a real HTTP
    429, not a mock — the router's retry path is exercised for real)."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server API
            body = (
                b'{"error": {"message": "Too many requests", '
                b'"type": "rate_limit_error", "code": "429"}}'
            )
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        task = Task(
            task_id="abuse-retryloop",
            repo_path=str(repo),
            issue_text="mean() divides by len-1; should divide by len",
            config={
                # REAL harness, REAL (local) endpoint, NO mock provider —
                # the whole point is the router's real retry path.
                "use_mock_provider": False,
                "provider": "openai",
                "model": "abuse-model",
                "api_key": "k",
                "api_base": f"http://127.0.0.1:{port}/v1",
                "test_command": "python -m pytest -q",
                "target_test": "tests/test_mathutil.py::test_mean_two",
                "verify_timeout_s": 120,
                "command_timeout_s": 60,
                "max_step_turns": 6,
                "max_retries": 1,
                "budget_cap_usd": 3.0,
                "crash_retries": 0,
                "resume": False,
                "hang_heartbeat_stale_s": 90.0,
                "log_root": str(out / "tasklogs"),
                # bounded backoff: 2 retries at 2s base -> ~2+4=6s worst
                "rate_limit_retries": 2,
                "rate_limit_backoff_s": 2.0,
                "max_wallclock_s": 300.0,
            },
        )
        return _run_one(task, out / "tasklogs", "run-abuse-retryloop")
    finally:
        server.shutdown()


def scenario_overshoot(out: Path, repo: Path) -> Dict[str, Any]:
    """Cost-cap overshoot: budget_cap_usd is tiny ($0.10) and every model
    reply is a HUGE (valid-plan-shaped) completion, so each call carries a
    large mock-accounted cost. The harness checks over_budget() at attempt
    START (core.py), so one in-attempt session of calls can burn past the
    cap before the next check — the measurement is final cost vs cap; the
    honest bound is cap + one attempt's worth of calls (max_step_turns),
    never unbounded. Parent-process mock state is invisible to worker
    subprocesses (worker installs from task config), so the giant
    response rides as mock_responses: {model: giant_valid_plan}."""
    pad = "x" * 30000  # giant completion -> ~7.5k completion tokens/call
    giant_plan = json.dumps(
        {
            "analysis": pad[:4000],
            "plan": [
                {
                    "id": 1,
                    "description": "fix the mean function",
                    "checkpoint": "target test passes",
                    "files_hint": [],
                }
            ],
            "padding": pad,  # valid JSON key; huge -> huge completion tokens
        }
    )
    # 'gpt-4o' is in model_router._PRICES ($2.50/$10 per 1M tok) — the
    # mock-accounted cost per giant call is real-priced so the budget
    # check has something to trip on.
    cfg = _base_cfg(
        out / "tasklogs",
        mock_responses={"gpt-4o": giant_plan},
        model="gpt-4o",  # explicit: every call giant+priced
        budget_cap_usd=0.10,  # TINY cap: the abuse condition
        max_retries=3,
        max_wallclock_s=300.0,
        crash_retries=0,
        resume=False,
    )
    cfg.pop("mock_script", None)
    cfg["use_mock_provider"] = True
    # no target pass possible: the "step commands" are the giant plan
    # text -> command extraction fails -> the model-call path keeps
    # burning giant replies until budget/turns stop it.
    task = Task(
        task_id="abuse-overshoot",
        repo_path=str(repo),
        issue_text="mean() divides by len-1; should divide by len",
        config=cfg,
    )
    return _run_one(task, out / "tasklogs", "run-abuse-overshoot")


def scenario_escalate(out: Path, repo: Path) -> Dict[str, Any]:
    """Escalation storm: the issue text + feedback tails are engineered
    to maximize the difficulty predictor's score (stack traces, race
    wording, many burned turns). Under adaptive routing the router may
    escalate to the expensive tier; the measurement is how MANY hard
    calls result vs total calls — bounded escalation, never all-hard,
    and the task must still terminate. The scripted mock (installed by
    the WORKER from mock_script) ignores the model argument — routing
    still happens per call and the per-call LEDGER records the chosen
    tier, which is what we measure."""
    scary_issue = (
        "Intermittent race in mean(): concurrent callers crash with a "
        "Traceback (most recent call last) and ValueError. Flaky, "
        "timing-dependent, memory leak suspected; possibly a deadlock. "
        "Regression in the timing/interpolation path; reproduce under "
        "concurrent load. Encoding/unicode issues may be involved."
    )
    # scripts: burn turns with failing pytest runs so the struggle signal
    # accumulates in the conversation tail (real failing-test output)
    burn = [
        "python -m pytest -q tests/test_mathutil.py::test_mean_two",
        "python -m pytest -q tests/test_mathutil.py::test_mean_two",
        "python -m pytest -q tests/test_mathutil.py::test_mean_two",
        "sed -n '1,20p' abuselib/mathutil.py",
    ]
    script = {"plan": _plan(), "scripts": {1: burn}}
    cfg = _base_cfg(
        out / "tasklogs",
        mock_script=script,
        adaptive_routing=True,
        model_tiers={
            "easy": {"provider": "openai", "model": "cheap-abuse-model"},
            "medium": {"provider": "openai", "model": "cheap-abuse-model"},
            "hard": {"provider": "openai", "model": "expensive-abuse-model"},
        },
        max_retries=1,
        max_wallclock_s=300.0,
    )
    task = Task(
        task_id="abuse-escalate",
        repo_path=str(repo),
        issue_text=scary_issue,
        config=cfg,
    )
    m = _run_one(task, out / "tasklogs", "run-abuse-escalate")
    # re-read the ledger WITH model detail for the verdict
    ledger_path = out / "tasklogs" / "abuse-escalate.runtime" / "model_ledger.jsonl"
    if ledger_path.exists():
        m["_ledger_detail"] = [
            json.loads(l)
            for l in ledger_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    else:
        m["_ledger_detail"] = []
    return m


def scenario_runaway(out: Path, repo: Path) -> Dict[str, Any]:
    """A step bash command that runs forever ('sleep 9900'). Layers that
    must each hold: command_timeout_s (sandbox kills the container),
    the harness deadline (mid-step check), the scheduler wall-clock cap.
    Measurement: total wall vs the configured caps; no orphan container
    residue."""
    task = _scripted_task(
        "abuse-runaway",
        repo,
        out / "tasklogs",
        ["sleep 9900", "sleep 9900"],
        command_timeout_s=20,  # sandbox layer bound (fast for the test)
        max_retries=1,
        max_wallclock_s=120.0,
        hang_heartbeat_stale_s=60.0,
    )
    return _run_one(task, out / "tasklogs", "run-abuse-runaway")


def scenario_crashloop(out: Path, repo: Path) -> Dict[str, Any]:
    """Crash-loop exhaustion: a worker that hard-dies (os._exit) mid-task.
    The full anti-crash-loop chain is two mechanisms and this scenario
    exercises BOTH halves back-to-back as separate tasks:
      (a) 'abuse-crashloop0': crash_retries=0 — the FIRST crash must go
          straight to a terminal 'error' result (no requeue at all);
          journal: crash -> crash_exhausted, exactly 1 spawn.
      (b) 'abuse-crashloopN': crash_retries=2 — the relaunch path: the
          worker DISARMS the fault injection on relaunch (one-shot by
          design, runtime/worker.py) and finishes via resume — proving
          a same-crash-point relaunch can never loop forever (the
          disarm IS the loop breaker); journal: crash -> crash_retry ->
          finish(success), 2 spawns.
    A perpetual same-crash respawn is impossible by construction — that
    impossibility is what (b) demonstrates and (a) bounds the other way
    (zero-budget crash -> terminal, never lost)."""
    outcomes: Dict[str, Any] = {}
    for tid, retries in (("abuse-crashloop0", 0), ("abuse-crashloopN", 2)):
        cfg = {
            "use_fake_harness": True,  # deterministic instant-crash harness
            "fake_crash_step": "s1",  # os._exit(70) at step 1
            "fake_steps": ["s1", "s2"],
            "fake_state_dir": str(out / "tasklogs" / tid),
            "resume_dir": str(out / "tasklogs" / f"{tid}.runtime"),
            "log_root": str(out / "tasklogs"),
            "crash_retries": retries,
            "resume": True,
            "max_wallclock_s": 300.0,
            "hang_heartbeat_stale_s": 90.0,
            "budget_cap_usd": 3.0,
        }
        task = Task(task_id=tid, repo_path="", issue_text="crash loop", config=cfg)
        outcomes[tid] = _run_one(task, out / "tasklogs", f"run-{tid}")
    # merge into a single measurement for the verdict
    first = outcomes["abuse-crashloop0"]
    second = outcomes["abuse-crashloopN"]
    merged = {
        "status": first["status"],
        "wall_s": round(first["wall_s"] + second["wall_s"], 1),
        "events": first["events"] + second["events"],
        "zero_budget": {
            "spawns": first["events"].count("spawn"),
            "exhausted": first["events"].count("crash_exhausted"),
            "status": first["status"],
        },
        "with_retries": {
            "spawns": second["events"].count("spawn"),
            "retries": second["events"].count("crash_retry"),
            "finished": second["events"].count("finish"),
            "status": second["status"],
        },
    }
    return merged


def scenario_prestate(out: Path, repo: Path) -> Dict[str, Any]:
    """Worker hung BEFORE any state.json write (planner call that never
    returns). The state-stale hang check has no file to look at; only
    the scheduler's wall-clock cap bounds this. The scripted-mock path
    can't sleep (scripted dispatch is instant), so this drives the REAL
    network path against a localhost endpoint that ACCEPTS the request
    and never responds (a hung model call, for real)."""
    import socket
    import threading

    # a TCP server that accepts and never replies = model call hangs
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    port = sock.getsockname()[1]

    def _hold() -> None:
        while True:
            try:
                conn, _ = sock.accept()
                # hold the connection open; never read, never respond
                threading.Thread(
                    target=lambda c: None, args=(conn,), daemon=True
                ).start()
            except OSError:
                return

    threading.Thread(target=_hold, daemon=True).start()
    try:
        cfg = _base_cfg(
            out / "tasklogs",
            max_wallclock_s=90.0,
            hang_heartbeat_stale_s=30.0,
            crash_retries=0,
            resume=False,
            use_mock_provider=False,
            provider="openai",
            model="abuse-hang-model",
            api_key="k",
            api_base=f"http://127.0.0.1:{port}/v1",
            command_timeout_s=60,
        )
        cfg.pop("mock_script", None)
        task = Task(
            task_id="abuse-prestate",
            repo_path=str(repo),
            issue_text="mean() divides by len-1; should divide by len",
            config=cfg,
        )
        return _run_one(task, out / "tasklogs", "run-abuse-prestate")
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Verdicts: measured assertions per scenario
# ---------------------------------------------------------------------------


def _verdict_retryloop(m: Dict[str, Any]) -> Dict[str, Any]:
    """Task must terminate (any status) within a few x the bounded
    backoff budget: 2 retries * (2s + 4s) = ~6s sleeping + call overhead
    per call, and the planner call is the first to die. Expected: error
    status (model call failed), wall << wall-clock cap."""
    cap = 300.0
    bounded_backoff = 2.0 + 4.0  # rate_limit_retries=2, base 2s
    ok = m["status"] in ("error", "failed") and m["wall_s"] < min(
        cap, bounded_backoff * 20
    )
    return {
        "pass": ok,
        "why": f"status={m['status']} wall={m['wall_s']}s "
        f"(bounded backoff ~{bounded_backoff}s, wall cap {cap}s)",
        "wall_s": m["wall_s"],
        "status": m["status"],
    }


def _verdict_overshoot(m: Dict[str, Any]) -> Dict[str, Any]:
    """Final cost must be bounded by cap + ~one attempt's worth of
    overshoot (the harness checks over_budget() at attempt START, so a
    single in-attempt session of calls can burn past the cap before the
    next check — the honest bound is cap + max_step_turns calls, NOT
    unbounded). With per-call cost ~$0.09 and cap $0.10: attempt 1 is
    allowed to burn up to its turns; attempt 2's START check must trip.
    Expected: status failed('budget cap exceeded'), final cost bounded
    to roughly one attempt (~7x cap here) — the MEASURED ratio is the
    deliverable."""
    cap = 0.10
    cost = m["ledger_cost_usd"] or m["result_cost_usd"] or 0.0
    ratio = round(cost / cap, 2) if cost else None
    one_attempt = 7.0 * cap  # ~6-7 calls/attempt at ~$0.09 each
    ok = (
        m["status"] == "failed"
        and ratio is not None
        and cost <= cap + one_attempt * 1.5
    )
    return {
        "pass": ok,
        "overshoot_ratio": ratio,
        "final_cost_usd": round(cost, 6),
        "cap_usd": cap,
        "bound": f"cap + one attempt (~{one_attempt / cap:.0f}x)",
        "why": f"status={m['status']} cost/cap={ratio}x — budget "
        f"check at next attempt start stops the burn; "
        f"overshoot is one-attempt-bounded, never unbounded",
    }


def _verdict_escalate(m: Dict[str, Any]) -> Dict[str, Any]:
    """Under the engineered scary text, hard-tier calls must be a MINORITY
    of total (bounded false escalation): the predictor may escalate under
    struggle evidence, but the cheap tier still carries most calls; and
    the task must terminate with a status."""
    led = m.get("_ledger_detail", [])
    total = len(led)
    hard = sum(1 for r in led if "expensive" in str(r.get("model")))
    frac = round(hard / total, 2) if total else None
    ok = m["status"] is not None and total > 0 and hard <= total * 0.5
    return {
        "pass": ok,
        "total_calls": total,
        "hard_calls": hard,
        "hard_fraction": frac,
        "why": f"hard/total={hard}/{total} "
        f"(bounded false escalation under adversarial text)",
    }


def _verdict_runaway(m: Dict[str, Any]) -> Dict[str, Any]:
    """The sandbox's command_timeout_s (20s) kills the sleep; the harness
    deadline + wall-clock cap (120s) bound the whole task. Wall must be
    well under the wall cap; a leftover 'sleep' container must not
    survive (checked via docker in the caller)."""
    cap = 120.0
    ok = m["wall_s"] < cap * 1.5 and m["status"] is not None
    return {
        "pass": ok,
        "wall_s": m["wall_s"],
        "wall_cap_s": cap,
        "status": m["status"],
        "why": f"wall={m['wall_s']}s vs cap {cap}s; "
        f"command layer killed the sleep at 20s",
    }


def _verdict_crashloop(m: Dict[str, Any]) -> Dict[str, Any]:
    """Both halves of the anti-crash-loop chain:
    (a) zero-budget: exactly 1 spawn, 1 crash_exhausted, terminal error;
    (b) with retries: 2 spawns, 1 crash_retry, relaunch finishes success
        (the injection disarm is the loop-breaker). Together: a crashing
        worker either exhausts into a terminal result or resumes and
        finishes — never an infinite respawn."""
    zb = m.get("zero_budget", {})
    wr = m.get("with_retries", {})
    ok = (
        zb.get("spawns") == 1
        and zb.get("exhausted") == 1
        and zb.get("status") == "error"
        and wr.get("spawns") == 2
        and wr.get("retries") == 1
        and wr.get("finished") == 1
        and wr.get("status") == "success"
    )
    return {
        "pass": ok,
        "zero_budget": zb,
        "with_retries": wr,
        "wall_s": m.get("wall_s"),
        "why": f"zero-budget crash -> terminal error (1 spawn); "
        f"with-retries crash -> disarm+resume success "
        f"(2 spawns) — no infinite respawn path exists",
    }


def _verdict_prestate(m: Dict[str, Any]) -> Dict[str, Any]:
    """A worker stuck in a never-returning planner call (pre-plan) must
    be ended by a supervision kill and the run must terminate with the
    terminal timeout status, bounded by hang_heartbeat_stale_s (the
    state-stale hang check fires on the never-refreshed state.json the
    harness writes at task start) — never an infinite hang. crash_
    retries=0: the first kill is terminal."""
    events = m["events"]
    hang_kills = events.count("hang_timeout")
    wc_kills = events.count("wallclock_timeout")
    ok = (
        m["status"] == "timeout"
        and (hang_kills + wc_kills) >= 1
        and m["wall_s"] < 180.0
    )
    return {
        "pass": ok,
        "hang_kills": hang_kills,
        "wallclock_kills": wc_kills,
        "status": m["status"],
        "wall_s": m["wall_s"],
        "why": f"supervision killed the pre-plan hang "
        f"(hang={hang_kills}, wallclock={wc_kills}, "
        f"wall={m['wall_s']}s, status={m['status']})",
    }


# ---------------------------------------------------------------------------

_SCENARIOS = {
    "retryloop": (scenario_retryloop, _verdict_retryloop),
    "overshoot": (scenario_overshoot, _verdict_overshoot),
    "escalate": (scenario_escalate, _verdict_escalate),
    "runaway": (scenario_runaway, _verdict_runaway),
    "crashloop": (scenario_crashloop, _verdict_crashloop),
    "prestate": (scenario_prestate, _verdict_prestate),
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="runtime.abuse")
    ap.add_argument("--scenario", default="all", choices=("all",) + tuple(_SCENARIOS))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "abuse" / ts)
    out.mkdir(parents=True, exist_ok=True)
    repo = _build_repo(out)
    names = list(_SCENARIOS) if args.scenario == "all" else [args.scenario]

    report: Dict[str, Any] = {"ts": ts, "scenarios": {}, "all_pass": True}
    for name in names:
        run_fn, verdict_fn = _SCENARIOS[name]
        print(f"[abuse:{name}] running...")
        t0 = time.time()
        try:
            m = run_fn(out, repo)
        except Exception as exc:  # noqa: BLE001 — report, don't die
            m = {
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "wall_s": round(time.time() - t0, 1),
            }
        v = verdict_fn(m)
        entry = {
            "measurement": {
                k: w for k, w in m.items() if k not in ("events", "_ledger_detail")
            },
            "verdict": v,
            "pass": v.get("pass", False),
        }
        # keep the raw detail for the escalation fraction
        if name == "escalate":
            entry["measurement"]["ledger_detail"] = m.get("_ledger_detail", [])
        report["scenarios"][name] = entry
        report["all_pass"] = report["all_pass"] and entry["pass"]
        print(f"[abuse:{name}] {'PASS' if entry['pass'] else 'FAIL'} — {v.get('why')}")

    # container-residue check across the whole suite (scoped to hexec-*)
    try:
        import subprocess as sp

        r = sp.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        residue = [n for n in r.stdout.split() if n.startswith("hexec-")]
        report["container_residue"] = residue
        report["all_pass"] = report["all_pass"] and not residue
        print(f"[abuse] container residue: {residue or 'none'}")
    except (OSError, sp.TimeoutExpired):
        report["container_residue"] = "docker-unavailable"

    (out / "abuse_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nresults: {out / 'abuse_report.json'}")
    print("ALL SCENARIOS:", "PASS" if report["all_pass"] else "FAIL")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
