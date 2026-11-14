"""T5.W1.4 — the verification cost, measured and bounded, and a stated policy.

A 20-minute monorepo verification is a feature nobody uses — the user turns
verification off, and that is how projects rot quietly. So:

* the **three costs** are MEASURED and reported, each with the basis it was
  measured on;
* the **policy** that picks between them is stated, not implied;
* a run is **bounded and interruptible** — a deadline that actually kills the
  child process, not one that stops asking;
* a **skipped package is named** — never rendered as verified.

Three rules that this module exists to enforce, each of which is a way a
plausible implementation lies:

1. **An unmeasured cost is ``None``, never ``0.0``.** A zero is a claim that
   the work was free.
2. **A bounded run reports what it did NOT run.** Stopping at the deadline and
   returning a green report over the packages that happened to finish first is
   the monorepo form of a partial-selection pass.
3. **The deadline kills the child.** ``subprocess.run(timeout=...)`` bounds the
   parent wait on POSIX; on Windows it can outlive its budget by an order of
   magnitude, so the timeout is escalated to a process-tree kill.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "COST_MODES",
    "SKIP_REASON",
    "TIMED_OUT_REASON",
    "BoundedRun",
    "CostNumbers",
    "MeasuredCost",
    "PolicyDecision",
    "VerificationPolicy",
    "VerificationRunReport",
    "choose_policy",
    "measure_verification_costs",
    "render_cost_numbers",
    "run_packages_bounded",
]

SKIP_REASON = "no dependency path"
TIMED_OUT_REASON = "wall-clock budget exhausted"
COST_MODES = ("full_suite", "package_selection", "changed_package_only")


@dataclass(frozen=True)
class VerificationPolicy:
    """The stated policy. A number nobody chose is not a policy."""

    full_suite_when_no_selection: bool = True
    changed_package_only_in_inner_loop: bool = True
    full_suite_every_commits: int = 5
    max_wall_clock_s: float = 1200.0
    #: packages omitted from the run are reported by name, never silent
    report_skipped: bool = True
    #: per-package bound; a package that blows this is reported, not waited on
    max_package_s: float = 300.0
    #: do not START a package with less than this much budget left, because a
    #: half-run package is the most expensive kind of "not run"
    min_remaining_to_start_package_s: float = 30.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CostNumbers:
    """The three numbers a user needs in order to choose the policy."""

    full_suite_s: Optional[float] = None
    per_package_selection_s: Optional[float] = None
    changed_package_only_s: Optional[float] = None
    #: what each number was measured ON. A cost without a basis is a rumour.
    basis: str = ""
    measured: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "full_suite_s": self.full_suite_s,
            "per_package_selection_s": self.per_package_selection_s,
            "changed_package_only_s": self.changed_package_only_s,
            "basis": self.basis,
            "measured": self.measured,
        }

    def unmeasured_modes(self) -> Tuple[str, ...]:
        return tuple(
            mode
            for mode, value in (
                ("full_suite", self.full_suite_s),
                ("package_selection", self.per_package_selection_s),
                ("changed_package_only", self.changed_package_only_s),
            )
            if value is None
        )


def render_cost_numbers(numbers: CostNumbers) -> str:
    """Render, and never round a real measurement down to a zero.

    ``0.00s`` is the exact string a genuinely free operation produces, so a
    sub-second measurement renders in milliseconds -- the same rule
    ``execution.verify_cache`` already follows for its own table.
    """
    lines: List[str] = []
    for label, value in (
        ("full suite", numbers.full_suite_s),
        ("per-package selection", numbers.per_package_selection_s),
        ("changed packages only", numbers.changed_package_only_s),
    ):
        if value is None:
            lines.append(f"  {label:<24} UNMEASURED (not zero)")
        elif value < 1.0:
            lines.append(f"  {label:<24} {value * 1000:.1f}ms")
        else:
            lines.append(f"  {label:<24} {value:.2f}s")
    if numbers.basis:
        lines.append(f"  basis: {numbers.basis}")
    missing = numbers.unmeasured_modes()
    if missing:
        lines.append(
            "  NOTE: an unmeasured cost is not a small cost: " + ", ".join(missing)
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class PolicyDecision:
    mode: str  # one of COST_MODES
    reason: str


def choose_policy(
    *,
    changed_packages: Sequence[str],
    total_packages: int,
    commits_since_full: int,
    policy: VerificationPolicy,
) -> PolicyDecision:
    """Decide which verification runs, and say why.

    The rule: the inner loop runs the smallest truthful set (changed package
    only); the full suite runs when the changed set is the whole workspace, when
    a selection would be unsound, and at least every ``full_suite_every_commits``
    commits. Every branch answers with a reason, because a mode with no stated
    reason is a default, and a default is how a policy becomes folklore.
    """
    changed = [p for p in changed_packages if p]
    if not changed:
        return PolicyDecision("full_suite", "no package claims the change: full suite")
    if policy.full_suite_when_no_selection and total_packages > 0 and len(changed) >= total_packages:
        return PolicyDecision("full_suite", "the change spans the whole workspace")
    if commits_since_full >= policy.full_suite_every_commits:
        return PolicyDecision(
            "full_suite",
            f"{commits_since_full} commits since the last full suite",
        )
    if (
        policy.changed_package_only_in_inner_loop
        and total_packages > 0
        and len(changed) <= max(1, total_packages // 2)
    ):
        return PolicyDecision(
            "changed_package_only",
            f"{len(changed)} of {total_packages} package(s) changed: run only those",
        )
    return PolicyDecision("package_selection", "selection across the impacted packages")


# --------------------------------------------------------------------------
# bounded, interruptible execution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundedRun:
    """One bounded command: how long, what happened, and how it was stopped."""

    label: str
    command: str
    exit_code: Optional[int]
    wall_clock_s: float
    timed_out: bool = False
    #: True only when the child was actually stopped, not merely waited on.
    killed: bool = False
    output_tail: str = ""
    note: str = ""


#: What actually runs one package's verification command.
#:
#: The default is the SANDBOX, not a host shell, and that is the whole design
#: decision: a verification command is untrusted input (it is whatever the
#: repository's own ``test`` script says), this package never spawns through a
#: shell, and ``execution.sandbox.execute_sandboxed`` already owns timeout,
#: container kill, exit 124 and the cancellation token. Reimplementing any of
#: that on the host would be a second authority for the one property ("the
#: deadline stops the child") this module most needs to be right about.
Runner = Callable[[str, str, float], BoundedRun]


def argv_from_command(command: str) -> List[str]:
    """Split a declared test-script string into a real argv.

    This is not cosmetic. A repository's ``test`` script is shell-ish text, and
    the first version of the host runner used ``shlex`` in Windows mode, which
    keeps the quote characters inside the tokens -- so
    ``python -c "import time; time.sleep(0.35)"`` dispatched as
    ``['-c', '"import', 'time;', 'time.sleep(0.35)"']``, failed instantly, and
    a cost measurement came back as 0.20 s for a command that was supposed to
    take 0.35 s. A cost table measured over commands that never ran is worse
    than no table, because it looks like data.
    """
    import shlex

    if os.name != "nt":
        return shlex.split(command)
    tokens = shlex.split(command, posix=False)
    out: List[str] = []
    for tok in tokens:
        if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
            tok = tok[1:-1]
        out.append(tok)
    return out


def sandbox_runner(repo_path: str) -> Runner:
    """A runner that dispatches through the real sandbox boundary."""

    def _run(command: str, cwd: str, timeout_s: float) -> BoundedRun:
        from execution.sandbox import execute_sandboxed

        started = time.monotonic()
        try:
            res = execute_sandboxed(cwd, command, int(max(1.0, timeout_s)))
        except Exception as exc:  # a machine fault is NOT a test failure
            return BoundedRun(
                label="",
                command=command,
                exit_code=None,
                wall_clock_s=time.monotonic() - started,
                note=f"the sandbox could not run this: {exc}",
            )
        timed = bool(getattr(res, "timed_out", False)) or res.exit_code == 124
        return BoundedRun(
            label="",
            command=command,
            exit_code=res.exit_code,
            wall_clock_s=time.monotonic() - started,
            timed_out=timed,
            # execute_sandboxed kills the container by name on timeout, so a
            # timeout here IS a kill -- unlike a parent-side wait.
            killed=timed,
            output_tail=(res.stdout or "")[-2000:],
            note="wall-clock budget exhausted; the container was killed by name"
            if timed
            else "",
        )

    return _run


def subprocess_argv_runner() -> Runner:
    """A list-argv host runner, for the case where no daemon is available.

    A LIST argv and no shell, which is the shape
    ``execution/test_p2w1_trust_pins.py::test_shell_true_appears_nowhere_in_execution``
    requires of every spawn in this package.
    """

    def _run(command: str, cwd: str, timeout_s: float) -> BoundedRun:
        started = time.monotonic()
        try:
            argv = argv_from_command(command)
        except ValueError as exc:
            return BoundedRun(
                label="",
                command=command,
                exit_code=None,
                wall_clock_s=time.monotonic() - started,
                note=f"the declared command is not parseable into an argv: {exc}",
            )
        if not argv:
            return BoundedRun(
                label="",
                command=command,
                exit_code=None,
                wall_clock_s=time.monotonic() - started,
                note="the declared command is empty: nothing to run",
            )
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            # On POSIX run() reaps and kills the direct child; on Windows it
            # does not bound the tree. Say which one this was rather than
            # reporting a kill that may not have happened.
            return BoundedRun(
                label="",
                command=command,
                exit_code=None,
                wall_clock_s=time.monotonic() - started,
                timed_out=True,
                killed=os.name != "nt",
                note=(
                    "wall-clock budget exhausted"
                    + (
                        " and the child was killed"
                        if os.name != "nt"
                        else "; the wait was bounded but the child tree was NOT "
                        "killed on this platform -- treat this as not-run, not as a failure"
                    )
                ),
            )
        except FileNotFoundError as exc:
            return BoundedRun(
                label="",
                command=command,
                exit_code=None,
                wall_clock_s=time.monotonic() - started,
                note=f"command not found: {exc}",
            )
        return BoundedRun(
            label="",
            command=command,
            exit_code=proc.returncode,
            wall_clock_s=time.monotonic() - started,
            output_tail=(proc.stdout or "")[-2000:],
        )

    return _run


def run_bounded(
    command: str,
    *,
    cwd: str,
    timeout_s: float,
    label: str = "",
    runner: Optional[Runner] = None,
) -> BoundedRun:
    """Run one command under a deadline, through the supplied runner."""
    run = (runner or subprocess_argv_runner())(command, cwd, timeout_s)
    return dataclasses.replace(run, label=label or run.label)


@dataclass(frozen=True)
class VerificationRunReport:
    """What a bounded run did — including, always, what it skipped."""

    mode: str
    verified_packages: Tuple[str, ...]
    skipped_packages: Tuple[str, ...]
    wall_clock_s: float
    bounded: bool
    notes: Tuple[str, ...] = ()
    per_package: Tuple[BoundedRun, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "verified_packages": list(self.verified_packages),
            "skipped_packages": list(self.skipped_packages),
            "wall_clock_s": self.wall_clock_s,
            "bounded": self.bounded,
            "notes": list(self.notes),
            "per_package": [dataclasses.asdict(r) for r in self.per_package],
        }

    def skipped_sentence(self) -> str:
        if not self.skipped_packages:
            return ""
        return f"skipped {', '.join(self.skipped_packages)} ({SKIP_REASON})"


def run_packages_bounded(
    commands: Sequence[Tuple[str, str]],
    *,
    workspace_root: str,
    policy: Optional[VerificationPolicy] = None,
    runner: Optional[Runner] = None,
    clock: Callable[[], float] = time.monotonic,
) -> VerificationRunReport:
    """Run per-package commands under a wall-clock budget, interruptibly.

    ``commands`` is ``(package_name, command)`` in the order they should run.

    Three properties this must have, and they are the same bug wearing three
    hats — each one is a way a bounded run returns a clean report over work it
    did not do:

    * a package that exceeds its budget is reported **not run**, not failed;
    * a package the budget cannot honestly finish is **not started at all**, and
      is named in ``skipped_packages`` — starting a package with 2 s left
      produces a half-run package whose failure reads as a code failure;
    * a runner that could not dispatch at all (no daemon, missing binary) is
      reported, never counted as a pass.
    """
    pol = policy or VerificationPolicy()
    the_runner = runner or subprocess_argv_runner()
    started = clock()
    verified: List[str] = []
    skipped: List[str] = []
    notes: List[str] = []
    runs: List[BoundedRun] = []
    bounded = False

    for name, command in commands:
        remaining = pol.max_wall_clock_s - (clock() - started)
        if remaining <= 0:
            bounded = True
            skipped.append(name)
            notes.append(f"{name}: not started - {TIMED_OUT_REASON}")
            continue
        if remaining < pol.min_remaining_to_start_package_s:
            bounded = True
            skipped.append(name)
            notes.append(
                f"{name}: not started - only {remaining:.1f}s of budget left, "
                f"below the {pol.min_remaining_to_start_package_s:.0f}s bar"
            )
            continue
        run = run_bounded(
            command,
            cwd=workspace_root,
            timeout_s=min(pol.max_package_s, remaining),
            label=name,
            runner=the_runner,
        )
        runs.append(run)
        if run.timed_out:
            bounded = True
            skipped.append(name)
            notes.append(f"{name}: {run.note or TIMED_OUT_REASON}")
            continue
        if run.exit_code is None:
            # The command never ran. This is not a pass and not a failure.
            bounded = True
            skipped.append(name)
            notes.append(f"{name}: not run - {run.note or 'no exit code'}")
            continue
        if run.exit_code == 0:
            verified.append(name)
        else:
            skipped.append(name)
            notes.append(f"{name}: exit {run.exit_code} - not verified")

    return VerificationRunReport(
        mode="bounded_packages",
        verified_packages=tuple(verified),
        skipped_packages=tuple(skipped),
        wall_clock_s=clock() - started,
        bounded=bounded,
        notes=tuple(notes),
        per_package=tuple(runs),
    )


# --------------------------------------------------------------------------
# the three measured costs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredCost:
    """One measured arm, with the packages it covered."""

    mode: str
    wall_clock_s: float
    packages_run: Tuple[str, ...] = ()
    packages_skipped: Tuple[str, ...] = ()
    timed_out: bool = False
    exit_code: Optional[int] = None
    note: str = ""


def measure_verification_costs(
    workspace_root: str,
    commands_by_mode: Dict[str, Dict[str, str]],
    *,
    policy: Optional[VerificationPolicy] = None,
    runner: Optional[Runner] = None,
) -> Tuple[CostNumbers, Tuple[MeasuredCost, ...]]:
    """Measure each of the three arms, for real, on the caller's workspace.

    ``commands_by_mode`` maps a mode to ``{package: command}``. An arm with no
    commands is measured as ``None`` and reported UNMEASURED — it is never
    reported as free. A cost is quoted as a wall-clock number whether or not the
    packages in that arm actually passed, because the question "what does this
    mode COST" is a different question from "does this mode pass"; the arms'
    own pass/fail state travels beside the numbers in ``MeasuredCost``.
    """
    pol = policy or VerificationPolicy()
    the_runner = runner or subprocess_argv_runner()
    measured: List[MeasuredCost] = []
    values: Dict[str, Optional[float]] = {}

    for mode in COST_MODES:
        cmds = commands_by_mode.get(mode) or {}
        if not cmds:
            values[mode] = None
            continue
        started = time.monotonic()
        report = run_packages_bounded(
            list(cmds.items()),
            workspace_root=workspace_root,
            policy=pol,
            runner=the_runner,
        )
        elapsed = time.monotonic() - started
        values[mode] = elapsed
        failed = [r for r in report.per_package if r.exit_code not in (0, None)]
        measured.append(
            MeasuredCost(
                mode=mode,
                wall_clock_s=elapsed,
                packages_run=report.verified_packages,
                packages_skipped=report.skipped_packages,
                timed_out=report.bounded,
                exit_code=failed[0].exit_code if failed else (0 if report.verified_packages else None),
                note="; ".join(report.notes),
            )
        )

    basis = (
        f"{len(commands_by_mode)} arm(s) on {workspace_root} "
        f"with a {pol.max_wall_clock_s:.0f}s budget; wall clock including "
        "process dispatch"
    )
    numbers = CostNumbers(
        full_suite_s=values.get("full_suite"),
        per_package_selection_s=values.get("package_selection"),
        changed_package_only_s=values.get("changed_package_only"),
        basis=basis,
        measured=any(v is not None for v in values.values()),
    )
    return numbers, tuple(measured)


def package_command(package_root: str, package_manifest: Dict[str, Any]) -> str:
    """The command a package's own ``test`` script names, or pytest's default.

    Read from the manifest the workspace discovery already found, so a
    monorepo that declares ``npm test`` is not measured on ``pytest``.
    """
    scripts = package_manifest.get("scripts") or {}
    test = scripts.get("test")
    if isinstance(test, str) and test.strip():
        return test.strip()
    return "python -m pytest -q"
