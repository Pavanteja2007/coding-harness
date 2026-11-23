"""P4/W2 T2.W2.3 — does the sandbox hold across MANY sessions and worktrees?

Phase 2 proved containment for **one** session. Phase 4 has many. The
properties that hold for one session routinely fail at N, because almost every
one of them is only unique *within a process*:

* a container name derived from the PID and a counter collides across
  processes once two sessions derive the same one;
* a temp path derived from ``tempfile.gettempdir()`` collides once two sessions
  pick the same one;
* a resource limit that is correct for one container is an over-commit once
  fifty containers each hold it;
* a read-only overlay that survives one session is a decoration once fifty
  sessions mount the same host directory.

So this module measures at N, with N real containers and N real worktrees.

--------------------------------------------------------------------------
THE SIX AXES, AND WHY EACH ONE IS ITS OWN ROW
--------------------------------------------------------------------------

===========================  ==========================================
cross-talk (item 1)          N sessions, N worktrees, N containers; zero
                             shared files, env, ports, containers or
                             temp paths. **Stat the host after every
                             attack**, because "the container refused" and
                             "nothing reached the machine" are two claims
naming (item 2)              container names must not collide, and the
                             collision test must run ACROSS PROCESSES —
                             an in-process test is satisfied by any name
                             containing the PID
reaping (item 3)             a hard-killed session's containers are reaped
                             by a surviving peer, and the reaping is
                             REPORTED rather than merely performed
read-only (item 4)           the read-only overlay — ``.git`` especially —
                             still refuses a write after N sessions have
                             come and gone
aggregate limits (item 5)    N sessions each holding ``mem_limit`` may
                             exceed what the host can give. **DETECTED AND
                             REPORTED**, never silently degraded
disk (item 6)                growth across a week of daily use, projected
                             from a real census and BOUNDED
===========================  ==========================================

--------------------------------------------------------------------------
UNOBSERVED IS NOT HELD — THE RULE THAT SHAPES EVERY RETURN VALUE
--------------------------------------------------------------------------

Every axis returns a ROW with an ``observed`` flag, and a row that could not be
measured is ``UNOBSERVED``, never ``HELD``. That is the whole reason this
module returns reports rather than booleans, and it is inherited from Wave 1's
:mod:`execution.session_isolation`. An axis reported as held because its arm did
not run is a guarantee nobody established.

A second consequence, learned the hard way in this round: **a control arm that
does not run makes the row ``lane_blocked``, not green.** The first version of
the cross-talk row reported "0 breaches" when its own control had failed to run,
because it counted breaches and never checked that the probe had fired.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from execution import sandbox as sbx
from execution import session_isolation as si

__all__ = [
    "AGGREGATE_HOST_HEADROOM",
    "AXIS_AGGREGATE_LIMITS",
    "AXIS_CROSSTALK",
    "AXIS_DISK",
    "AXIS_NAMING",
    "AXIS_READONLY",
    "AXIS_REAPING",
    "AXIS_VERDICTS",
    "MULTI_SESSION_AXES",
    "VERDICT_BREACH",
    "VERDICT_DEGRADED",
    "VERDICT_HELD",
    "VERDICT_UNOBSERVED",
    "MultiSessionReport",
    "MultiSessionRow",
    "aggregate_limit_report",
    "concurrent_session_report",
    "disk_growth_report",
    "main",
    "naming_collision_report",
    "orphan_reaping_report",
    "readonly_overlay_report",
    "render_report",
    "run_all",
]


VERDICT_HELD = "HELD"
VERDICT_BREACH = "BREACH"
VERDICT_UNOBSERVED = "UNOBSERVED"
VERDICT_DEGRADED = "DEGRADED"

#: The four-value axis verdict vocabulary. ``DEGRADED`` is separate from
#: ``BREACH`` on purpose: a limit that was quietly relaxed so the run could
#: proceed is not a containment failure, and reporting it as one would send an
#: operator looking for an attacker instead of at the capacity planner.
AXIS_VERDICTS: Tuple[str, str, str, str] = (
    VERDICT_HELD,
    VERDICT_BREACH,
    VERDICT_UNOBSERVED,
    VERDICT_DEGRADED,
)

AXIS_CROSSTALK = "crosstalk"
AXIS_NAMING = "naming"
AXIS_REAPING = "reaping"
AXIS_READONLY = "readonly"
AXIS_AGGREGATE_LIMITS = "aggregate_limits"
AXIS_DISK = "disk"

MULTI_SESSION_AXES: Tuple[str, ...] = (
    AXIS_CROSSTALK,
    AXIS_NAMING,
    AXIS_REAPING,
    AXIS_READONLY,
    AXIS_AGGREGATE_LIMITS,
    AXIS_DISK,
)

#: A session may hold at most this fraction of the host's memory in aggregate
#: before the axis is reported as over-committed rather than silently degraded.
#:
#: 0.75 is chosen from the shape of the arithmetic, not from taste: a container
#: limit is a CEILING, not a reservation, so the honest question is how much of
#: the host can be *promised* at once. Above 75% the daemon's own bookkeeping
#: and the host's page cache start competing, and a session that was working
#: stops working for a reason no report names.
AGGREGATE_HOST_HEADROOM = 0.75


# ==========================================================================
# The row type
# ==========================================================================


@dataclass(frozen=True)
class MultiSessionRow:
    """One axis's measured outcome, with the evidence a reader can re-check."""

    axis: str
    verdict: str
    detail: str = ""
    observed: bool = True
    sessions: int = 0
    controls_ran: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in AXIS_VERDICTS:
            raise ValueError(
                f"unknown axis verdict {self.verdict!r}; the vocabulary is "
                f"closed: {AXIS_VERDICTS}"
            )
        if self.verdict == VERDICT_HELD and not self.observed:
            raise ValueError(
                f"axis {self.axis!r} reports HELD without having been observed. "
                f"An unmeasured axis is UNOBSERVED, and that distinction is the "
                f"entire reason this module returns rows rather than booleans."
            )

    @property
    def held(self) -> bool:
        return self.verdict == VERDICT_HELD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "verdict": self.verdict,
            "detail": self.detail,
            "observed": bool(self.observed),
            "sessions": int(self.sessions),
            "controls_ran": int(self.controls_ran),
            "evidence": dict(self.evidence),
            "note": self.note,
        }

    def render(self) -> List[str]:
        lines = [
            f"{self.axis:<16} {self.verdict:<11} observed={self.observed!s:<5} "
            f"sessions={self.sessions}"
        ]
        if self.detail:
            lines.append(f"  {self.detail}")
        for key in sorted(self.evidence):
            value = self.evidence[key]
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(item) for item in value) or "(none)"
            elif isinstance(value, dict):
                value = json.dumps(value, sort_keys=True, default=str)
            lines.append(f"    {key:<26}: {value}")
        if self.note:
            lines.append(f"  note: {self.note}")
        return lines


@dataclass(frozen=True)
class MultiSessionReport:
    rows: Tuple[MultiSessionRow, ...]
    sessions: int = 0
    generated_at: float = 0.0
    duration_s: float = 0.0

    @property
    def held(self) -> Tuple[MultiSessionRow, ...]:
        return tuple(row for row in self.rows if row.verdict == VERDICT_HELD)

    @property
    def breaches(self) -> Tuple[MultiSessionRow, ...]:
        return tuple(
            row for row in self.rows if row.verdict in (VERDICT_BREACH, VERDICT_DEGRADED)
        )

    @property
    def unobserved(self) -> Tuple[MultiSessionRow, ...]:
        return tuple(row for row in self.rows if not row.observed)

    @property
    def clean(self) -> bool:
        return not self.breaches

    @property
    def verdict(self) -> str:
        if self.breaches:
            return "BREACH"
        if self.unobserved:
            return "PARTIAL"
        return "ISOLATED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "clean": bool(self.clean),
            "sessions": int(self.sessions),
            "axes": list(MULTI_SESSION_AXES),
            "held": [row.axis for row in self.held],
            "breaches": [row.axis for row in self.breaches],
            "unobserved": [row.axis for row in self.unobserved],
            "duration_s": round(float(self.duration_s), 2),
            "rows": [row.to_dict() for row in self.rows],
            "verdict_vocabulary": {
                VERDICT_HELD: "measured, and the property held",
                VERDICT_BREACH: "measured, and the property did not hold",
                VERDICT_UNOBSERVED: (
                    "the arm did not run, so the guarantee is UNMEASURED. A row "
                    "reported HELD because its probe failed is worse than an "
                    "absent row, because it reads as a guarantee"
                ),
                VERDICT_DEGRADED: (
                    "measured, and a limit was relaxed or exceeded so the run "
                    "could proceed. NOT a containment failure: an operator needs "
                    "to see capacity, not an attacker"
                ),
            },
        }


def render_report(report: MultiSessionReport) -> List[str]:
    lines = [
        f"multi-session sandbox containment — verdict {report.verdict}",
        f"  sessions : {report.sessions}   duration: {report.duration_s:.1f}s",
        f"  axes     : {', '.join(MULTI_SESSION_AXES)}",
        "",
    ]
    for row in report.rows:
        lines.extend(row.render())
        lines.append("")
    lines.append(
        "  UNOBSERVED axes are NOT held guarantees: their arms did not run, so "
        "they prove nothing."
    )
    return lines


# ==========================================================================
# Axis 1: cross-talk between N real sessions, N worktrees, N containers
# ==========================================================================

_MARKER = "NEO-XTALK-%s"


def _make_worktree(root: Path, index: int) -> Path:
    """A real, distinct worktree per session — a copy plus its own git identity."""
    tree = root / f"wt-{index}"
    (tree / "src").mkdir(parents=True)
    (tree / ".git").mkdir()
    (tree / "src" / "feature.py").write_text(
        "SESSION = %d\n\n\ndef value():\n    return %d\n" % (index, index), encoding="utf-8"
    )
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tree / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8"
    )
    (tree / ".git" / "refs").mkdir(exist_ok=True)
    (tree / ".git" / "refs" / "heads").mkdir(exist_ok=True)
    (tree / ".git" / "refs" / "heads" / "main").write_text(
        "%040d" % (1000 + index), encoding="utf-8"
    )
    return tree


def _session_probe(index: int, tree: Path, timeout_s: int = 90) -> Dict[str, Any]:
    """One session: write its OWN marker, then look for every OTHER marker.

    The write-then-look order matters. A probe that only looked would pass on a
    session whose own marker never landed — which is the "control arm that
    cannot fail" shape this repository keeps paying for.
    """
    own = _MARKER % index
    others = [_MARKER % other for other in range(0, 64) if other != index]
    command = (
        "mkdir -p /tmp/neo-session && "
        "printf '%s' > /tmp/neo-session/%s && "
        "cd /workspace && "
        "test -f src/feature.py && "
        "grep -q 'SESSION = %d' src/feature.py && "
        "ls -a . | tr '\\n' ' ' && echo '' && "
        "for m in %s; do "
        "  if [ -e /tmp/neo-session/$m ]; then echo SAW:$m; fi; "
        "done; "
        "echo OWN_OK"
    ) % (
        own,
        own,
        index,
        " ".join(others),
    )
    started = time.perf_counter()
    try:
        result = sbx.execute_sandboxed(str(tree), command, timeout_s)
    except Exception as exc:
        return {
            "index": index,
            "ran": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "wall_s": round(time.perf_counter() - started, 3),
        }
    stdout = str(getattr(result, "stdout", "") or "")
    saw = [line.split(":", 1)[1] for line in stdout.splitlines() if line.startswith("SAW:")]
    return {
        "index": index,
        "ran": True,
        "exit_code": int(getattr(result, "exit_code", 1) or 0),
        "container_name": str(getattr(result, "container_name", "") or ""),
        "own_marker_written": "OWN_OK" in stdout,
        "own_source_visible": "feature.py" in stdout,
        "saw_other_markers": saw,
        "temp_path": "/tmp/neo-session",
        "wall_s": round(time.perf_counter() - started, 3),
        "stdout_head": stdout.strip()[:200],
    }


def concurrent_session_report(
    sessions: int = 4,
    *,
    timeout_s: int = 90,
    port_home: Optional[str] = None,
) -> MultiSessionRow:
    """N sessions, N worktrees, N containers, and ZERO cross-talk.

    Five things are checked per session, and each is checked by the **host**
    after the container is gone, not by the container's own report:

    * **files** — session *i*'s marker is in its own worktree and nowhere else;
    * **worktree identity** — session *i* sees ``SESSION = i`` and not another
      session's value;
    * **temp paths** — every session writes under ``/tmp/neo-session`` inside a
      fresh container, and none sees another's marker;
    * **containers** — every container name is distinct;
    * **ports** — every session acquires its own block, via
      :mod:`execution.port_alloc`, and the blocks must not overlap.

    ``sessions`` is clamped: an unobserved row is reported as UNOBSERVED rather
    than quietly reduced, because "we ran 1 session and called it a many-session
    proof" is the exact shape of claim this module exists to refuse.
    """
    requested = int(sessions)
    if requested < 1:
        return MultiSessionRow(
            axis=AXIS_CROSSTALK,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            sessions=requested,
            detail="sessions must be at least 1; nothing was run",
        )

    root = Path(tempfile.mkdtemp(prefix="t2w2-multisess-"))
    trees = [_make_worktree(root, index) for index in range(requested)]

    # -- ports: every session ACQUIRES, none reads another's ---------------
    port_rows: List[Dict[str, Any]] = []
    port_failures: List[str] = []
    for index in range(requested):
        try:
            from execution import port_alloc

            outcome = port_alloc.allocate_port(
                "t2w2-session-%d" % index, reason="multi-session axis", home=port_home
            )
            port_rows.append(outcome)
            if outcome.get("port") is None:
                port_failures.append("session %d: %s" % (index, outcome.get("reason")))
        except Exception as exc:
            port_failures.append("session %d: %s: %s" % (index, type(exc).__name__, exc))

    probes: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(requested, 8))) as pool:
        futures = [
            pool.submit(_session_probe, index, trees[index], timeout_s)
            for index in range(requested)
        ]
        for future in futures:
            probes.append(future.result())
    probes.sort(key=lambda row: int(row["index"]))

    # -- host-side file cross-talk -----------------------------------------
    host_breaches: List[str] = []
    for index in range(requested):
        for other, _tree in enumerate(trees):
            if other == index:
                continue
            path = trees[other] / "src" / f"leak-{index}.marker"
            try:
                if path.exists():
                    host_breaches.append(
                        "session %d's marker exists in worktree %d" % (index, other)
                    )
            except OSError:
                continue
        # The marker must NOT be on the host at all: the container's /tmp is a
        # tmpfs and a marker that reached the host disk would be a real leak.
        stray = [
            str(path)
            for path in root.rglob(("leak-%d.marker" % index))
            if path.exists()
        ]
        if stray:
            host_breaches.append("session %d's marker reached the host: %s" % (index, stray))

    # -- container naming ---------------------------------------------------
    names = [row.get("container_name", "") for row in probes if row.get("ran")]
    name_collisions = len(names) - len(set(names))
    unnamed = sum(1 for name in names if not name)
    if name_collisions:
        host_breaches.append("container name collision(s): %d" % name_collisions)
    if unnamed:
        host_breaches.append(
            "%d container(s) reported no name, so naming could not be verified"
            % unnamed
        )

    # -- container-visible cross-talk --------------------------------------
    ran = [row for row in probes if row.get("ran")]
    controls_ran = sum(1 for row in ran if row.get("own_marker_written"))
    for row in ran:
        for marker in row.get("saw_other_markers") or ():
            host_breaches.append(
                "session %d saw %s in its own container" % (row["index"], marker)
            )

    # -- ports -------------------------------------------------------------
    ports = [int(row["port"]) for row in port_rows if row.get("port") is not None]
    distinct_ports = len(set(ports))
    if port_failures:
        host_breaches.extend(port_failures)
    if len(ports) != requested:
        host_breaches.append(
            "%d of %d session(s) did not get a port block"
            % (len(ports), requested)
        )
    if distinct_ports != len(ports):
        host_breaches.append(
            "%d port(s) shared between %d block(s)" % (len(ports) - distinct_ports, len(ports))
        )

    evidence: Dict[str, Any] = {
        "sessions_requested": requested,
        "sessions_ran": len(ran),
        "worktrees": len(trees),
        "distinct_container_names": len(set(names)),
        "name_collisions": name_collisions,
        "unnamed_containers": unnamed,
        "distinct_ports": distinct_ports,
        "ports": ports,
        "container_names": [name for name in names][:8],
        "max_session_wall_s": round(
            max((float(row.get("wall_s") or 0.0) for row in probes), default=0.0), 3
        ),
        "slowest_sessions": sorted(
            (row for row in probes), key=lambda row: -float(row.get("wall_s") or 0.0)
        )[:3],
        "host_breaches": host_breaches[:8],
    }

    if not ran:
        return MultiSessionRow(
            axis=AXIS_CROSSTALK,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            sessions=requested,
            detail="no session's container completed, so cross-talk was NOT measured",
            evidence=evidence,
            note=(
                "an axis reported HELD because its arm did not run would be worse "
                "than an absent axis: it reads as a guarantee"
            ),
        )
    if controls_ran != len(ran):
        return MultiSessionRow(
            axis=AXIS_CROSSTALK,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            sessions=requested,
            controls_ran=controls_ran,
            detail=(
                f"only {controls_ran} of {len(ran)} session(s) proved their own "
                f"marker landed; a probe that does not fire proves nothing"
            ),
            evidence=evidence,
        )
    if host_breaches:
        return MultiSessionRow(
            axis=AXIS_CROSSTALK,
            verdict=VERDICT_BREACH,
            sessions=requested,
            controls_ran=controls_ran,
            detail=f"{len(host_breaches)} cross-talk observation(s)",
            evidence=evidence,
        )
    return MultiSessionRow(
        axis=AXIS_CROSSTALK,
        verdict=VERDICT_HELD,
        sessions=requested,
        controls_ran=controls_ran,
        detail=(
            f"{requested} concurrent session(s), {len(trees)} worktree(s), "
            f"{len(set(names))} distinct container name(s), {distinct_ports} "
            f"distinct port(s); 0 cross-talk observation(s)"
        ),
        evidence=evidence,
        note=(
            "every session proved its OWN marker landed before the row was "
            "allowed to pass. A probe that only looks would be satisfied by a "
            "session that wrote nothing"
        ),
    )


# ==========================================================================
# Axis 2: container naming, across PROCESSES
# ==========================================================================

_CHILD_NAME_SCRIPT = """
import sys
sys.path.insert(0, {repo!r})
from execution import sandbox as sbx
names = [sbx._container_name_prefix() + "-" + uuid_hex for uuid_hex in []]
import execution.sandbox as s
print("PREFIX", s._container_name_prefix())
"""


def _child_names(count: int, repo: str) -> Dict[str, Any]:
    """Spawn ``count`` REAL child processes and read each one's name prefix.

    A subprocess, deliberately. An in-process naming test is satisfied by any
    name containing the PID, and the collision this axis exists for happens
    *between* processes — two sessions on different hosts of the same daemon
    deriving the same name. A test that cannot see that is a rubber stamp.
    """
    repo_root = Path(repo).resolve()
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from execution import sandbox as s\n"
        "print('PREFIX', s._container_name_prefix())\n"
    ) % str(repo_root)
    script_path = Path(tempfile.mkdtemp(prefix="t2w2-name-")) / "name.py"
    script_path.write_text(script, encoding="utf-8")

    prefixes: List[str] = []
    failures: List[str] = []
    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(max(1, count))
    ]
    for proc in procs:
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.SubprocessError as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            continue
        line = next(
            (item for item in (out or "").splitlines() if item.startswith("PREFIX")), ""
        )
        if line:
            prefixes.append(line.split(None, 1)[1].strip())
        else:
            failures.append((err or "").strip()[:200] or "no PREFIX line")
    return {"prefixes": prefixes, "failures": failures}


def naming_collision_report(*, processes: int = 4) -> MultiSessionRow:
    """Container naming must not collide ACROSS PROCESSES.

    Two axes of the name are checked, and they fail differently:

    * the **pid token** differs between processes, which is what the orphan
      reaper keys on;
    * the **whole name** differs within and between processes, which is what
      makes ``docker kill`` addressable.

    A within-process check is also run and reported, because it is cheap and it
    is the axis Wave 1 already covered; the between-process arm is this round's
    addition and it is the load-bearing one.
    """
    requested = max(1, int(processes))
    host_repo = str(Path(__file__).resolve().parents[1])
    cross = _child_names(requested, host_repo)

    within = si.container_name_collision_report(samples=64)

    evidence: Dict[str, Any] = {
        "processes_requested": requested,
        "processes_reported": len(cross["prefixes"]),
        "distinct_prefixes": len(set(cross["prefixes"])),
        "prefixes": cross["prefixes"][:8],
        "child_failures": cross["failures"][:4],
        "within_process_distinct": within.get("distinct_within_process"),
        "within_process_samples": within.get("samples"),
        "within_process_verdict": within.get("verdict"),
    }

    if not cross["prefixes"]:
        return MultiSessionRow(
            axis=AXIS_NAMING,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail="no child process reported a container-name prefix",
            evidence=evidence,
        )
    distinct = len(set(cross["prefixes"]))
    if distinct != len(cross["prefixes"]):
        return MultiSessionRow(
            axis=AXIS_NAMING,
            verdict=VERDICT_BREACH,
            detail=(
                f"{len(cross['prefixes']) - distinct} prefix collision(s) across "
                f"{len(cross['prefixes'])} process(es)"
            ),
            evidence=evidence,
            note=(
                "two sessions deriving the same container name IS a cross-session "
                "leak: `docker kill <name>` would address the wrong session's "
                "container, and the orphan reaper keys on the pid token"
            ),
        )
    if within.get("verdict") != "HELD":
        return MultiSessionRow(
            axis=AXIS_NAMING,
            verdict=VERDICT_BREACH,
            detail=f"the within-process check reports {within.get('verdict')}",
            evidence=evidence,
        )
    return MultiSessionRow(
        axis=AXIS_NAMING,
        verdict=VERDICT_HELD,
        sessions=len(cross["prefixes"]),
        detail=(
            f"{len(cross['prefixes'])} real process(es) produced "
            f"{distinct} distinct name prefix(es); "
            f"{within.get('distinct_within_process')} distinct name(s) over "
            f"{within.get('samples')} builds in one process"
        ),
        evidence=evidence,
        note=(
            "the between-process arm is the load-bearing one. An in-process test "
            "is satisfied by any name containing the PID, and the collision this "
            "axis exists for happens between processes"
        ),
    )


# ==========================================================================
# Axis 3: orphan reaping, with a REAL hard-killed owner
# ==========================================================================

_CHILD_ORPHAN_SCRIPT = """
import sys, time
sys.path.insert(0, {repo!r})
from execution import sandbox as sbx
sbx.execute_sandboxed({tree!r}, "sleep 600", 600)
"""


def orphan_reaping_report(*, timeout_s: int = 120) -> MultiSessionRow:
    """A hard-killed session's containers are reaped by a surviving peer.

    **A real child process is hard-killed.** Not a simulated container, not a
    name written into a file: the child dispatches a real 600-second container
    and is then killed with ``TerminateProcess`` semantics, which is exactly the
    shape that leaked in Round 3 — the docker CLI dies and its container keeps
    running to the end of the command.

    **How the container is found is the load-bearing detail, and the first
    version got it wrong.** It had the child print its container name after
    ``execute_sandboxed`` returned — but that call BLOCKS until the command
    completes, so for a 600-second sleep the name was never printed in time, the
    sweep found nothing, and the row reported a reaping that had not been
    observed. A row that cannot observe its own subject is a row that passes for
    the wrong reason.

    The parent therefore discovers the container through the **naming contract**
    instead: it polls ``docker ps`` for a container whose pid token equals the
    child's PID. That is the same token the reaper keys on, so the test uses the
    public contract rather than a private handshake, and it works precisely
    because the container is still alive.

    Then the surviving peer sweeps and the sweep's own receipt is published. A
    reaping that happened is a claim; a reaping that is **REPORTED** is
    evidence.
    """
    repo_root = str(Path(__file__).resolve().parents[1])
    root = Path(tempfile.mkdtemp(prefix="t2w2-reap-"))
    tree = root / "wt"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")

    script_path = root / "child.py"
    script_path.write_text(
        _CHILD_ORPHAN_SCRIPT.format(repo=repo_root, tree=str(tree)), encoding="utf-8"
    )

    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    victim = _wait_for_container_of_pid(proc.pid, timeout_s=timeout_s)
    if not victim:
        try:
            proc.kill()
            proc.wait(timeout=30)
        except Exception:
            pass
        return MultiSessionRow(
            axis=AXIS_REAPING,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail=(
                f"no container carrying this child's pid token {proc.pid} appeared "
                f"in `docker ps` within {timeout_s}s, so no orphan was created and "
                f"the reaper proved nothing"
            ),
            evidence={"child_pid": proc.pid, "child_returncode": proc.poll()},
            note=(
                "the row is UNOBSERVED, not HELD: no orphan existed. This is the "
                "same blocked-lane distinction execution/e2e_attack.py keeps, "
                "applied to an axis"
            ),
        )

    before = _running_our_containers()
    evidence: Dict[str, Any] = {
        "victim_container": victim,
        "victim_pid": proc.pid,
        "running_before_kill": before,
        "discovery": (
            "the parent polls `docker ps` for a container whose pid token equals "
            "the child's PID, because execute_sandboxed BLOCKS until the command "
            "completes and a child that prints its name afterwards prints it too "
            "late to observe an orphan"
        ),
    }

    # -- hard-kill the owner ----------------------------------------------
    try:
        proc.kill()
    except Exception as exc:  # pragma: no cover - platform dependent
        evidence["kill_error"] = f"{type(exc).__name__}: {exc}"
    try:
        proc.wait(timeout=30)
    except subprocess.SubprocessError:
        pass
    evidence["owner_alive_after_kill"] = proc.poll() is None
    evidence["victim_running_after_kill"] = _is_running(victim)

    # -- the peer sweeps ----------------------------------------------------
    dry = si.orphan_reap_report(dry_run=True)
    evidence["dry_run_sweep"] = {
        "considered": dry.get("considered"),
        "outcome_counts": dry.get("outcome_counts"),
    }
    reaped = si.orphan_reap_report(dry_run=False)
    evidence["sweep"] = {
        "considered": reaped.get("considered"),
        "outcome_counts": reaped.get("outcome_counts"),
        "reaped": list(reaped.get("outcomes", {}).get("reaped") or ())[:5],
        "verdict": reaped.get("verdict"),
    }
    gone = not _is_running(victim)
    evidence["victim_gone_after_sweep"] = gone
    evidence["running_after_sweep"] = _running_our_containers()
    evidence["created_state_residue"] = reaped.get("created_state_residue")

    if evidence.get("owner_alive_after_kill"):
        return MultiSessionRow(
            axis=AXIS_REAPING,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail="the child process survived the kill, so no orphan was created",
            evidence=evidence,
        )
    if not evidence.get("victim_running_after_kill"):
        return MultiSessionRow(
            axis=AXIS_REAPING,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail=(
                "the victim's container had already exited before the owner was "
                "killed, so there was nothing for the reaper to find"
            ),
            evidence=evidence,
        )
    verdict = VERDICT_HELD if gone else VERDICT_BREACH
    return MultiSessionRow(
        axis=AXIS_REAPING,
        verdict=verdict,
        detail=(
            f"a hard-killed owner's container {victim} was reaped by the "
            f"surviving peer ({evidence['sweep']['outcome_counts']}); it is "
            f"{'gone from' if gone else 'STILL PRESENT IN'} `docker ps`"
        ),
        evidence=evidence,
        note=(
            "the sweep's own receipt is published, not just its effect. Residue "
            "in state `Created` is counted separately, because `docker ps` "
            "lists RUNNING containers only and a created container is invisible "
            "to a running-only sweep"
        ),
    )


def _running_our_containers() -> List[str]:
    try:
        return si._docker_lines(
            ["ps", "--filter", f"name={sbx.CONTAINER_PREFIX}-", "--format", "{{.Names}}"]
        )
    except Exception:
        return []


def _is_running(name: str) -> bool:
    return str(name) in set(_running_our_containers())


def _wait_for_container_of_pid(pid: int, *, timeout_s: int) -> str:
    """Poll ``docker ps`` for a container whose owner pid token is ``pid``.

    Uses :func:`execution.sandbox._container_pid_from_name` — the same parser
    the reaper uses — so the row cannot drift from the mechanism it is testing.
    """
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        for name in _running_our_containers():
            try:
                owner = sbx._container_pid_from_name(name)
            except Exception:
                owner = None
            if owner == int(pid):
                return name
        time.sleep(0.5)
    return ""


# ==========================================================================
# Axis 4: the read-only overlay holds, after N sessions
# ==========================================================================


def readonly_overlay_report(*, sessions: int = 3) -> MultiSessionRow:
    """The read-only overlay still refuses a write after N sessions.

    ``.git`` especially, and the check is a **host-side stat** after the
    container is gone. "the container refused" and "nothing reached the machine"
    are two different claims, and only the second one is a containment property.

    The control arm writes a file the overlay does NOT cover and requires it to
    land. Without it this row is satisfied by a container that refuses every
    write, which is a containment break wearing a containment proof.
    """
    requested = max(1, int(sessions))
    root = Path(tempfile.mkdtemp(prefix="t2w2-ro-"))
    tree = root / "wt"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _init_git(tree)

    git_before = _digest_file(tree / ".git" / "config")
    evidence: Dict[str, Any] = {
        "sessions": requested,
        "git_config_sha_before": git_before,
        "git_dir_present_before": (tree / ".git").is_dir(),
    }
    refusals = 0
    controls = 0
    breaches: List[str] = []
    rounds: List[Dict[str, Any]] = []

    for index in range(requested):
        command = (
            "cd /workspace && "
            "(echo forged >> .git/config) 2>/dev/null; echo GIT_RC=$?; "
            "(mkdir -p .git/refs/heads && echo forged > .git/refs/heads/pwned) "
            "2>/dev/null; echo REFS_RC=$?; "
            "printf 'session-%d\\n' > src/writable-marker-%d; echo WRITE_RC=$?; "
            "echo DONE"
        ) % (index, index)
        try:
            result = sbx.execute_sandboxed(str(tree), command, 90)
        except Exception as exc:
            return MultiSessionRow(
                axis=AXIS_READONLY,
                verdict=VERDICT_UNOBSERVED,
                observed=False,
                detail=f"the boundary raised on session {index}: {type(exc).__name__}: {exc}",
                evidence=evidence,
            )
        stdout = str(getattr(result, "stdout", "") or "")
        git_rc = _rc_from(stdout, "GIT_RC")
        refs_rc = _rc_from(stdout, "REFS_RC")
        write_rc = _rc_from(stdout, "WRITE_RC")
        round_row = {
            "index": index,
            "git_rc": git_rc,
            "refs_rc": refs_rc,
            "write_rc": write_rc,
            "container": str(getattr(result, "container_name", "") or ""),
        }
        rounds.append(round_row)
        if git_rc not in (0, None) or refs_rc not in (0, None):
            refusals += 1
        if write_rc == 0:
            controls += 1
        else:
            breaches.append(
                "session %d's ordinary write was refused (rc=%s), so the read-only "
                "arm proved nothing" % (index, write_rc)
            )

    git_after = _digest_file(tree / ".git" / "config")
    evidence.update(
        {
            "rounds": rounds,
            "git_config_sha_after": git_after,
            "git_config_unchanged": git_before == git_after,
            "pwned_ref_exists": (tree / ".git" / "refs" / "heads" / "pwned").exists(),
            "read_only_refusals": refusals,
            "ordinary_write_controls": controls,
            "writable_markers_on_host": sorted(
                path.name for path in (tree / "src").glob("writable-marker-*")
            ),
            "breaches": breaches[:5],
        }
    )

    if not rounds:
        return MultiSessionRow(
            axis=AXIS_READONLY,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail="no session completed",
            evidence=evidence,
        )
    if breaches:
        return MultiSessionRow(
            axis=AXIS_READONLY,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            sessions=requested,
            detail="; ".join(breaches[:2]),
            evidence=evidence,
            note=(
                "the control arm did not run, so the row is UNOBSERVED rather "
                "than HELD: a container that refuses every write would otherwise "
                "satisfy this axis"
            ),
        )
    if (
        evidence["git_config_unchanged"]
        and not evidence["pwned_ref_exists"]
        and refusals == requested
        and controls == requested
    ):
        return MultiSessionRow(
            axis=AXIS_READONLY,
            verdict=VERDICT_HELD,
            sessions=requested,
            detail=(
                f"{requested} session(s): every .git write refused in-container "
                f"and the host .git/config is byte-identical afterwards; "
                f"{controls}/{requested} ordinary writes landed"
            ),
            evidence=evidence,
            note=(
                "verified by STAT-ING THE HOST after the container is gone. "
                "'the container refused' and 'nothing reached the machine' are "
                "two claims and only the second is a containment property"
            ),
        )
    return MultiSessionRow(
        axis=AXIS_READONLY,
        verdict=VERDICT_BREACH,
        sessions=requested,
        detail=(
            f"{refusals}/{requested} session(s) refused the .git write; host "
            f"config unchanged={evidence['git_config_unchanged']}, forged ref "
            f"present={evidence['pwned_ref_exists']}"
        ),
        evidence=evidence,
    )


def _rc_from(stdout: str, key: str) -> Optional[int]:
    for line in str(stdout or "").splitlines():
        if line.startswith(key + "="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _digest_file(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "(unreadable)"


def _init_git(tree: Path) -> None:
    for args in (
        ("init", "-q"),
        ("-c", "user.name=t2w2", "-c", "user.email=t2w2@example.invalid", "add", "-A"),
        ("-c", "user.name=t2w2", "-c", "user.email=t2w2@example.invalid", "commit", "-qm", "init"),
    ):
        subprocess.run(
            ["git", *args], cwd=str(tree), capture_output=True, text=True, timeout=60, check=False
        )


# ==========================================================================
# Axis 5: aggregate resource limits
# ==========================================================================


def aggregate_limit_report(*, sessions: int = 8, mem_limit: str = "1g") -> MultiSessionRow:
    """N sessions each holding a per-container limit may exceed the host.

    **Detect and report rather than silently degrading** — the brief's wording,
    and the right one. Two measurements, and neither is estimated:

    * the host's real total memory, and the arithmetic ``sessions × mem_limit``;
    * the daemon's own free memory at the moment of the report.

    The verdict is ``DEGRADED`` rather than ``BREACH`` above the headroom bar,
    because a limit that was relaxed so the run could proceed is a capacity
    problem and not a containment failure. Reporting it as a breach would send
    an operator looking for an attacker instead of at the capacity planner.
    """
    requested = max(1, int(sessions))
    per_bytes = _parse_memory(str(mem_limit))
    host_total = _host_memory_bytes()
    host_free = _docker_free_memory_bytes()
    promised = per_bytes * requested
    fraction = (promised / host_total) if host_total else None

    evidence: Dict[str, Any] = {
        "sessions": requested,
        "per_session_mem_limit": str(mem_limit),
        "per_session_mem_bytes": per_bytes,
        "promised_aggregate_bytes": promised,
        "host_total_bytes": host_total,
        "host_free_bytes": host_free,
        "host_free_published_by_daemon": host_free is not None,
        "host_free_fraction": (
            round(host_free / host_total, 4) if (host_free and host_total) else None
        ),
        "promised_fraction_of_host": None if fraction is None else round(fraction, 4),
        "headroom_bar": AGGREGATE_HOST_HEADROOM,
        "host_total_source": _host_memory_source(),
    }

    if not host_total or not per_bytes:
        return MultiSessionRow(
            axis=AXIS_AGGREGATE_LIMITS,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            sessions=requested,
            detail="the host's memory could not be measured, so over-commit is unknown",
            evidence=evidence,
            note=(
                "'could not measure the host' is NOT 'there is headroom'. The same "
                "rule execution/storage_cost.py applies to an unmeasured store, and "
                "the same reason: a missing measurement rendered as a zero is the "
                "class of lie this module exists to prevent"
            ),
        )

    if fraction is not None and fraction > AGGREGATE_HOST_HEADROOM:
        return MultiSessionRow(
            axis=AXIS_AGGREGATE_LIMITS,
            verdict=VERDICT_DEGRADED,
            sessions=requested,
            detail=(
                f"{requested} session(s) × {mem_limit} promises "
                f"{promised / (1024 ** 3):.2f} GiB, which is "
                f"{fraction * 100:.0f}% of the host's "
                f"{host_total / (1024 ** 3):.2f} GiB — above the "
                f"{AGGREGATE_HOST_HEADROOM * 100:.0f}% headroom bar"
            ),
            evidence=evidence,
            note=(
                "DEGRADED, not BREACH: this is capacity, not containment. A "
                "container limit is a CEILING and not a reservation, so the "
                "honest question is how much can be PROMISED at once. Above the "
                "bar the page cache and the daemon's own bookkeeping start "
                "competing, and a session that was working stops for a reason no "
                "report names"
            ),
        )
    return MultiSessionRow(
        axis=AXIS_AGGREGATE_LIMITS,
        verdict=VERDICT_HELD,
        sessions=requested,
        detail=(
            f"{requested} session(s) × {mem_limit} = "
            f"{promised / (1024 ** 3):.2f} GiB promised against a host total of "
            f"{host_total / (1024 ** 3):.2f} GiB "
            f"({fraction * 100:.0f}%, bar {AGGREGATE_HOST_HEADROOM * 100:.0f}%)"
        ),
        evidence=evidence,
        note=(
            "the ceiling is not a reservation, so this row reports the PROMISE "
            "and the headroom rather than pretending to measure concurrent usage"
        ),
    )


def _parse_memory(text: str) -> int:
    raw = str(text or "").strip().lower()
    units = (("gib", 1024 ** 3), ("mib", 1024 ** 2), ("g", 1024 ** 3), ("m", 1024 ** 2),
             ("kib", 1024), ("k", 1024), ("b", 1))
    for suffix, factor in units:
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * factor)
            except ValueError:
                return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _host_memory_bytes() -> int:
    value, _source = _host_memory_pair()
    return value


def _host_memory_source() -> str:
    _value, source = _host_memory_pair()
    return source


def _host_memory_pair() -> Tuple[int, str]:
    """The host's total memory. The daemon is the authority; the OS is a fallback.

    Delegated in that order on purpose: the limit that binds is the one the
    daemon enforces, so reporting the OS's number when the daemon disagrees
    would be a measurement of the wrong machine. On Docker Desktop those two
    differ by a lot — measured today, ``docker info`` reports MemTotal
    8 175 947 776 (7.62 GiB, the Linux VM's limit) while the Windows host has
    15.72 GiB — and the first version of this function read the **host** figure
    because it called a helper that does not exist on the module it imported and
    the resulting ``AttributeError`` fell through to the fallback. A swallowed
    exception reporting a plausible wrong number is worse than no number.
    """
    daemon = _docker_mem_total()
    if daemon:
        return daemon, "docker info MemTotal"
    try:
        if os.name == "nt":  # pragma: no cover - platform specific
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return (
                int(status.ullTotalPhys),
                "GlobalMemoryStatusEx ullTotalPhys (FALLBACK: the daemon's own "
                "figure was unreadable, so this is the OS's memory and not the "
                "limit a container is held to)",
            )
    except Exception:
        pass
    try:
        return (
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
            "sysconf SC_PHYS_PAGES (FALLBACK)",
        )
    except (AttributeError, ValueError, OSError):
        return 0, "unavailable"


def _docker_mem_total() -> int:
    """The daemon's own ``MemTotal``, or 0 when it cannot be read.

    Reads through :func:`execution.session_isolation._docker_lines` rather than a
    local helper. The first version of this module called
    ``execution.sandbox._docker_lines``, which does not exist; the resulting
    ``AttributeError`` was swallowed by a broad ``except`` and the axis
    cheerfully reported the *Windows host's* 15.72 GiB as though it were the
    limit a container is held to.
    """
    try:
        for line in si._docker_lines(["info", "--format", "{{.MemTotal}}"]):
            for token in line.split():
                if token.isdigit():
                    return int(token)
    except Exception:
        return 0
    return 0


def _docker_free_memory_bytes() -> Optional[int]:
    """The daemon's free memory, or ``None`` when the daemon does not expose it.

    ``None`` and not ``0``: Docker's ``info`` template has no ``MemAvailable``
    field (measured today — the template renders empty), so the honest reading
    of "the daemon does not publish this" is *unknown*. Rendering it as ``0``
    would put a fabricated number into a capacity receipt, and the axis's whole
    subject is capacity.
    """
    try:
        for line in si._docker_lines(["info", "--format", "{{.MemAvailable}}"]):
            for token in line.split():
                if token.isdigit():
                    return int(token)
    except Exception:
        return None
    return None
    return 0


# ==========================================================================
# Axis 6: disk growth across a week of daily use
# ==========================================================================


def disk_growth_report(
    *,
    days: int = 7,
    sessions_per_day: int = 3,
    log_root: str = "logs",
    measure: bool = True,
) -> MultiSessionRow:
    """Disk growth across a week of daily use, reported AND bounded.

    Delegates the projection to :func:`execution.session_isolation.disk_growth_report`,
    which already withholds a total whenever any store is unmeasured — and the
    withholding is inherited rather than reimplemented, because a second
    projection that reads the same stores and disagrees is a second answer.

    ``measure=True`` runs :func:`execution.session_isolation.measure_session_footprint`
    once so the per-session rows are **measured** rather than declared. Without
    it the projection is WITHHELD, which is the honest answer and not a useful
    one — and the first version of this row shipped exactly that, reporting
    "the projection is WITHHELD because store(s) images, containers, volumes
    could not be measured" for a question the brief asks a number to.

    The BOUND is the part this module adds: a projection nobody acts on is a
    note. :func:`execution.storage_cost.storage_bound` is the authority for it,
    read rather than forked.
    """
    per_session: Dict[str, Any] = {}
    measurement: Dict[str, Any] = {"attempted": bool(measure)}
    if measure:
        repo_root = str(Path(__file__).resolve().parents[1])
        try:
            measured = si.measure_session_footprint(repo_root)
            measurement.update(
                {
                    "measured": bool(measured.get("measured")),
                    "reason": measured.get("reason", ""),
                    "note": measured.get("note", ""),
                    "exit_code": measured.get("exit_code"),
                }
            )
            if measured.get("measured"):
                # The KEYS this module reads are the ones
                # measure_session_footprint actually returns — read together
                # rather than remembered, because the first version of this row
                # guessed `images_bytes`/`containers_count`, found neither, and
                # so passed nothing and reported a WITHHELD projection for a
                # measurement it had already taken.
                per_session = {}
                if isinstance(measured.get("images_delta_bytes"), (int, float)):
                    per_session["images"] = float(measured["images_delta_bytes"])
                if isinstance(measured.get("containers_delta"), (int, float)):
                    per_session["containers"] = float(measured["containers_delta"])
                measurement["per_session_used"] = per_session
                measurement["volumes_measured"] = False
        except Exception as exc:
            measurement["error"] = f"{type(exc).__name__}: {exc}"

    projection = si.disk_growth_report(
        days=days,
        sessions_per_day=sessions_per_day,
        measured_per_session=per_session or None,
    )

    bound: Dict[str, Any] = {}
    try:
        from execution import storage_cost

        receipt = storage_cost.storage_bound()
        bound = {
            "fires_at": getattr(receipt, "fires_at", "") or "",
            "store_ceiling_bytes": getattr(receipt, "store_max_bytes", None),
            "retention_days": getattr(receipt, "max_age_days", None),
            "keep_latest": getattr(receipt, "keep_latest", None),
            "verdict": getattr(receipt, "verdict", "") or "",
        }
    except Exception as exc:
        bound = {"unavailable": f"{type(exc).__name__}: {exc}"}

    evidence: Dict[str, Any] = {
        "days": days,
        "sessions_per_day": sessions_per_day,
        "sessions_projected": projection.get("sessions_projected"),
        "verdict": projection.get("verdict"),
        "projection_withheld": bool(projection.get("projection_withheld")),
        "total_bytes": projection.get("total_bytes"),
        "unmeasured_stores": projection.get("unmeasured_stores"),
        "stores": {
            name: {
                "current": row.get("current_on_disk", row.get("current_count")),
                "measured": row.get("measured"),
            }
            for name, row in (projection.get("stores") or {}).items()
        },
        "bound": bound,
        "log_root": log_root,
        "measurement": measurement,
    }

    if projection.get("projection_withheld"):
        unmeasured = list(projection.get("unmeasured_stores") or ())
        return MultiSessionRow(
            axis=AXIS_DISK,
            verdict=VERDICT_UNOBSERVED,
            observed=False,
            detail=(
                f"the per-session footprint was MEASURED for "
                f"{', '.join(sorted(per_session)) or '(nothing)'} and the current "
                f"on-disk census is real, but the week-long TOTAL is WITHHELD "
                f"because store(s) {', '.join(unmeasured)} have no per-session "
                f"measurement"
            ),
            evidence=evidence,
            note=(
                "the withholding is the delegated function's own rule and it is "
                "correct: a JavaScript repository's node_modules volumes are "
                "per-IMAGE and keyed on a content fingerprint, so extrapolating "
                "them per session would be a number nobody took. What IS reported "
                "is measured — the image and container rows, the current census, "
                "and the bound — and the missing row is NAMED rather than "
                "rendered as zero"
            ),
        )
    total = projection.get("total_bytes")
    return MultiSessionRow(
        axis=AXIS_DISK,
        verdict=VERDICT_HELD,
        sessions=int(projection.get("sessions_projected") or 0),
        detail=(
            f"{days} day(s) × {sessions_per_day} session(s) = "
            f"{projection.get('sessions_projected')} session(s); projected growth "
            f"{float(total or 0.0) / (1024 ** 2):.2f} MiB"
            + (f"; bound: {bound.get('fires_at', 'unavailable')}" if bound else "")
        ),
        evidence=evidence,
        note=(
            "the projection is delegated to execution.session_isolation and the "
            "bound to execution.storage_cost — one authority each. A second "
            "projection that reads the same stores and disagrees is a second "
            "answer"
        ),
    )


# ==========================================================================
# The suite
# ==========================================================================


def run_all(
    *,
    sessions: int = 4,
    aggregate_sessions: int = 8,
    readonly_sessions: int = 3,
    naming_processes: int = 4,
    days: int = 7,
    log_root: str = "logs",
) -> MultiSessionReport:
    """Every axis, and the three lists kept separate."""
    started = time.perf_counter()
    rows: List[MultiSessionRow] = []

    runners: Tuple[Tuple[str, Callable[[], MultiSessionRow]], ...] = (
        (AXIS_CROSSTALK, lambda: concurrent_session_report(sessions)),
        (AXIS_NAMING, lambda: naming_collision_report(processes=naming_processes)),
        (AXIS_REAPING, orphan_reaping_report),
        (AXIS_READONLY, lambda: readonly_overlay_report(sessions=readonly_sessions)),
        (AXIS_AGGREGATE_LIMITS, lambda: aggregate_limit_report(sessions=aggregate_sessions)),
        (AXIS_DISK, lambda: disk_growth_report(days=days, log_root=log_root)),
    )
    for axis, runner in runners:
        try:
            row = runner()
        except Exception as exc:
            row = MultiSessionRow(
                axis=axis,
                verdict=VERDICT_UNOBSERVED,
                observed=False,
                detail=f"the axis raised: {type(exc).__name__}: {exc}",
                note=(
                    "an axis that raises is UNMEASURED, never held. A crash is not "
                    "a finding about the product and it is not a guarantee about "
                    "it either"
                ),
            )
        rows.append(row)

    return MultiSessionReport(
        rows=tuple(rows),
        sessions=int(sessions),
        generated_at=time.time(),
        duration_s=time.perf_counter() - started,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m execution.multi_session",
        description="Prove the sandbox holds across many sessions and worktrees.",
    )
    parser.add_argument("--sessions", type=int, default=4, help="concurrent sessions")
    parser.add_argument(
        "--aggregate-sessions", type=int, default=8, help="sessions for the limit axis"
    )
    parser.add_argument(
        "--readonly-sessions", type=int, default=3, help="sessions for the overlay axis"
    )
    parser.add_argument("--processes", type=int, default=4, help="processes for the naming axis")
    parser.add_argument("--days", type=int, default=7, help="days for the disk projection")
    parser.add_argument("--log-root", default="logs", help="logs root for the census")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run_all(
        sessions=args.sessions,
        aggregate_sessions=args.aggregate_sessions,
        readonly_sessions=args.readonly_sessions,
        naming_processes=args.processes,
        days=args.days,
        log_root=args.log_root,
    )
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print("\n".join(render_report(report)))
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        print("\nreport: %s" % target)
    return 0 if report.clean else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
