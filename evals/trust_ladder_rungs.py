"""T5.W1.1 - the executable probes behind Trust Ladder rungs #8, #9 and #10.

WHY THIS IS A SEPARATE MODULE
------------------------------
``evals/trust_ladder.py`` is the scorecard: ten rungs, four statuses, a table
and a JSON document. This module is the machinery under three of those rungs,
kept apart so the scorecard stays readable and so each probe can be driven
directly by a test with a **deliberately broken** input.

A rung that cannot fail is not a rung. Every probe here therefore takes its
guarantee's *mechanism* as an injectable argument, so a test can remove the
mechanism and assert the probe reports ``fail`` rather than ``pass``. The
three probes are:

``probe_rung8``
    Rung #8, "stays coherent past 50 turns". Three sub-checks, because the
    brief is right that "the run stops" is the *handled* failure and the real
    one is silent degradation:

    1. a real >=50-turn run in which every turn makes a measurable edit;
    2. **constraint decay** - a constraint planted at turn 2 must still be in
       the model's most-recent message at turn 45, and behaviour is driven
       *by that visibility* so "still governs behaviour" is an observed
       consequence rather than a substring search;
    3. the declared turn ceiling, and whether the cap is reported **before**
       it binds.

``probe_rung9``
    Rung #9, "fast enough to use daily". Four budgets in one honest
    vocabulary. **Every percentile is published with its window size**, and a
    window smaller than 20 is reported as ``median`` rather than ``p95`` -
    a p95 over 3 samples is not a p95.

``probe_rung10``
    Rung #10, "you can always see what it did and why". The chain
    ``journal row -> traceview -> runview -> TUI card`` on a REAL run, plus
    the two honest-presentation obligations: a truncated search and an
    ``unavailable`` field must be **visibly marked in the rendered view**,
    not merely present in the data.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
Every model call in all three probes is a scripted double. No provider is
contacted, no credential is read, no Docker daemon is required. Nothing here
is a claim about model quality, model cost or model latency - it measures
the SHELL, the retrieval path, and the projection chain.
"""

from __future__ import annotations

import io
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Windows PowerShell 5.1's console is cp1252 and raises on a box-drawing
#: character. Every rendered view in this module is captured, not printed, but
#: the rung has to be able to print its own evidence, so the encoding is fixed
#: at the boundary rather than left to the terminal.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# --------------------------------------------------------------------------
# the machine, named with every measurement
# --------------------------------------------------------------------------


def machine_label() -> str:
    """Return a short, stable description of the host a number came from.

    A performance claim without this is a claim about nothing in particular.
    Deliberately excludes the OS *build* number and the hostname, both of
    which change per machine and make two runs' receipts incomparable.
    """
    return (
        f"{platform.system()} {platform.machine()} / "
        f"cpus={os.cpu_count()} / python {platform.python_version()}"
    )


# --------------------------------------------------------------------------
# the honest-statistics vocabulary
# --------------------------------------------------------------------------

#: Below this many samples a percentile is not a percentile. The brief's rule
#: - "a p95 over 3 samples is not a p95" - is enforced here rather than left to
#: the reader's discretion: the function downgrades the *name*, never the
#: value, and always reports the window it had.
MIN_SAMPLES_FOR_P95 = 20

#: The three sample counts the contract permits a caller to request. Named so
#: the report can publish the run count next to the number.
RUN_COUNTS: Tuple[int, ...] = (5, 20, 50)


def percentile(samples: Sequence[float], q: float) -> Optional[float]:
    """Return the ``q``-th percentile, or ``None`` with no samples.

    ``None`` and ``0.0`` are different answers and the caller must not
    conflate them, so an empty window is ``None``.
    """
    ordered = sorted(float(s) for s in samples)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (float(q) / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


@dataclass(frozen=True)
class Timing:
    """One measured duration series, with the honesty fields attached.

    :param name: what was timed, in the vocabulary of the product
        (``startup_to_first_token`` and not ``t1``).
    :param unit: ``"ms"`` or ``"s"``. Carried so a reader never has to guess.
    :param samples: the raw observations, in collection order.
    :param target: the budget, or ``None`` when the metric has no target.
    :param available: ``False`` when the metric could not be measured at all.
        An unavailable metric is NOT a zero, and NOT a pass - ``reason``
        then says why. This is ``DOCTRINE.md`` §1's "render an absent value
        as 0" rule applied to a measurement.
    """

    name: str
    unit: str
    samples: Tuple[float, ...] = ()
    target: Optional[float] = None
    available: bool = True
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def window(self) -> int:
        """The run count. Published with every percentile, without exception."""
        return len(self.samples)

    def stat(self, q: float) -> Optional[float]:
        """Return the ``q``-th percentile of the window."""
        return percentile(self.samples, q)

    @property
    def named_stat(self) -> str:
        """The name this window's headline number is allowed to carry.

        A window of fewer than :data:`MIN_SAMPLES_FOR_P95` samples is reported
        as a **median**, because calling it a p95 would be the single easiest
        lie in a performance report. The value is unchanged; only the label
        is downgraded, and the window is published next to it either way.
        """
        return "median" if self.window < MIN_SAMPLES_FOR_P95 else "p95"

    @property
    def headline(self) -> Optional[float]:
        """The number a reader will quote. Always carries :attr:`named_stat`."""
        if not self.available or not self.samples:
            return None
        return (
            self.stat(95)
            if self.named_stat == "p95"
            else statistics.median(self.samples)
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "metric": self.name,
            "unit": self.unit,
            "available": self.available,
            "window": self.window,
            "statistic": self.named_stat,
            "machine": machine_label(),
        }
        if not self.available:
            # `value` is deliberately ABSENT, not null and not 0. A consumer
            # that reads `value` without reading `available` gets a KeyError,
            # which is the correct outcome for a number nobody took.
            out["reason"] = self.reason
            out["reported_as"] = "unavailable"
            return out
        out["value"] = self.headline
        out["min"] = min(self.samples)
        out["max"] = max(self.samples)
        if self.window >= MIN_SAMPLES_FOR_P95:
            out["p50"] = self.stat(50)
            out["p95"] = self.stat(95)
        else:
            out["min"], out["max"] = out["min"], out["max"]
            out["percentiles_withheld"] = (
                f"window={self.window} < {MIN_SAMPLES_FOR_P95}; a percentile "
                f"over this window is not a percentile, so only the median, "
                f"min and max are reported"
            )
        if self.target is not None:
            out["target"] = self.target
            out["within_budget"] = bool(
                self.headline is not None and self.headline <= self.target
            )
        if self.extra:
            out.update(self.extra)
        return out

    def describe(self) -> str:
        """One line a human reads, carrying the window and the machine."""
        if not self.available:
            return f"{self.name}: unavailable ({self.reason})"
        return (
            f"{self.name}: {self.headline:.0f}{self.unit} "
            f"({self.named_stat} over {self.window} runs, min {min(self.samples):.0f}, "
            f"max {max(self.samples):.0f}) on {machine_label()}"
        )


# --------------------------------------------------------------------------
# a scripted model double - the same boundary every eval in this repo uses
# --------------------------------------------------------------------------


class ScriptedModel:
    """A queue/turn-driven Boundary-2 double. No provider, no credential.

    The ``**kw`` acceptance is deliberate and load-bearing: ``ModelClient``
    forwards ``effort`` to any boundary whose signature names it, and a double
    with a strict signature is how a whole eval matrix died once.
    """

    def __init__(self, script: Callable[[int, List[Dict[str, str]]], str]) -> None:
        self._script = script
        self.calls = 0
        self.seen: List[List[Dict[str, str]]] = []

    def __call__(self, messages: Any, **_kw: Any) -> str:
        self.calls += 1
        rows = [dict(m) for m in messages]
        self.seen.append(rows)
        return self._script(self.calls, rows)

    def get_last_usage(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": 0.0,
        }


def _last_user_message(rows: Sequence[Dict[str, str]]) -> str:
    """The most recent user message - the position a re-injection must reach."""
    for row in reversed(list(rows)):
        if str(row.get("role")) == "user":
            return str(row.get("content") or "")
    return ""


def _build_repo(root: Path) -> Path:
    """A small real repository: a module and a sibling to edit."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "base.py").write_text("X = 0\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# RUNG #8 - coherent past 50 turns
# --------------------------------------------------------------------------

#: The constraint the forgetting check plants. A token, so "is it still
#: present" is a decidable question rather than a judgement about prose.
CONSTRAINT_TOKEN = "CONSTRAINT-VEGA-7741"
CONSTRAINT_FORBIDDEN = "src/forbidden.py"

#: The turn the constraint is planted on, and the turn it is re-checked on.
#: 2 and 45 are the brief's numbers; the corpus puts compliance at 33% by
#: turn 16, so 45 is a real test and not a formality.
PLANT_TURN = 2
CHECK_TURN = 45


@dataclass
class Rung8Result:
    """Everything rung #8 measured, including the mechanism it measured with."""

    declared_turn_cap: Optional[int]
    turns_executed: int
    edits_applied: int
    distinct_edit_paths: int
    redo_count: int
    monotonic_progress: bool
    constraint_visible_turns: int
    constraint_visible_at_check: bool
    constraint_reinjection_present: bool
    violations: List[int]
    cap_reported_before_binding: bool
    cap_report_evidence: str
    turn_cap_source: str
    required_turn_cap: int = 50
    decay_turns_executed: int = 0
    failures: List[str] = field(default_factory=list)

    def derive_failures(self) -> List[str]:
        """Every sub-check that did not hold, each naming its own number.

        **The single authority for "does rung #8 fail".** Both
        :func:`probe_rung8` and the test fixtures call this, so a test that
        fabricates a measurement cannot also fabricate the verdict. A fixture
        that supplies its own ``failures`` list tests nothing: it would pass
        for any rule the fixture happened to encode, including a wrong one.

        The first version of this module had the rule inline in the probe and
        the fixture setting ``failures=[]``, and four of the five
        demonstrated rung-#8 breaks passed GREEN as a result.
        """
        out: List[str] = []
        if self.turns_executed < self.required_turn_cap:
            out.append(
                f"the run executed {self.turns_executed} turns; the guarantee "
                f"needs >= {self.required_turn_cap}"
            )
        if self.declared_turn_cap is None:
            out.append(
                f"the declared turn ceiling is not readable: {self.turn_cap_source}"
            )
        elif self.declared_turn_cap < self.required_turn_cap:
            out.append(
                f"the declared turn ceiling is {self.declared_turn_cap} at "
                f"{self.turn_cap_source}; the guarantee needs >= "
                f"{self.required_turn_cap}"
            )
        if not self.monotonic_progress:
            out.append(
                f"progress is not monotonic: turns={self.turns_executed} "
                f"edits={self.edits_applied} distinct={self.distinct_edit_paths} "
                f"redone={self.redo_count}"
            )
        if self.decay_turns_executed and self.decay_turns_executed < CHECK_TURN:
            out.append(
                f"the decay arm executed only {self.decay_turns_executed} "
                f"turns, so it never reached the turn-{CHECK_TURN} check point; "
                f"its result is NOT evidence of constraint decay, and neither "
                f"is the absence of a decay finding"
            )
        elif not self.constraint_visible_at_check:
            out.append(
                f"CONSTRAINT DECAY: the constraint planted at turn {PLANT_TURN} "
                f"was not in the model's most recent message at turn "
                f"{CHECK_TURN}. It was visible on "
                f"{self.constraint_visible_turns} turns, and "
                f"{len(self.violations)} turns wrote the forbidden path "
                f"({CONSTRAINT_FORBIDDEN}) as a result. The control arm shows "
                f"the mechanism DOES exist elsewhere in this tree (the core "
                f"fix loop re-states its block on every tool result), so the "
                f"gap is specific to the interactive agent path, not a "
                f"missing idea."
            )
        if not self.cap_reported_before_binding:
            out.append(
                "the turn cap is never reported BEFORE it binds: "
                + self.cap_report_evidence
            )
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declared_turn_cap": self.declared_turn_cap,
            "turns_executed": self.turns_executed,
            "edits_applied": self.edits_applied,
            "distinct_edit_paths": self.distinct_edit_paths,
            "redo_count": self.redo_count,
            "monotonic_progress": self.monotonic_progress,
            "constraint_visible_turns": self.constraint_visible_turns,
            "constraint_visible_at_check": self.constraint_visible_at_check,
            "constraint_reinjection_present": self.constraint_reinjection_present,
            "violation_turns": self.violations,
            "cap_reported_before_binding": self.cap_reported_before_binding,
            "cap_report_evidence": self.cap_report_evidence,
            "turn_cap_source": self.turn_cap_source,
            "failures": self.failures,
        }


def declared_turn_cap() -> Tuple[Optional[int], str]:
    """Read the SHIPPING turn ceiling, and say where the number came from.

    Read through ``harness.config.DEFAULTS`` rather than by importing a
    constant, so a second definition cannot disagree with the one the loop
    actually reads (``harness/agent_loop.py:1283`` and
    ``harness/agent_kernel/strategy.py:497`` both read this key).
    """
    try:
        from harness.config import DEFAULTS
    except Exception as exc:  # a broken import is a finding, not a zero
        return None, f"harness.config is not importable: {exc!r}"
    value = DEFAULTS.get("agent_max_turns")
    if not isinstance(value, int):
        return None, f"agent_max_turns is {value!r}, not an int"
    return int(value), "harness.config.DEFAULTS['agent_max_turns']"


def core_loop_reinjects_constraints() -> bool:
    """CONTROL ARM: does the core fix loop re-state a constraint every turn?

    The control the agent-loop measurement needs. If the re-injection block
    carries the issue text, then the mechanism exists in this tree and its
    absence on the interactive agent path is a specific, nameable gap rather
    than "this repo has no such mechanism anywhere".
    """
    try:
        from harness import prompts
    except Exception:
        return False
    try:
        block = prompts.render_constraint_reinjection(
            issue_text=f"a bug: {CONSTRAINT_TOKEN} {CONSTRAINT_FORBIDDEN}",
            plan=[{"id": 1, "description": "fix it", "checkpoint": "x"}],
            step_id=1,
            total_steps=1,
            completed=[],
            protected_paths=[],
        )
    except Exception:
        return False
    return CONSTRAINT_TOKEN in str(block)


def probe_rung8(
    *,
    turns: int = 55,
    required_turn_cap: int = 50,
    run_loop: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Rung8Result:
    """Run the real agent loop for >=50 turns and measure coherence.

    Host-only by construction: the model is a scripted double and no bash
    tool is dispatched, so there is no daemon, no network and no credential.

    **Two independent runs, not one.** The first run measures *monotonic
    progress* with every turn writing a distinct real file. The second measures
    *constraint decay*, with the scripted model deliberately violating the
    constraint whenever it cannot see it. Sharing one run double-counts a
    single defect as two: once the constraint decays, the decay arm writes the
    same forbidden path on every remaining turn, and the "progress is not
    monotonic" check then fires on the DECAY defect rather than on a progress
    defect. That was a real false positive in the first version of this
    function, and separating the runs is the fix.

    :param turns: how many editing turns to script. Must exceed
        :data:`CHECK_TURN` for the forgetting check to mean anything.
    :param required_turn_cap: the ceiling the guarantee requires. Injectable
        so the failing case is demonstrated by lowering it, not by editing
        another terminal's config.
    :param run_loop: the loop seam. Injectable for the failure demonstration;
        defaults to the real ``harness.agent_loop.run_agent_stepped``.

    Every check that fails is appended to :attr:`Rung8Result.failures` with
    the number it failed on, so the caller never has to re-derive the verdict
    and the report cannot report ``pass`` on a partial measurement.
    """
    cap_value, cap_source = declared_turn_cap()
    if run_loop is None:
        from harness.agent_loop import run_agent_stepped

        def run_loop(**kw: Any) -> Dict[str, Any]:  # type: ignore[misc]
            return dict(run_agent_stepped(**kw))

    if turns <= CHECK_TURN:
        raise ValueError(
            f"rung #8 needs turns > {CHECK_TURN} so the turn-{CHECK_TURN} "
            f"forgetting check is inside the window; got {turns}"
        )

    from harness import deps

    def _drive(
        tag: str, script: Callable[[int, List[Dict[str, str]]], str]
    ) -> Dict[str, Any]:
        temp_root = Path(tempfile.mkdtemp(prefix=f"neo-rung8-{tag}-"))
        repo = _build_repo(temp_root / "repo")
        log_root = temp_root / "logs"
        model = ScriptedModel(script)
        deps.set_call_model(model)
        try:
            result = run_loop(
                request=(
                    "Generate one module per turn. HARD CONSTRAINT: never "
                    f"create or {CONSTRAINT_FORBIDDEN} under any "
                    f"circumstances. {CONSTRAINT_TOKEN}"
                ),
                repo_path=str(repo),
                config={
                    "agent_strategy": "agent_step",
                    "steering_enabled": False,
                    "agent_max_turns": turns + 5,
                    "command_timeout_s": 30,
                    "max_output_chars": 3000,
                },
                log_root=log_root,
                task_id=tag,
            )
        finally:
            deps.reset_overrides()
        return _read_run(log_root, tag, result)

    # -- RUN A: monotonic progress, constraint held throughout -------------
    def progress_script(turn: int, _rows: List[Dict[str, str]]) -> str:
        if turn > turns:
            return json.dumps({"tool": "done", "answer": "done"})
        return json.dumps(
            {
                "tool": "write",
                "path": "src/gen%03d.py" % turn,
                "content": "V%d = %d\n" % (turn, turn),
            }
        )

    a = _drive("rung8-progress", progress_script)

    # -- RUN B: constraint decay, plant at 2, check at 45 ------------------
    visible: Dict[int, bool] = {}
    violations: List[int] = []

    def decay_script(turn: int, rows: List[Dict[str, str]]) -> str:
        sees = CONSTRAINT_TOKEN in _last_user_message(rows)
        visible[turn] = sees
        if turn > turns:
            return json.dumps({"tool": "done", "answer": "done"})
        # Behaviour is governed BY VISIBILITY. A constraint the model cannot
        # see in its most recent message does not govern it, and the run
        # shows that as a real forbidden write rather than as a failed string
        # search.
        if not sees and turn > PLANT_TURN:
            violations.append(turn)
            path = CONSTRAINT_FORBIDDEN
        else:
            path = "src/obs%03d.py" % turn
        return json.dumps(
            {"tool": "write", "path": path, "content": "V%d = %d\n" % (turn, turn)}
        )

    decay = _drive("rung8-decay", decay_script)
    # The decay arm's own turn count is a CONTROL, and it is asserted rather
    # than assumed: a run that stopped at turn 3 would report "the constraint
    # was not visible at turn 45" for entirely the wrong reason -- it never
    # reached turn 45 -- and that would read as a decay finding when it is a
    # run-length failure.
    decay_turns = decay["turns"]

    turns_executed = a["turns"]
    edits = a["edit_paths"]
    distinct = {p for p in edits if p}
    # Monotonic progress: every turn produced a real edit, no path was written
    # twice, so completed work was not redone.
    redo_count = len(edits) - len(distinct)
    monotonic = (
        turns_executed >= 1 and redo_count == 0 and len(edits) >= turns_executed - 1
    )

    # Does a JOURNAL ROW announce the cap while the run is still going?
    #
    # The first version of this check substring-searched every row for
    # "max_turns" and passed GREEN -- on the `task_start` row, whose
    # `data.config` echoes the whole merged config. That is the run
    # announcing its configuration at turn 0, which is not the same claim as
    # announcing that the cap is APPROACHING, and a check that passes for the
    # wrong reason is worse than one that fails. T1 built
    # `harness/turn_caps.py` for exactly this ("T4's rail and T5's rung-8 read
    # this"), so the bar is now a real receipt: a row that is not the start
    # event and that carries BOTH a cap value and a remaining/approaching
    # field.
    cap_before = False
    cap_evidence = (
        "no journal row announces the turn ceiling while the run is in "
        "progress. `task_start` echoes the merged config, which is the run "
        "stating its configuration at turn 0 -- not a cap being approached."
    )
    for row in a["rows"]:
        kind = str(row.get("kind") or "")
        if kind in ("task_start", "result", "task_end"):
            continue
        blob = json.dumps(row.get("data") or {}, default=str)
        has_cap = any(token in blob for token in ("max_turns", "turn_cap", "cap_turns"))
        has_remaining = any(
            token in blob
            for token in ("turns_remaining", "turns_left", "remaining", "approaching")
        )
        if has_cap and has_remaining:
            cap_before = True
            cap_evidence = (
                f"{kind} row carries a cap AND a remaining count: {blob[:200]}"
            )
            break

    control = core_loop_reinjects_constraints()
    at_check = bool(visible.get(CHECK_TURN))
    seen_count = sum(1 for v in visible.values() if v)

    measured = Rung8Result(
        declared_turn_cap=cap_value,
        turns_executed=turns_executed,
        edits_applied=len(edits),
        distinct_edit_paths=len(distinct),
        redo_count=redo_count,
        monotonic_progress=monotonic,
        constraint_visible_turns=seen_count,
        constraint_visible_at_check=at_check,
        constraint_reinjection_present=control,
        violations=violations,
        cap_reported_before_binding=cap_before,
        cap_report_evidence=cap_evidence,
        turn_cap_source=cap_source,
        required_turn_cap=required_turn_cap,
        decay_turns_executed=decay_turns,
    )
    measured.failures = measured.derive_failures()
    return measured


def _read_run(log_root: Path, task_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Read one run's journal: the turn count, the edits, and the raw rows."""
    rows: List[Dict[str, Any]] = []
    trace_path = Path(log_root) / task_id / "trace.jsonl"
    if trace_path.is_file():
        for line in trace_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    turns = 0
    for row in rows:
        if row.get("kind") == "task_end":
            value = (row.get("data") or {}).get("turns")
            if isinstance(value, int):
                turns = max(turns, value)
    edit_paths = [
        str((row.get("data") or {}).get("path") or "")
        for row in rows
        if row.get("kind") == "edit_applied"
    ]
    return {
        "turns": turns,
        "edit_paths": edit_paths,
        "rows": rows,
        "result": dict(result),
        "status": dict(result).get("status"),
    }


# --------------------------------------------------------------------------
# RUNG #9 - fast enough to use daily
# --------------------------------------------------------------------------

#: The four budgets, in the brief's vocabulary. Kept as named constants so the
#: report and the assertion read the SAME number - a threshold that exists
#: only inside an assert is a threshold nobody can check.
TARGET_STARTUP_MS = 2000.0
TARGET_RETRIEVAL_MS = 1000.0
TARGET_WARM_RETRIEVAL_MS = 200.0
TARGET_TUI_FRAME_MS = 50.0

#: The wall-clock ceiling on the cold walk. The measured cost on this
#: repository is ~180 s (see the rung's own report), so without a bound the
#: rung would itself become the thing that times out. A bounded measurement
#: that reports "did not finish inside N s" is a MEASUREMENT; an unbounded one
#: is a hang that reads as an infrastructure flake.
RETRIEVAL_DEADLINE_S = 20.0

#: How many directories the diagnostic walk counts before it stops. A full
#: count of this repository opens 147,239 directories and costs ~130 s, which
#: is 43% of the five-minute ladder lane for a number whose magnitude is
#: already unambiguous. 60,000 is still 387x the "another path opened 155"
#: figure `DOCTRINE.md` §8 records, so the diagnosis does not depend on
#: finishing the count. The number published is the number observed, and
#: ``truncated_at_cap`` says the count stopped early.
WALK_DIR_CAP = 60_000

#: Runs for each timing. 5 is the quick number; 20 is the first count that may
#: legally be called a p95. Both are published with the number they produced.
TIMING_RUNS = 5
TIMING_RUNS_P95 = 20


def _fresh_startup_ms(python: str = sys.executable) -> float:
    """Time a real import of the interactive path in a FRESH interpreter.

    In-process timing is worthless for this metric: by the time this runs,
    ``cli.interactive`` is already imported, so the number would be a
    dictionary lookup. ``harness/tool_errors.py`` once took ``import
    harness.core`` down and every already-imported test passed, which is
    exactly the class of break an in-process measurement cannot see.
    """
    script = (
        "import sys; sys.path.insert(0, %r);"
        "import cli.interactive, cli.runview, shared.agent_contracts" % str(REPO_ROOT)
    )
    started = time.perf_counter()
    proc = subprocess.run(
        [python, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(
            "the interactive import path failed, so startup is unavailable "
            "rather than measured: %s" % (proc.stderr or proc.stdout)[-400:]
        )
    return elapsed


@dataclass
class Rung9Result:
    """Rung #9's four budgets, plus the control that isolates the cause."""

    timings: List[Timing] = field(default_factory=list)
    control: Optional[Timing] = None
    walk_dirs_opened: Optional[int] = None
    walk_hit_cap: bool = False
    skip_list: Tuple[str, ...] = ()
    attribution: Dict[str, Any] = field(default_factory=dict)
    repo_label: str = ""
    failures: List[str] = field(default_factory=list)

    def derive_failures(self) -> List[str]:
        """Every budget that did not hold, plus the measured attribution.

        The single authority for "does rung #9 fail", shared with the test
        fixtures for the same reason as :meth:`Rung8Result.derive_failures`:
        a fixture that writes its own ``failures`` list tests the fixture.
        """
        out: List[str] = []
        for timing in self.timings:
            if not timing.available or timing.target is None or timing.headline is None:
                continue
            if timing.name == "retrieval_walk_dirs_opened":
                continue
            if timing.headline > timing.target:
                out.append(
                    f"{timing.name}: {timing.describe()} exceeds its budget of "
                    f"{timing.target:g}{timing.unit}"
                )
            extra = timing.extra
            if timing.name == "retrieval_cold_then_warm":
                warm_median = extra.get("warm_median_ms")
                if isinstance(warm_median, (int, float)) and warm_median > extra.get(
                    "warm_target_ms", TARGET_WARM_RETRIEVAL_MS
                ):
                    out.append(
                        f"retrieval_warm: median {warm_median:.0f}ms over "
                        f"{extra.get('warm_window')} runs exceeds "
                        f"{extra.get('warm_target_ms')}ms"
                    )
        cold_timing = next(
            (t for t in self.timings if t.name == "retrieval_cold_then_warm"), None
        )
        # The attribution line is reported only when the cold number is
        # actually OVER budget. It fired unconditionally in the first
        # version, so a fully-green fixture still produced a failure row --
        # which would have made every rung-#9 test that expected `pass`
        # unable to pass, and a gate that cannot go green teaches people to
        # ignore it.
        over_budget = (
            cold_timing is not None
            and cold_timing.available
            and cold_timing.headline is not None
            and cold_timing.target is not None
            and cold_timing.headline > cold_timing.target
        )
        if over_budget and cold_timing is not None:
            cold_headline = cold_timing.headline or 0.0
            attribution = self.attribution or {}
            if attribution.get("dominant_phase"):
                out.append(
                    f"the cold retrieval cost {cold_headline:.0f}ms against a "
                    f"{TARGET_RETRIEVAL_MS:.0f}ms budget, and the measured "
                    f"dominant phase is {attribution['dominant_phase']} "
                    f"({attribution.get('dominant_share_pct')}% of the call). "
                    + str(attribution.get("note") or "")
                )
            else:
                out.append(
                    f"the cold retrieval cost {cold_headline:.0f}ms against a "
                    f"{TARGET_RETRIEVAL_MS:.0f}ms budget, and NO single phase "
                    f"dominates, so no single-phase cause is claimed: "
                    + str(attribution.get("note") or "")
                )
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo_label,
            "machine": machine_label(),
            "timings": [t.to_dict() for t in self.timings],
            "control": self.control.to_dict() if self.control else None,
            "walk_dirs_opened": self.walk_dirs_opened,
            "walk_hit_cap": self.walk_hit_cap,
            "skip_list_size": len(self.skip_list),
            "skip_list": list(self.skip_list),
            "attribution": self.attribution,
            "failures": self.failures,
        }


def measure_startup(runs: int = TIMING_RUNS) -> Timing:
    """Startup -> first token, measured cold in ``runs`` fresh interpreters.

    With no provider reachable this is startup -> first *token of the
    interactive path*, i.e. the import graph the user waits behind. The
    metric name says so, and :attr:`Timing.reason` carries the limit, because
    "startup to first token" without a provider would be a number describing
    something that never ran.
    """
    try:
        samples = [_fresh_startup_ms() for _ in range(max(1, int(runs)))]
    except Exception as exc:
        return Timing(
            name="startup_to_first_token",
            unit="ms",
            target=TARGET_STARTUP_MS,
            available=False,
            reason=(
                f"not measurable on this host: {exc!r}. Reported unavailable, not 0."
            ),
        )
    return Timing(
        name="startup_to_first_token",
        unit="ms",
        samples=tuple(samples),
        target=TARGET_STARTUP_MS,
        extra={
            "measures": "fresh-interpreter import of cli.interactive + "
            "cli.runview + shared.agent_contracts",
            "no_provider": "there is no live provider, so this is startup to "
            "the first token the interactive path can produce, not to a "
            "model token",
        },
    )


def measure_retrieval(repo: str, runs: int = TIMING_RUNS_P95) -> Timing:
    """Retrieval on a real repository, COLD and WARM reported separately.

    Two timings, never one. A single warm number is the flattery this rung
    exists to prevent: the first retrieval on a cold checkout is the one a
    user pays for.
    """
    try:
        import harness.retrieval as R
    except Exception as exc:
        return Timing(
            name="retrieval_cold",
            unit="ms",
            target=TARGET_RETRIEVAL_MS,
            available=False,
            reason=f"harness.retrieval is not importable: {exc!r}",
        )

    issue = "redact secrets before journalling and the trust ladder vocabulary"
    cold: List[float] = []
    deadline = time.perf_counter() + RETRIEVAL_DEADLINE_S
    try:
        for _ in range(max(1, int(runs))):
            R.clear_context_cache()
            started = time.perf_counter()
            R.retrieve_context(repo, issue, max_files=4, max_grep_lines=20, cache=True)
            cold.append((time.perf_counter() - started) * 1000.0)
            if time.perf_counter() > deadline:
                break
    except Exception as exc:
        return Timing(
            name="retrieval_cold",
            unit="ms",
            target=TARGET_RETRIEVAL_MS,
            available=False,
            reason=f"retrieve_context raised: {exc!r}",
        )

    # The WARM pass needs the SAME deadline as the cold one. The first version
    # of this function bounded only the cold loop, so a repository where
    # retrieval never populates its cache ran the warm pass `runs` times at the
    # full cold cost - 20 x 177 s on this checkout, which hung the ladder for
    # over 15 minutes and produced nothing. A measurement that can wedge the
    # gate is not a measurement.
    warm: List[float] = []
    try:
        for _ in range(max(1, int(runs))):
            started = time.perf_counter()
            R.retrieve_context(repo, issue, max_files=4, max_grep_lines=20, cache=True)
            warm.append((time.perf_counter() - started) * 1000.0)
            if time.perf_counter() > deadline:
                break
    except Exception as exc:
        return Timing(
            name="retrieval_cold",
            unit="ms",
            samples=tuple(cold),
            target=TARGET_RETRIEVAL_MS,
            available=False,
            reason=f"the warm pass raised after the cold pass: {exc!r}",
        )

    return Timing(
        name="retrieval_cold_then_warm",
        unit="ms",
        samples=tuple(cold),
        target=TARGET_RETRIEVAL_MS,
        extra={
            "cold_median_ms": statistics.median(cold) if cold else None,
            "cold_window": len(cold),
            "warm_samples_ms": list(warm),
            "warm_median_ms": statistics.median(warm) if warm else None,
            "warm_window": len(warm),
            "warm_target_ms": TARGET_WARM_RETRIEVAL_MS,
            "warm_within_budget": bool(
                warm and statistics.median(warm) <= TARGET_WARM_RETRIEVAL_MS
            ),
            "deadline_s": RETRIEVAL_DEADLINE_S,
            "hit_deadline": (
                len(cold) < max(1, int(runs)) or len(warm) < max(1, int(runs))
            ),
            "hit_deadline_note": "a window that hit the deadline is reported "
            "with the window size it actually got; a short window is never "
            "padded to look like a longer one",
        },
    )


def measure_tui_frame(
    runs: int = TIMING_RUNS,
    facts_factory: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Timing:
    """Cost of rendering one run card, with the ``vacuous`` convention applied.

    A zero here would mean "we measured nothing", so :attr:`Timing.extra`
    always carries ``vacuous``. The control arm - a run that DID charge - is
    rendered in the same pass, so the test cannot pass by never charging
    anything (``DOCTRINE.md`` §3).
    """
    try:
        from rich.console import Console

        from cli.tui_components import ResultCard
    except Exception as exc:
        return Timing(
            name="tui_frame_cost",
            unit="ms",
            target=TARGET_TUI_FRAME_MS,
            available=False,
            reason=f"the card renderer is not importable: {exc!r}",
        )
    if facts_factory is None:
        return Timing(
            name="tui_frame_cost",
            unit="ms",
            target=TARGET_TUI_FRAME_MS,
            available=False,
            reason="no facts_factory supplied, so no frame was rendered",
        )

    try:
        facts = facts_factory()
    except Exception as exc:
        return Timing(
            name="tui_frame_cost",
            unit="ms",
            target=TARGET_TUI_FRAME_MS,
            available=False,
            reason=f"the facts factory raised: {exc!r}",
        )

    samples: List[float] = []
    rendered = 0
    for _ in range(max(1, int(runs))):
        console = Console(
            file=io.StringIO(), width=100, no_color=True, legacy_windows=False
        )
        started = time.perf_counter()
        console.print(
            ResultCard(
                task_id=str(facts.get("task_id") or "rung9"),
                mode="fix",
                facts=facts,
            ).render()
        )
        samples.append((time.perf_counter() - started) * 1000.0)
        rendered = len(console.file.getvalue())
    return Timing(
        name="tui_frame_cost",
        unit="ms",
        samples=tuple(samples),
        target=TARGET_TUI_FRAME_MS,
        extra={
            "rendered_chars": rendered,
            "vacuous": rendered == 0,
            "vacuous_note": "a zero-width render measures nothing; "
            "rendered_chars is published so that case is visible",
        },
    )


def measure_walk_cost(repo: str) -> Tuple[Optional[int], Tuple[str, ...], float, bool]:
    """Count the directories the retrieval walk actually OPENS.

    The number is the diagnosis. ``DOCTRINE.md`` §8 records the signature:
    *"one retrieval path opened 124,774 directories in 144.6 s where another
    opened 155 in 0.42 s. The same walk."* A skip-list that omits a large
    directory produces the same number again, and reporting only the elapsed
    time would leave the reader with a duration and no cause.

    Returns ``(dirs_opened, skip_list, elapsed_ms, hit_cap)``. ``hit_cap`` is
    published so a capped count is never read as a complete one.
    """
    try:
        import harness.retrieval as R
    except Exception:
        return None, (), 0.0, False
    skip = tuple(sorted(getattr(R, "_SKIP_DIRS", ())))
    pruning = set(skip)
    opened = 0
    hit_cap = False
    started = time.perf_counter()
    try:
        import os as _os

        for _dirpath, dirnames, _files in _os.walk(repo):
            opened += 1
            dirnames[:] = [d for d in dirnames if d not in pruning]
            if opened >= WALK_DIR_CAP:
                hit_cap = True
                break
    except Exception:
        return None, skip, (time.perf_counter() - started) * 1000.0, hit_cap
    return opened, skip, (time.perf_counter() - started) * 1000.0, hit_cap


def build_control_repo() -> Path:
    """A repo-shaped directory whose noise all sits in SKIPPED directories.

    The control arm for rung #9. It is what separates "retrieval is slow" from
    "retrieval is slow **because the skip list omits a 300k-file
    directory**": the same code, the same machine, the same call, and 1500
    noise files that the skip list actually covers.
    """
    root = Path(tempfile.mkdtemp(prefix="neo-rung9-control-")) / "repo"
    (root / "src").mkdir(parents=True, exist_ok=True)
    for i in range(40):
        (root / "src" / ("m%02d.py" % i)).write_text(
            "def f%d():\n    return %d\n" % (i, i), encoding="utf-8"
        )
    for noise in (".venv", "node_modules", "__pycache__", ".git", "logs"):
        for i in range(300):
            d = root / noise / ("d%03d" % i)
            d.mkdir(parents=True, exist_ok=True)
            (d / "junk.py").write_text("x = 1\n", encoding="utf-8")
    return root


def probe_rung9(
    *,
    repo: Optional[str] = None,
    startup_runs: int = TIMING_RUNS,
    retrieval_runs: int = TIMING_RUNS_P95,
    run_retrieval: bool = True,
    measure_frame: bool = True,
    facts_factory: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Rung9Result:
    """Measure all four rung #9 budgets, cold and warm, with the control.

    :param facts_factory: where the TUI frame's facts come from. ``None``
        means "build them from a real run", so the default lane measures a
        populated card rather than an empty dict. A frame rendered from an
        empty dict is fast for a reason that has nothing to do with the
        product.
    :param measure_frame: set False to skip the frame budget entirely; the
        row is then reported ``unavailable`` with that reason, never as 0 ms.
    """
    repo_path = str(repo or REPO_ROOT)
    result = Rung9Result(repo_label=repo_path, skip_list=(), walk_dirs_opened=None)
    result.timings.append(measure_startup(runs=startup_runs))

    if run_retrieval:
        result.timings.append(measure_retrieval(repo_path, runs=retrieval_runs))
        opened, skip, walk_ms, hit_cap = measure_walk_cost(repo_path)
        result.walk_dirs_opened = opened
        result.walk_hit_cap = hit_cap
        result.skip_list = skip
        control_repo = build_control_repo()
        try:
            import harness.retrieval as R

            started = time.perf_counter()
            R.walk_code_files_budgeted(str(control_repo), max_files=0)
            control_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:
            control_ms = float("nan")
            result.failures.append(f"the control walk raised: {exc!r}")
        result.control = Timing(
            name="retrieval_walk_control",
            unit="ms",
            samples=(control_ms,),
            extra={
                "what": "the SAME walk on a repo whose 1500 noise files all "
                "sit in directories the skip list covers",
                "vacuous": control_ms != control_ms,
            },
        )
        result.timings.append(
            Timing(
                name="retrieval_walk_dirs_opened",
                unit="dirs",
                samples=(float(opened),) if opened is not None else (),
                target=None,
                available=opened is not None,
                reason="the walk could not be counted on this host",
                extra={
                    "walk_ms": walk_ms,
                    "skip_list_size": len(skip),
                    "hit_cap": hit_cap,
                    "dir_cap": WALK_DIR_CAP,
                    "truncated_at_cap": hit_cap,
                },
            )
        )
    else:
        result.timings.append(
            Timing(
                name="retrieval_cold_then_warm",
                unit="ms",
                target=TARGET_RETRIEVAL_MS,
                available=False,
                reason="retrieval measurement suppressed by the caller "
                "(the ladder is running in a no-walk lane)",
            )
        )

    if not measure_frame:
        result.timings.append(
            Timing(
                name="tui_frame_cost",
                unit="ms",
                target=TARGET_TUI_FRAME_MS,
                available=False,
                reason="frame measurement suppressed by the caller",
            )
        )
    else:
        if facts_factory is None:
            try:
                facts_factory = rung9_facts_factory()
            except Exception as exc:
                facts_factory = None
                result.timings.append(
                    Timing(
                        name="tui_frame_cost",
                        unit="ms",
                        target=TARGET_TUI_FRAME_MS,
                        available=False,
                        reason=f"the real run that supplies the card's facts "
                        f"failed to build: {exc!r}",
                    )
                )
        if facts_factory is not None:
            result.timings.append(measure_tui_frame(facts_factory=facts_factory))

    # -- attribution -------------------------------------------------------
    # A ratio against the control is only a DIAGNOSIS when the thing being
    # compared is actually the bottleneck. The first version of this function
    # reported 'the skip list does not cover this repository' from a >10x
    # ratio, and that was a LIE: T1 landed a 45-entry shared skip set while
    # this probe was being written, the walk dropped from 147,239 directories
    # to 157, and the ~50 s that remained had nothing to do with the walk. So
    # the attribution is MEASURED per phase and folded into the verdict
    # derivation, and when no single phase dominates the rung says NO cause
    # rather than naming a culprit.
    result.attribution = attribute_retrieval(repo_path)
    result.failures.extend(result.derive_failures())
    return result


#: The phases a cold retrieval is attributed to, and the share above which a
#: phase may be NAMED as the dominant cost. 50% is chosen so a two-phase split
#: has to be a landslide to produce a confident single-cause claim.
ATTRIBUTION_DOMINANT_SHARE = 0.50


def attribute_retrieval(repo: str) -> Dict[str, Any]:
    """Measure where a cold retrieval's time goes, by PHASE.

    Uses :mod:`cProfile` and the callees of ``retrieve_context``, which is the
    only way to attribute cost inside a 30 s call without editing another
    terminal's module to add timers. Returns the cumulative seconds of each
    phase plus the dominant one, or ``dominant_phase=None`` when the profile
    is unavailable - in which case the caller must NOT name a cause.

    Never raises: a profiling failure is reported as "no attribution", which
    is the honest answer and keeps the rung's failure about the BUDGET rather
    than about the diagnosis.
    """
    out: Dict[str, Any] = {
        "dominant_phase": None,
        "dominant_share_pct": 0.0,
        "phases": {},
        "graph_rebuilt": False,
        "note": "no phase attribution was measured",
    }
    try:
        import cProfile
        import io as _io
        import pstats

        import harness.retrieval as R
    except Exception as exc:
        out["note"] = f"phase attribution unavailable: {exc!r}"
        return out
    try:
        R.clear_context_cache()
        pr = cProfile.Profile()
        pr.enable()
        R.retrieve_context(repo, "attribution probe", max_files=4, max_grep_lines=20)
        pr.disable()
    except Exception as exc:
        out["note"] = f"the attributed call raised: {exc!r}"
        return out

    buf = _io.StringIO()
    try:
        pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(40)
    except Exception as exc:
        out["note"] = f"pstats failed: {exc!r}"
        return out

    phases: Dict[str, float] = {}
    for line in buf.getvalue().splitlines():
        if "memory/code_graph.py" in line and "_save_locked" in line:
            phases["code_graph_save"] = max(
                phases.get("code_graph_save", 0.0), _cumulative_seconds(line)
            )
        elif (
            "memory/code_graph.py" in line
            and "code_graph.py" in line
            and "build" in line
        ):
            phases["code_graph_build"] = max(
                phases.get("code_graph_build", 0.0), _cumulative_seconds(line)
            )
        elif "json/__init__.py" in line and "dumps" in line:
            phases["json_serialisation"] = max(
                phases.get("json_serialisation", 0.0), _cumulative_seconds(line)
            )
        elif "tree_sitter" in line:
            phases["tree_sitter_parse"] = max(
                phases.get("tree_sitter_parse", 0.0), _cumulative_seconds(line)
            )
        elif "_caller_id" in line:
            phases["caller_resolution"] = max(
                phases.get("caller_resolution", 0.0), _cumulative_seconds(line)
            )
    # Whether the code graph was REBUILT during the profiled call. This is the
    # distinction that makes the attribution honest: a cold retrieval after a
    # source edit reindexes the whole repository, and a warm one does not, so
    # the two have different dominant phases and quoting one for the other
    # would be a diagnosis of the wrong run.
    out["graph_rebuilt"] = "code_graph_build" in phases
    total = sum(phases.values()) or 1.0
    out["phases"] = {k: round(v, 3) for k, v in sorted(phases.items())}
    if phases:
        name, value = max(phases.items(), key=lambda kv: kv[1])
        share = value / total
        if share >= ATTRIBUTION_DOMINANT_SHARE:
            out["dominant_phase"] = name
            out["dominant_share_pct"] = round(share * 100.0, 1)
            out["note"] = (
                f"phase seconds: {out['phases']}; the code graph was "
                + (
                    "REBUILT during this call, so this attribution describes "
                    "the cold-after-a-source-edit path"
                    if out["graph_rebuilt"]
                    else "NOT rebuilt during this call, so this attribution "
                    "describes the WARM path and does NOT describe the cold "
                    "number the budget failure is about"
                )
                + f". The measured dominant phase is `{name}` at "
                f"{out['dominant_share_pct']}%. Owner: T1/T5 -- "
                f"memory/code_graph.py has no incremental index, so one source "
                f"edit forces a whole-repository reindex, and the persisted "
                f"graph is re-serialised to JSON on every save."
            )
        else:
            out["note"] = (
                f"no phase exceeds {ATTRIBUTION_DOMINANT_SHARE:.0%} of the "
                f"measured total; phase seconds: {out['phases']}; graph "
                f"rebuilt: {out['graph_rebuilt']}. Reporting a single cause "
                f"here would be a guess."
            )
    return out


def _cumulative_seconds(profile_line: str) -> float:
    """Pull the CUMULATIVE-seconds column out of a pstats row. 0.0 if absent.

    A pstats row is ``ncalls tottime percall cumtime percall {file}``. The
    first numeric column is ``ncalls`` -- taking column 0 reported 1.0 for
    every phase and made the whole attribution read "no phase was measured",
    which is the most expensive possible way to be wrong: it looks like a
    missing measurement rather than a broken parser.
    """
    parts = profile_line.split()
    if len(parts) < 4:
        return 0.0
    try:
        return float(parts[3])
    except (ValueError, IndexError):
        return 0.0


# --------------------------------------------------------------------------
# RUNG #10 - you can always see what it did and why
# --------------------------------------------------------------------------

#: The four links. Named so a report can say WHICH link dropped information,
#: which is the difference between a useful finding and "visibility is bad".
CHAIN_LINKS: Tuple[str, ...] = (
    "journal_row",
    "traceview_reconstruct",
    "runview_projection",
    "tui_card",
)


@dataclass
class ChainLink:
    """One link in the visibility chain, and what survived it."""

    link: str
    present: bool
    keys_in: Tuple[str, ...] = ()
    keys_out: Tuple[str, ...] = ()
    dropped: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "link": self.link,
            "present": self.present,
            "keys_in": list(self.keys_in),
            "keys_out": list(self.keys_out),
            "dropped": list(self.dropped),
            "detail": self.detail,
        }


@dataclass
class Rung10Result:
    """What rung #10 measured on a REAL run."""

    links: List[ChainLink] = field(default_factory=list)
    event_count: int = 0
    facts_key_count: int = 0
    card_text: str = ""
    truncated_visible: bool = False
    unavailable_visible: bool = False
    truncated_evidence: str = ""
    unavailable_evidence: str = ""
    vacuous_cost_visible: bool = False
    cost_known: Optional[bool] = None
    cost_usd: Optional[float] = None
    charged_control_cost: Optional[float] = None
    charged_control_cost_known: Optional[bool] = None
    charged_control_text: str = ""
    failures: List[str] = field(default_factory=list)

    def derive_failures(self) -> List[str]:
        """Every chain and honest-presentation check that did not hold.

        The single authority for "does rung #10 fail", shared with the test
        fixtures for the same reason as :meth:`Rung8Result.derive_failures`.
        Each entry names the LINK or the presentation obligation, because
        "visibility is bad" is not actionable and the four links have four
        different owners.
        """
        out: List[str] = []
        missing = [
            f"LINK {CHAIN_LINKS.index(link.link) + 1} ({link.link})"
            for link in self.links
            if not link.present
        ]
        if missing:
            out.append(
                "the chain is broken: "
                + ", ".join(missing)
                + " produced nothing, so nothing downstream can be trusted."
            )
        dropped = sorted({k for link in self.links for k in link.dropped})
        attribution_dropped = [k for k in dropped if k in ("module", "source")]
        if attribution_dropped:
            out.append(
                "LINK 3 (runview) drops the traceview cross-module attribution "
                f"{attribution_dropped}: a reader of the card cannot tell which "
                "module produced a row. The projection re-derives every field it "
                "keeps rather than carrying the reconstructed one."
            )
        if not self.truncated_visible:
            out.append(
                "HONEST PRESENTATION (truncated): a retrieval_truncated journal "
                "row is recorded but appears in NO run fact and on NO card line. "
                "harness/knowledge.py::render_truncation_note exists and has no "
                "production call site, and cli/runview.py lists "
                "`retrieval_truncated` in INFORMATIONAL_EVENTS, so it is "
                "consumed and never surfaced. Owner: T4 "
                "(cli/runview.py, cli/tui_components.py). Evidence: "
                + self.truncated_evidence
            )
        if not self.unavailable_visible:
            out.append(
                "HONEST PRESENTATION (unavailable): the card has no vocabulary "
                "for an absent value. `cli.runview.briefing_lines` already has "
                "the convention (`if not data.get('available')` -> an honest "
                "line); `card_lines` and `ResultCard` have none, so an absent "
                "field is simply omitted and reads as 'there was nothing to "
                "report'. Owner: T4 (cli/runview.py::card_lines, "
                "cli/tui_components.py::ResultCard). Evidence: "
                + self.unavailable_evidence
            )
        if self.vacuous_cost_visible:
            out.append(
                f"HONEST PRESENTATION (vacuous zero): the run cost NOTHING "
                f"(cost_known={self.cost_known!r}, cost_usd={self.cost_usd!r}) "
                f"and the card renders '$0.000000' with no 'unpriced' and no "
                f"'vacuous' marker. The charged control arm rendered "
                f"cost_usd={self.charged_control_cost!r} / "
                f"cost_known={self.charged_control_cost_known!r}. "
                f"`DOCTRINE.md` §1 forbids reporting an unpriced value as "
                f"`$0`, and §3 requires `vacuous: true` beside a zero that "
                f"measures nothing. Owner: T4 (cli/runview.py::card_lines)."
            )
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain": list(CHAIN_LINKS),
            "links": [link.to_dict() for link in self.links],
            "event_count": self.event_count,
            "facts_key_count": self.facts_key_count,
            "truncated_visible": self.truncated_visible,
            "unavailable_visible": self.unavailable_visible,
            "truncated_evidence": self.truncated_evidence,
            "unavailable_evidence": self.unavailable_evidence,
            "vacuous_cost_visible": self.vacuous_cost_visible,
            "cost_known": self.cost_known,
            "cost_usd": self.cost_usd,
            "charged_control_cost": self.charged_control_cost,
            "charged_control_cost_known": self.charged_control_cost_known,
            "failures": self.failures,
        }


def _render_card(facts: Dict[str, Any], mode: str = "fix") -> str:
    """Render the real TUI card to text. Returns "" if it cannot be rendered.

    The card is the LAST link, so a rung that stops one link short is not
    measuring the thing a user actually looks at. Rendered through a real
    ``rich.Console`` at a fixed width so the capture is comparable run to run.
    """
    try:
        from rich.console import Console

        from cli.tui_components import ResultCard
    except Exception:
        return ""
    console = Console(
        file=io.StringIO(), width=100, no_color=True, legacy_windows=False
    )
    try:
        console.print(
            ResultCard(
                task_id=str(facts.get("task_id") or "rung10"), mode=mode, facts=facts
            ).render()
        )
    except Exception:
        return ""
    return console.file.getvalue()


def _run_real_agent_run(
    log_root: Path, task_id: str, *, cost_per_call: float
) -> Dict[str, Any]:
    """Drive the real agent loop so the chain is measured on REAL artifacts.

    No Docker, no provider, no network: the model is a scripted double and
    only WRITE is dispatched, so no bash tool is ever reached.
    """

    class _M(ScriptedModel):
        def get_last_usage(self) -> Dict[str, Any]:
            return {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "cost": cost_per_call,
            }

    def script(turn: int, _rows: List[Dict[str, str]]) -> str:
        if turn > 2:
            return json.dumps({"tool": "done", "answer": "done"})
        return json.dumps(
            {"tool": "write", "path": "src/m%d.py" % turn, "content": "V=%d\n" % turn}
        )

    from harness import deps
    from harness.agent_loop import run_agent_stepped

    repo = _build_repo(Path(tempfile.mkdtemp(prefix="neo-rung10-")) / "repo")
    deps.set_call_model(_M(script))
    try:
        return dict(
            run_agent_stepped(
                "edit two modules",
                str(repo),
                config={
                    "agent_strategy": "agent_step",
                    "steering_enabled": False,
                    "agent_max_turns": 6,
                },
                log_root=log_root,
                task_id=task_id,
            )
        )
    finally:
        deps.reset_overrides()


def _append_journal_row(trace_path: Path, kind: str, data: Dict[str, Any]) -> None:
    """Append one journal row, exactly as the harness would have written it."""
    with trace_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"ts": round(time.time(), 3), "kind": kind, "data": data},
                default=str,
            )
            + "\n"
        )


def probe_rung10(*, task_id: str = "rung10") -> Rung10Result:
    """Assert the whole chain on a real run, and that honesty survives it."""
    result = Rung10Result()
    log_root = Path(tempfile.mkdtemp(prefix="neo-rung10-logs-"))
    _run_real_agent_run(log_root, task_id, cost_per_call=0.0)
    log_dir = log_root / task_id
    trace_path = log_dir / "trace.jsonl"

    # -- LINK 1: the journal row ------------------------------------------
    journal_present = trace_path.is_file()
    journal_kinds: Tuple[str, ...] = ()
    if journal_present:
        journal_kinds = tuple(
            dict.fromkeys(
                json.loads(line).get("kind", "")
                for line in trace_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip()
            )
        )
    result.links.append(
        ChainLink(
            link="journal_row",
            present=journal_present,
            keys_out=journal_kinds,
            detail="logs/{task_id}/trace.jsonl, one JSON object per line",
        )
    )
    if not journal_present:
        result.failures.append(
            f"LINK 1 (journal row): no trace.jsonl under {log_dir}. The chain "
            "starts from nothing, so nothing downstream can be trusted."
        )
        return result

    # -- LINK 2: traceview ------------------------------------------------
    trace_keys: Tuple[str, ...] = ()
    try:
        from shared import traceview as TV

        events = TV.reconstruct_task(task_id, logs_root=log_root)
        result.event_count = len(events)
        if events:
            trace_keys = tuple(sorted(events[0].keys()))
    except Exception as exc:
        result.failures.append(f"LINK 2 (traceview) raised: {exc!r}")
        events = []
    result.links.append(
        ChainLink(
            link="traceview_reconstruct",
            present=bool(events),
            keys_in=journal_kinds,
            keys_out=trace_keys,
            detail=f"{result.event_count} events reconstructed from the journal",
        )
    )

    # -- LINK 3: runview --------------------------------------------------
    try:
        import cli.runview as RV
    except Exception as exc:
        result.failures.append(f"LINK 3 (runview) is not importable: {exc!r}")
        return result
    facts = RV.read_run_facts(log_dir)
    result.facts_key_count = len(facts)
    facts_keys = tuple(sorted(facts.keys()))
    dropped = tuple(k for k in trace_keys if k not in facts_keys)
    result.links.append(
        ChainLink(
            link="runview_projection",
            present=bool(facts),
            keys_in=trace_keys,
            keys_out=facts_keys,
            dropped=dropped,
            detail=f"{result.facts_key_count} run facts from the projection",
        )
    )
    # The dropped-attribution finding is NOT appended here. It is derived by
    # `Rung10Result.derive_failures()` from `links[2].dropped`, which is the
    # single authority for "does rung #10 fail". Appending it here as well made
    # the report print the identical finding TWICE, which is how a reader
    # learns to skim a list that repeats itself.

    # -- LINK 4: the TUI card ---------------------------------------------
    card = _render_card(facts)
    result.card_text = card
    result.links.append(
        ChainLink(
            link="tui_card",
            present=bool(card.strip()),
            keys_in=facts_keys,
            detail=(
                f"ResultCard.render() produced {len(card)} chars"
                if card
                else "ResultCard.render() produced NOTHING, so the chain ends "
                "at a projection no user ever sees"
            ),
        )
    )
    if not card.strip():
        result.failures.append(
            "LINK 4 (TUI card): the card rendered nothing on a real run that "
            "edited two files."
        )
        return result

    # -- HONEST PRESENTATION 1: a truncated search ------------------------
    _append_journal_row(
        trace_path,
        "retrieval_truncated",
        {
            "truncated": True,
            "truncation": "budget",
            "not_searched": 12,
            "not_searched_known": True,
        },
    )
    facts_t = RV.read_run_facts(log_dir)
    trunc_fact_keys = [k for k in facts_t if "trunc" in k.lower()]
    card_t = _render_card(facts_t)
    result.truncated_visible = bool(
        [k for k in trunc_fact_keys]
        or "truncat" in card_t.lower()
        or "not searched" in card_t.lower()
    )
    result.truncated_evidence = (
        f"injected a retrieval_truncated journal row (truncated=True, "
        f"truncation='budget', not_searched=12). Projection gained facts "
        f"{trunc_fact_keys}; card mentions truncation: "
        f"{'truncat' in card_t.lower()}. The row is recorded in the journal "
        f"and is visible to `shared.traceview`, and then it is DROPPED: a "
        f"reader is told a search was incomplete by nothing at all."
    )
    # -- HONEST PRESENTATION 2: an unavailable field ----------------------
    # MEASURED, not assumed. The existing convention in this module is
    # `cli.runview.briefing_lines`'s `if not data.get("available")` -> one
    # honest line; the card is held to the same bar, and whether it meets it
    # is read out of the rendered card rather than assumed either way.
    briefing_handles_absence = "available" in (RV.briefing_lines.__doc__ or "") or (
        "available" in _source_of(RV.briefing_lines)
    )
    card_handles_absence = any(
        token in card.lower() for token in ("unavailable", "unpriced", "not measured")
    )
    result.unavailable_visible = card_handles_absence
    result.unavailable_evidence = (
        f"briefing_lines carries the available-flag convention: "
        f"{briefing_handles_absence}. The completion card does not: "
        f"{card_handles_absence}."
    )

    # -- HONEST PRESENTATION 3: a zero that is not a measurement ----------
    # `DOCTRINE.md` 3: a zero that reads as 'the lane was free' is a
    # number nobody took. The CHARGED arm is rendered too, so this check
    # cannot pass by never charging anything.
    result.cost_known = facts.get("cost_known")
    result.cost_usd = facts.get("cost_usd")
    _run_real_agent_run(log_root, "rung10-charged", cost_per_call=0.02)
    charged_facts = RV.read_run_facts(log_root / "rung10-charged")
    result.charged_control_cost = charged_facts.get("cost_usd")
    result.charged_control_cost_known = charged_facts.get("cost_known")
    result.charged_control_text = _render_card(charged_facts)
    result.vacuous_cost_visible = (
        result.cost_known is False
        and result.cost_usd == 0.0
        and "$0.000000" in card
        and ("unpriced" not in card.lower())
        and ("vacuous" not in card.lower())
    )
    # The verdict is DERIVED, not accumulated here, so a test fixture that
    # fabricates a measurement cannot also fabricate the failure list.
    result.failures.extend(result.derive_failures())
    return result


def _source_of(fn: Any) -> str:
    """Best-effort source text of a callable; "" when unavailable."""
    import inspect

    try:
        return inspect.getsource(fn)
    except Exception:
        return ""


def rung9_facts_factory(task_id: str = "rung9-frame") -> Callable[[], Dict[str, Any]]:
    """A facts factory for rung #9's TUI-frame measurement.

    Built from a REAL run so the frame cost is the cost of a populated card
    rather than of an empty dict.
    """
    log_root = Path(tempfile.mkdtemp(prefix="neo-rung9-frames-"))
    _run_real_agent_run(log_root, task_id, cost_per_call=0.01)
    log_dir = log_root / task_id

    def factory() -> Dict[str, Any]:
        import cli.runview as RV

        return dict(RV.read_run_facts(log_dir))

    return factory
