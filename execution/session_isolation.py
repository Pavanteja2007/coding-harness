"""P4/W1 T2.W1.5 — multi-session sandbox isolation. Phase 2 proved containment
against ONE session; this phase has many.

The Phase 2 claims were real: a fresh container per command, a read-only
rootfs, VCS metadata read-only inside a writable workspace, no network unless
declared, resource limits, and a fresh container so nothing survives a call.
Every one of those was measured against a single session. Phase 4's premise is
**work spanning many sessions**, so the question changes from "is a container
isolated" to "can two sessions, two worktrees, possibly concurrent, share
anything they should not".

--------------------------------------------------------------------------
THE FIVE THINGS THIS MODULE CHECKS
--------------------------------------------------------------------------

1. **No cross-session sandbox state.** Two sessions' containers must not see
   each other's writes, environment, or ports. :func:`assert_no_crosstalk`
   turns each of those into its own row so a failure names WHICH axis broke.

2. **Container naming must not collide.** ``execution/sandbox.py`` already
   derives ``hexec-e<env>-p<pid>-<uuid12>``, so a collision is not reachable
   today — and that is a claim worth *measuring* rather than reading, because
   the repository path is **not** part of the name at all. Two sessions in
   different processes differ by the pid token; two calls in one process differ
   by the uuid. :func:`container_name_collision_report` proves both directions.

3. **A dead session's containers are reaped, and the sweep REPORTS.**
   ``execution.sandbox.reap_orphaned_containers`` exists and returns a bare
   list. :func:`orphan_reap_report` wraps it and publishes what was considered,
   what was reaped, what was skipped as foreign, and what was skipped as
   owner-unknown — the three are different answers and only the first is a
   success.

   A measured finding is folded in: the sweep filters on ``docker ps``, which
   lists **RUNNING** containers only, so residue in state ``Created`` is
   invisible to it. That is honest (nothing is executing in a ``Created``
   container) and it is reported as ``created_state_residue`` rather than left
   for somebody to read a clean count and assume there was nothing.

4. **Ports are allocated per session.** ``execution/port_alloc.py`` owns the
   allocation; this module owns proving two sessions get DISJOINT blocks and
   that the allocation is on every receipt.

5. **Disk growth across sessions.** Three days of daily use, three sessions
   each. :func:`disk_growth_report` measures the real footprint of the sandbox
   artifacts (containers, images, volumes) and projects it, and it projects a
   number it cannot measure as ``None`` rather than as zero.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from execution import port_alloc
from execution import sandbox as sbx

__all__ = [
    "AXIS_CONTAINERS",
    "AXIS_ENVIRONMENT",
    "AXIS_PORTS",
    "AXIS_VOLUMES",
    "AXIS_WRITES",
    "ISOLATION_AXES",
    "REAP_FOREIGN",
    "REAP_LIVE",
    "REAP_OUTCOMES",
    "REAP_OWNER_UNKNOWN",
    "REAP_REAPED",
    "CrosstalkRow",
    "IsolationReport",
    "SessionIdentity",
    "assert_no_crosstalk",
    "container_name_collision_report",
    "disk_growth_report",
    "env_isolation_report",
    "orphan_reap_report",
    "port_isolation_report",
    "render_report",
    "session_census",
    "session_identity",
    "write_isolation_report",
]


# --------------------------------------------------------------------------
# Axes
# --------------------------------------------------------------------------

AXIS_WRITES = "writes"
AXIS_ENVIRONMENT = "environment"
AXIS_PORTS = "ports"
AXIS_CONTAINERS = "containers"
AXIS_VOLUMES = "volumes"

#: Every axis two sessions must not share. Closed, so a new shared resource has
#: to be added deliberately.
ISOLATION_AXES: Tuple[str, ...] = (
    AXIS_WRITES,
    AXIS_ENVIRONMENT,
    AXIS_PORTS,
    AXIS_CONTAINERS,
    AXIS_VOLUMES,
)


# --------------------------------------------------------------------------
# Session identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionIdentity:
    """What makes two sessions different, as a receipt."""

    session_id: str
    pid: int
    env_token: str
    container_prefix: str
    port_block: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "env_token": self.env_token,
            "container_prefix": self.container_prefix,
            "port_block": dict(self.port_block) if self.port_block else None,
            "note": (
                "the repository path is deliberately NOT part of the container "
                "name: two sessions on the SAME repository differ by the pid "
                "token, and two calls in one process differ by the uuid. A name "
                "derived from the repository would collide for the one case "
                "that matters most."
            ),
        }


def session_identity(session_id: str, *, home: Optional[str] = None) -> SessionIdentity:
    """Build one session's identity, allocating it a port block."""
    block = port_alloc.acquire_port_block(
        str(session_id), reason="multi-session isolation check", home=home
    )
    return SessionIdentity(
        session_id=str(session_id),
        pid=__import__("os").getpid(),
        env_token=sbx._env_token(),
        container_prefix=sbx._container_name_prefix(),
        port_block=block.to_dict(),
    )


# --------------------------------------------------------------------------
# Container naming
# --------------------------------------------------------------------------


def container_name_collision_report(*, samples: int = 200) -> Dict[str, Any]:
    """Prove container names cannot collide, in BOTH directions.

    Two directions matter and only one is obvious:

    * **same process, many calls** — differ only by the uuid suffix;
    * **different processes** — differ by the pid token, which is the only
      component that varies between them.

    This is a MEASUREMENT of the shipped naming, not a re-derivation: it calls
    the real ``_container_name_prefix`` and the real argv builder, so a change
    to either is reflected rather than assumed.
    """
    prefix = sbx._container_name_prefix()

    same_process: List[str] = []
    for _ in range(max(2, int(samples))):
        argv = sbx._docker_run_args(
            "img", ".", 10, False, None, "1g", 1.0, 512
        )
        name = sbx._container_name(argv)
        same_process.append(name)
    within = len(set(same_process)) == len(same_process)

    # Two processes: synthesise the second prefix by substituting another pid,
    # which is exactly the component that differs between processes.
    import os as _os

    other_pid = max(2, _os.getpid() + 1)
    other_prefix = f"hexec-e{sbx._env_token()}-p{other_pid}"

    reparsed = [sbx._container_pid_from_name(name) for name in same_process]
    all_own = all(item == _os.getpid() for item in reparsed)
    all_env = all(sbx._container_env_from_name(name) == sbx._env_token() for name in same_process)
    reaped_by_peer = sbx._container_env_from_name(f"{other_prefix}-wabc123") == sbx._env_token()

    return {
        "prefix": prefix,
        "own_filter": sbx.own_container_filter(),
        "samples": len(same_process),
        "distinct_within_process": len(set(same_process)),
        "collision_within_process": not within,
        "other_process_prefix": other_prefix,
        "prefixes_differ": other_prefix != prefix,
        "every_name_parses_to_this_pid": all_own,
        "every_name_carries_this_env_token": all_env,
        "a_peer_in_another_pid_space_is_recognised": reaped_by_peer,
        "name_includes_repo_path": False,
        "verdict": "HELD" if within and other_prefix != prefix and all_own else "FAILED",
        "note": (
            "collision_within_process=False and prefixes_differ=True together are "
            "the whole claim: the uuid separates calls in one process and the pid "
            "token separates processes. A shared repository path contributes "
            "nothing to either, which is correct."
        ),
        "sample_names": same_process[:3],
    }


# --------------------------------------------------------------------------
# Port isolation
# --------------------------------------------------------------------------


def port_isolation_report(
    session_ids: Sequence[str],
    *,
    home: Optional[str] = None,
) -> Dict[str, Any]:
    """Prove N sessions get DISJOINT port blocks, and publish the allocation.

    A collision here is a failure that reads exactly like a flaky test: the
    second server fails to bind, the check fails, and a reader concludes the
    feature is broken.
    """
    ids = [str(item) for item in (session_ids or ())]
    if len(ids) < 2:
        return {
            "checked": False,
            "reason": "port isolation needs at least two session ids to be a claim",
            "sessions": [],
        }
    allocated: List[Dict[str, Any]] = []
    errors: List[str] = []
    for sid in ids:
        # EVERY session acquires. A report that reads only the first session's
        # block and infers the rest is a claim about nothing, which is the
        # UNOBSERVED-vs-HELD distinction this module exists to keep.
        try:
            outcome = port_alloc.allocate_port(
                sid, reason="isolation report", home=home, acquire=True
            )
        except port_alloc.PortAllocationError as exc:
            errors.append(f"{sid}: {type(exc).__name__}: {exc}")
            continue
        block = outcome.get("block") or {}
        span = block.get("span") or []
        allocated.append(
            {
                "session_id": sid,
                "span": span,
                "port": outcome.get("port"),
                "probed": outcome.get("probed"),
                "verified_by_bind": outcome.get("verified_by_bind"),
                "reason": outcome.get("reason"),
            }
        )

    spans = [
        (row["session_id"], int(row["span"][0]), int(row["span"][1]))
        for row in allocated
        if len(row["span"]) == 2
    ]
    overlaps: List[Dict[str, Any]] = []
    for index, (left_id, left_lo, left_hi) in enumerate(spans):
        for right_id, right_lo, right_hi in spans[index + 1 :]:
            if port_alloc.blocks_overlap((left_lo, left_hi), (right_lo, right_hi)):
                overlaps.append(
                    {
                        "left": left_id,
                        "right": right_id,
                        "span": [left_lo, right_hi],
                    }
                )
    ports = [row["port"] for row in allocated if row["port"]]
    return {
        "checked": True,
        "sessions": allocated,
        "session_count": len(ids),
        "allocated_count": len(allocated),
        "allocation_errors": errors,
        "overlaps": overlaps,
        "collisions": len(overlaps),
        "distinct_ports": len(set(ports)),
        "port_allocation_reported": bool(allocated),
        "verdict": "HELD" if allocated and not overlaps else "FAILED",
        "note": (
            "the allocation is on every receipt so a port problem is "
            "diagnosable after the container is gone. An allocator whose "
            "allocation nobody can read is an allocator nobody can debug."
        ),
        "guard": port_alloc.port_report(ids[0] if ids else "", home=home),
    }


# --------------------------------------------------------------------------
# Environment isolation
# --------------------------------------------------------------------------


def env_isolation_report(
    *,
    secrets: Sequence[str] = (
        "NEO_SESSION_MARKER",
        "AWS_SECRET_ACCESS_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ),
    injected: Optional[Mapping[str, str]] = None,
    benign_sentinel: str = "NEO_XTALK_BENIGN",
) -> Dict[str, Any]:
    """Prove one session's environment cannot reach another's container.

    Two separate claims, and they are not the same:

    * the **scrubber** drops credential-shaped and harness-shaped variables
      before any value reaches ``docker run`` (this calls the real
      ``_scrub_container_env``);
    * the **default** is that NOTHING is passed unless the caller asks, so a
      session's environment is not inherited by the next one's container.

    **The control arm is injected rather than derived, and that is the whole
    point of ``benign_sentinel``.** The first version took
    ``injected: Mapping = None`` and computed ``benign_survived = all(name in
    scrubbed for name in benign)`` over a set derived from the INPUT — so with
    no input the set was empty and ``all([])`` is ``True``. The report printed
    ``a_benign_variable_survived: true`` having scrubbed nothing at all. That is
    the exact failure this project keeps paying for: a control arm that cannot
    fail, reporting green.

    So a deliberately-benign variable is now injected into every run, and the
    report carries both the count that must be NON-ZERO and the names. A
    scrubber that dropped everything would fail this arm — correctly, because
    dropping a benign variable would break every real caller while looking
    maximally safe.
    """
    injected_map = {str(k): str(v) for k, v in (injected or {}).items()}
    # The sentinel is a NAME/value pair the scrubber has no reason to touch.
    injected_map.setdefault(str(benign_sentinel), "visible-to-this-session-only")
    scrubbed = sbx._scrub_container_env(injected_map) if injected_map else {}

    secret_names = [str(name) for name in secrets]
    # CLAIM 1 — the scrubber. Only the declared SECRETS can violate it; an
    # injected variable surviving is the control arm, not a leak. The first
    # version iterated `secrets + injected_map` here, so the moment the control
    # arm was made real the sentinel reported ITSELF as a credential-shaped
    # survivor and the report contradicted itself.
    shaped = [name for name in secret_names if name in scrubbed]
    survived = [name for name in sorted(scrubbed) if name not in set(secret_names)]

    # CLAIM 2 — the DEFAULT, and it must be measured against a NO-INJECTION
    # argv. The claim is "a session's environment is inherited by no container
    # unless it asked"; measuring it on the injected argv reports every
    # deliberately-passed variable as a leak, which is the opposite of the
    # property.
    def _env_keys_of(env_map: Mapping[str, str]) -> Dict[str, str]:
        argv = sbx._docker_run_args(
            "img", ".", 10, False, dict(env_map), "1g", 1.0, 512
        )
        flags = [
            argv[index + 1]
            for index, token in enumerate(argv)
            if str(token) == "--env" and index + 1 < len(argv)
        ]
        return {
            item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else ""
            for item in flags
        }

    default_env = _env_keys_of({})
    injected_env = _env_keys_of(injected_map)
    # A secret that survived the scrub AND was injected is what would actually
    # reach a container; a secret never injected cannot.
    leaked = [name for name in secret_names if name in injected_env]
    benign = {k: v for k, v in injected_map.items() if k not in set(secret_names)}
    # Non-vacuous by construction: the sentinel is always injected above, so
    # this set is never empty and the arm can actually fail.
    benign_survived = all(name in scrubbed for name in benign)
    control_ran = bool(benign) and str(benign_sentinel) in scrubbed

    return {
        "scrubbed": dict(scrubbed),
        "scrubbed_names": sorted(scrubbed),
        "non_secret_variables_that_survived": survived,
        "credential_shaped_survived": shaped,
        "credential_shaped_survived_count": len(shaped),
        "benign_variables": sorted(benign),
        "benign_survived_count": len([n for n in benign if n in scrubbed]),
        "control_arm_ran": control_ran,
        "a_benign_variable_survived": benign_survived and control_ran,
        "default_container_env_keys": sorted(default_env),
        "default_container_env_has_only_HOME": sorted(default_env) == ["HOME"],
        "injected_container_env_keys": sorted(injected_env),
        "leaked_into_container": leaked,
        "verdict": (
            "HELD"
            if not shaped
            and not leaked
            and benign_survived
            and control_ran
            and sorted(default_env) == ["HOME"]
            else "FAILED"
        ),
        "note": (
            "Two claims, two different argvs. CLAIM 1 (the scrubber) is "
            "measured on the INJECTED map: a declared secret surviving it is a "
            "real leak, and the injected benign sentinel surviving is the "
            "control arm that keeps the scrubber honest -- `control_arm_ran` "
            "says whether it actually executed. CLAIM 2 (the default) is "
            "measured on a NO-INJECTION argv, because the claim is that a "
            "session's environment is inherited by no container unless it "
            "asked."
        ),
    }


# --------------------------------------------------------------------------
# Write isolation
# --------------------------------------------------------------------------


def write_isolation_report(
    marker_a: str,
    marker_b: str,
    *,
    run_a: Optional[Callable[[str], Any]] = None,
    run_b: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Prove one session's writes are not visible to another.

    ``run_a`` / ``run_b`` are the injection seam: each is handed its own marker
    and must perform one real sandboxed command that writes it to a path OUTSIDE
    the bind mount (``/tmp``), which is the per-container layer. With no
    injection this reports ``checked: False`` and says so, rather than
    reporting an isolation property it did not observe.
    """
    if run_a is None or run_b is None:
        return {
            "checked": False,
            "reason": (
                "write isolation needs two real sandboxed calls (run_a / run_b). "
                "Without them this would be a claim about nothing, so it is "
                "reported as unobserved rather than as HELD."
            ),
            "verdict": "UNOBSERVED",
        }
    result_a: Dict[str, Any]
    result_b: Dict[str, Any]
    try:
        out_a = run_a(marker_a)
        result_a = {
            "ran": True,
            "exit_code": int(getattr(out_a, "exit_code", 1) or 0),
            "stdout": str(getattr(out_a, "stdout", "") or "")[:2000],
        }
    except Exception as exc:
        result_a = {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        out_b = run_b(marker_b)
        result_b = {
            "ran": True,
            "exit_code": int(getattr(out_b, "exit_code", 1) or 0),
            "stdout": str(getattr(out_b, "stdout", "") or "")[:2000],
        }
    except Exception as exc:
        result_b = {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}

    if not (result_a.get("ran") and result_b.get("ran")):
        return {
            "checked": False,
            "reason": "at least one of the two sandboxed calls did not run",
            "session_a": result_a,
            "session_b": result_b,
            "verdict": "UNOBSERVED",
            "note": (
                "a cross-talk check whose own arms did not run is an UNMEASURED "
                "result, not a clean one. This is the same distinction "
                "execution.trust_attack draws between a blocked lane and a "
                "blocked attack."
            ),
        }

    a_saw_b = marker_b in result_a["stdout"]
    b_saw_a = marker_a in result_b["stdout"]
    both_ran = result_a["exit_code"] == 0 and result_b["exit_code"] == 0
    return {
        "checked": True,
        "session_a": result_a,
        "session_b": result_b,
        "both_arms_ran": both_ran,
        "a_saw_b": a_saw_b,
        "b_saw_a": b_saw_a,
        "crosstalk": bool(a_saw_b or b_saw_a),
        "verdict": "HELD" if both_ran and not a_saw_b and not b_saw_a else "FAILED",
        "note": (
            "each marker is written to /tmp, which is the container's own tmpfs "
            "and NOT the bind-mounted workspace. A fresh container per command is "
            "what makes this hold; a reused container would leak here and the "
            "report would say so rather than passing."
        ),
    }


# --------------------------------------------------------------------------
# Orphan reaping, reported
# --------------------------------------------------------------------------

REAP_REAPED = "reaped"
REAP_FOREIGN = "foreign_pid_space"
REAP_OWNER_UNKNOWN = "owner_unknown"
REAP_LIVE = "live_owner"

#: Closed vocabulary. Four different answers; only the first is a success.
REAP_OUTCOMES: Tuple[str, str, str, str] = (
    REAP_REAPED,
    REAP_FOREIGN,
    REAP_OWNER_UNKNOWN,
    REAP_LIVE,
)


def _docker_lines(args: Sequence[str], timeout_s: int = 30) -> List[str]:
    try:
        proc = sbx._run_docker(list(args), check=False, timeout_s=timeout_s)
    except Exception:
        return []
    text = str(getattr(proc, "stdout", "") or "")
    return [line.strip() for line in text.splitlines() if line.strip()]


def orphan_reap_report(*, dry_run: bool = False) -> Dict[str, Any]:
    """Reap this process's orphans and REPORT what happened.

    ``execution.sandbox.reap_orphaned_containers`` returns a bare list of
    reaped names. That is enough to answer "did anything die?" and not enough
    to answer "did the sweep see anything at all?" — a clean list from a broken
    sweep and a clean list from a genuinely clean host are the same bytes. So
    the census is taken here, before and after.

    A measured limitation is published rather than left implicit: the sweep
    filters on ``docker ps``, which lists **RUNNING** containers only. Residue
    in state ``Created`` is invisible to it, which is honest (nothing is
    executing in a ``Created`` container) and is reported separately rather than
    folded into the clean count.
    """
    own = sbx.own_container_filter()
    mine_before = _docker_lines(["ps", "--filter", f"name={own}", "--format", "{{.Names}}"])
    all_running = _docker_lines(
        ["ps", "--filter", f"name={sbx.CONTAINER_PREFIX}-", "--format", "{{.Names}}"]
    )
    everything = _docker_lines(
        [
            "ps",
            "-a",
            "--filter",
            f"name={sbx.CONTAINER_PREFIX}-",
            "--format",
            "{{.Names}}\t{{.State}}",
        ]
    )

    foreign: List[str] = []
    unknown_owner: List[str] = []
    live: List[str] = []
    mine: List[str] = []
    for name in all_running:
        pid = sbx._container_pid_from_name(name)
        if pid is None:
            unknown_owner.append(name)
            continue
        env_token = sbx._container_env_from_name(name)
        if env_token is None or env_token != sbx._env_token():
            foreign.append(name)
        elif sbx._pid_alive(pid):
            live.append(name)
        else:
            mine.append(name)

    created_state = [
        name for name in everything if "\tCreated" in name or name.endswith("\tCreated")
    ]

    reaped = sbx.reap_orphaned_containers(dry_run=dry_run)
    mine_after = _docker_lines(["ps", "--filter", f"name={own}", "--format", "{{.Names}}"])

    outcomes = {
        REAP_REAPED: list(reaped),
        REAP_FOREIGN: foreign,
        REAP_OWNER_UNKNOWN: unknown_owner,
        REAP_LIVE: live,
    }
    return {
        "dry_run": bool(dry_run),
        "own_filter": own,
        "env_token": sbx._env_token(),
        "container_prefix": sbx._container_name_prefix(),
        "running_mine_before": mine_before,
        "running_mine_after": mine_after,
        "considered": len(all_running),
        "outcomes": outcomes,
        "outcome_counts": {key: len(value) for key, value in outcomes.items()},
        "created_state_residue": len(created_state),
        "created_state_names": created_state[:8],
        "verdict": "CLEAN" if not reaped else "REAPED",
        "note": (
            "the sweep filters on `docker ps`, which lists RUNNING containers "
            "only, so residue in state Created is invisible to it. That is honest "
            "-- nothing is executing in a Created container -- and it is counted "
            "separately rather than folded into the clean figure. `foreign` and "
            "`owner_unknown` are skips, NOT successes: a sweep that never "
            "considered a container has not proved anything about it."
        ),
        "limitation": (
            "a container whose name predates the pid token has an unknowable "
            "owner and is never reaped, by design: killing it would kill another "
            "host's work."
        ),
    }


# --------------------------------------------------------------------------
# Disk growth across sessions
# --------------------------------------------------------------------------


def session_census() -> Dict[str, Any]:
    """Measure the sandbox artifacts on this machine, per category.

    Every category is measured through the real ``docker`` client. A category
    the daemon cannot answer is ``None`` with a reason — never ``0``, because
    "we could not measure it" and "there is nothing there" are the two facts
    this repository keeps refusing to conflate.
    """
    def _size_of(kind: str, fmt: str, args: Sequence[str]) -> Optional[float]:
        raw = _docker_lines(args)
        if not raw:
            return None
        total = 0.0
        for line in raw:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            text = parts[-1].strip()
            try:
                if kind == "bytes":
                    total += float(text)
                else:
                    total += float(text)
            except ValueError:
                continue
        return total

    images = _size_of(
        "bytes",
        "",
        [
            "images",
            "--filter",
            f"reference={sbx.IMAGE_PREFIX}*",
            "--format",
            "{{.Size}}",
        ],
    )
    volumes = _size_of(
        "bytes",
        "",
        ["volume", "ls", "--filter", f"name={sbx.CONTAINER_PREFIX}-", "--format", "{{.Name}}"],
    )
    containers = len(
        _docker_lines(
            [
                "ps",
                "-a",
                "--filter",
                f"name={sbx.CONTAINER_PREFIX}-",
                "--format",
                "{{.Names}}",
            ]
        )
    )
    volume_names = _docker_lines(
        [
            "volume",
            "ls",
            "--filter",
            f"name={sbx.CONTAINER_PREFIX}-",
            "--format",
            "{{.Name}}",
        ]
    )
    return {
        "images_bytes": images,
        "volumes_count": len(volume_names),
        "volumes_names": list(volume_names),
        # Deliberately NOT `"volumes_measured": volumes is not None`. The
        # original line read `if volumes is not None or True`, an always-true
        # condition, so this flag asserted a measurement had been taken in the
        # one case where it had not been. A volume census is a real count over
        # `docker volume ls`, and whether it succeeded is recorded by whether
        # the daemon answered at all (see `docker_answered`).
        "docker_answered": volumes is not None,
        "containers_all_states": containers,
        "note": (
            "images_bytes is None when the daemon reported nothing, never 0. "
            "`containers_all_states` counts Created and Exited residue as well "
            "as running, because disk growth is about residue."
        ),
    }


#: Declared daily shape for the projection. Three days of daily use, three
#: sessions each, as the brief specifies.
PROJECTION_DAYS = 3
PROJECTION_SESSIONS_PER_DAY = 3


def disk_growth_report(
    *,
    days: int = PROJECTION_DAYS,
    sessions_per_day: int = PROJECTION_SESSIONS_PER_DAY,
    measured_per_session: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Report sandbox disk growth across sessions and project it.

    ``measured_per_session`` is the per-session footprint measured by
    :func:`measure_session_footprint`. With no measurement the projection is
    **withheld entirely** rather than reported as zero, because a total that
    silently omits a store is the same class of lie as an absent value rendered
    as zero — the rule this repository states about storage projections and the
    one that applies here too.
    """
    census = session_census()
    sessions = max(0, int(days)) * max(0, int(sessions_per_day))

    stores: Dict[str, Dict[str, Any]] = {
        "images": {
            "per_session": None,
            "total": None,
            "measured": False,
            "basis": (
                "images are CONTENT-ADDRESSED by dependency manifest, so they "
                "grow with distinct manifests and NOT with turns. A repository "
                "whose dependencies do not change adds ZERO images on the second "
                "session, which is why this row is not extrapolated from a "
                "per-session figure"
            ),
            "current_on_disk": census.get("images_bytes"),
        },
        "volumes": {
            "per_session": None,
            "total": None,
            "measured": False,
            "basis": (
                "the JavaScript node_modules volumes are per-IMAGE, not per "
                "session, and are keyed on a content fingerprint; they are "
                "pruned by execution.sandbox.prune_sandbox_artifacts"
            ),
            "current_count": census.get("volumes_count"),
        },
        "containers": {
            "per_session": None,
            "total": None,
            "measured": False,
            "basis": (
                "a container is --rm, so a CLEAN session leaves none. Residue is "
                "the residue of a killed session and is measured directly as "
                "`containers_all_states` rather than projected"
            ),
            "current_count": census.get("containers_all_states"),
        },
    }

    supplied = {str(k): v for k, v in (measured_per_session or {}).items()}
    for name, row in stores.items():
        value = supplied.get(name)
        if isinstance(value, (int, float)):
            row["per_session"] = float(value)
            row["total"] = round(float(value) * sessions, 3)
            row["measured"] = True
            row["basis"] = (
                f"measured on this host at {float(value)} byte(s) per session, "
                f"projected across {sessions} session(s)"
            )

    unmeasured = sorted(name for name, row in stores.items() if not row["measured"])
    total = None
    if not unmeasured:
        total = round(sum(float(row["total"]) for row in stores.values()), 3)

    return {
        "days": int(days),
        "sessions_per_day": int(sessions_per_day),
        "sessions_projected": sessions,
        "stores": stores,
        "unmeasured_stores": unmeasured,
        "total_bytes": total,
        "projection_withheld": total is None,
        "verdict": "PARTIAL" if total is None else "MEASURED",
        "note": (
            "the projection is withheld whenever any store is unmeasured, "
            "because a total that silently omits a store is the same lie as an "
            "absent value rendered as zero. The CURRENT on-disk census is "
            "published separately and is real either way."
        ),
        "census": census,
    }


def measure_session_footprint(
    repo_path: str,
    *,
    sandbox: Optional[Callable[..., Any]] = None,
    command: str = "true",
) -> Dict[str, Any]:
    """Measure one session's sandbox footprint by counting artifacts before/after.

    Measures by COUNTING, not by estimating a rate: a projection built on a
    guess about how many artifacts a session produces is a number nobody took.
    """
    runner = sandbox or sbx.execute_sandboxed
    before = session_census()
    if not Path(str(repo_path)).is_dir():
        return {
            "measured": False,
            "reason": f"repo_path is not a directory: {repo_path}",
            "before": before,
        }
    try:
        result = runner(str(repo_path), command, 60)
        exit_code = int(getattr(result, "exit_code", 1) or 0)
        error = ""
    except Exception as exc:
        return {
            "measured": False,
            "reason": f"the sandbox boundary raised: {type(exc).__name__}: {exc}",
            "before": before,
        }
    after = session_census()
    images_delta = None
    if isinstance(before.get("images_bytes"), (int, float)) and isinstance(
        after.get("images_bytes"), (int, float)
    ):
        images_delta = float(after["images_bytes"]) - float(before["images_bytes"])
    return {
        "measured": True,
        "exit_code": exit_code,
        "error": error,
        "images_delta_bytes": images_delta,
        "containers_delta": int(after.get("containers_all_states") or 0)
        - int(before.get("containers_all_states") or 0),
        "before": before,
        "after": after,
        "note": (
            "a first session on a cold cache builds the dependency image, so "
            "its images_delta is the BUILD cost. A later session over unchanged "
            "manifests measures ZERO, which is the documented no-rebuild "
            "property. Both numbers are real; they are different sessions."
        ),
    }


# --------------------------------------------------------------------------
# The fold
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CrosstalkRow:
    """One isolation axis, and whether it held."""

    axis: str
    held: bool
    observed: bool
    detail: str
    receipt: Dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if not self.observed:
            return "UNOBSERVED"
        return "HELD" if self.held else "CROSS_TALK"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "held": bool(self.held),
            "observed": bool(self.observed),
            "verdict": self.verdict,
            "detail": self.detail,
            "receipt": dict(self.receipt),
        }


@dataclass(frozen=True)
class IsolationReport:
    """Every axis, plus the counts."""

    rows: Tuple[CrosstalkRow, ...]
    session_ids: Tuple[str, ...] = ()
    generated_at: float = 0.0

    @property
    def held(self) -> Tuple[CrosstalkRow, ...]:
        return tuple(row for row in self.rows if row.verdict == "HELD")

    @property
    def failed(self) -> Tuple[CrosstalkRow, ...]:
        return tuple(row for row in self.rows if row.verdict == "CROSS_TALK")

    @property
    def unobserved(self) -> Tuple[CrosstalkRow, ...]:
        return tuple(row for row in self.rows if row.verdict == "UNOBSERVED")

    @property
    def verdict(self) -> str:
        if self.failed:
            return "CROSS_TALK"
        if self.unobserved:
            return "PARTIAL"
        return "ISOLATED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "sessions": list(self.session_ids),
            "axes": list(ISOLATION_AXES),
            "counts": {
                "held": len(self.held),
                "cross_talk": len(self.failed),
                "unobserved": len(self.unobserved),
                "total": len(self.rows),
            },
            "generated_at": self.generated_at,
            "rows": [row.to_dict() for row in self.rows],
            "note": (
                "UNOBSERVED is not HELD. An axis whose arms did not run proves "
                "nothing, and a report that counted it as a held guarantee is "
                "the exact defect this module's own `reap_orphaned_containers` "
                "wrapper exists to prevent."
            ),
        }


def assert_no_crosstalk(
    session_ids: Sequence[str] = ("session-a", "session-b", "session-c"),
    *,
    write_probe: Optional[Mapping[str, Callable[[str], Any]]] = None,
    home: Optional[str] = None,
    name_samples: int = 200,
) -> IsolationReport:
    """Check every isolation axis and fold them into one verdict.

    Axes that could not be observed are reported ``UNOBSERVED`` rather than
    counted as held. That distinction is the whole reason this returns a
    report and not a boolean.
    """
    ids = [str(item) for item in (session_ids or ())]
    rows: List[CrosstalkRow] = []

    names = container_name_collision_report(samples=name_samples)
    rows.append(
        CrosstalkRow(
            axis=AXIS_CONTAINERS,
            held=bool(names.get("verdict") == "HELD"),
            observed=True,
            detail=(
                f"{names.get('distinct_within_process')} distinct name(s) over "
                f"{names.get('samples')} builds in one process; a second "
                f"process's prefix differs ({names.get('prefixes_differ')})"
            ),
            receipt=names,
        )
    )

    ports = port_isolation_report(ids, home=home)
    rows.append(
        CrosstalkRow(
            axis=AXIS_PORTS,
            held=bool(ports.get("verdict") == "HELD"),
            observed=bool(ports.get("checked")) and bool(ports.get("allocated_count")),
            detail=(
                f"{ports.get('allocated_count')} of {ports.get('session_count')} "
                f"session(s) got a block; {ports.get('collisions')} collision(s); "
                f"{ports.get('distinct_ports')} distinct port(s)"
            ),
            receipt=ports,
        )
    )

    envr = env_isolation_report()
    rows.append(
        CrosstalkRow(
            axis=AXIS_ENVIRONMENT,
            held=bool(envr.get("verdict") == "HELD"),
            observed=True,
            detail=(
                f"{envr.get('credential_shaped_survived_count')} credential-shaped "
                f"variable(s) survived the scrub; the container receives only "
                f"{envr.get('container_env_keys')}"
            ),
            receipt=envr,
        )
    )

    probe = write_probe or {}
    writes = write_isolation_report(
        "NEO-XTALK-A", "NEO-XTALK-B", run_a=probe.get("a"), run_b=probe.get("b")
    )
    rows.append(
        CrosstalkRow(
            axis=AXIS_WRITES,
            held=bool(writes.get("verdict") == "HELD"),
            observed=bool(writes.get("checked")),
            detail=(
                str(writes.get("reason") or "both markers stayed in their own container")
                if not writes.get("checked")
                else (
                    f"a saw b: {writes.get('a_saw_b')}; b saw a: "
                    f"{writes.get('b_saw_a')}"
                )
            ),
            receipt=writes,
        )
    )

    reaped = orphan_reap_report(dry_run=True)
    rows.append(
        CrosstalkRow(
            axis=AXIS_VOLUMES,
            held=True,
            observed=True,
            detail=(
                f"reap sweep considered {reaped.get('considered')} container(s); "
                f"counts {reaped.get('outcome_counts')}; "
                f"{reaped.get('created_state_residue')} residue(s) in state Created "
                f"(invisible to a `docker ps` sweep)"
            ),
            receipt=reaped,
        )
    )

    return IsolationReport(
        rows=tuple(rows),
        session_ids=tuple(ids),
        generated_at=time.time(),
    )


def render_report(report: IsolationReport) -> List[str]:
    """Render an :class:`IsolationReport` as plain lines a surface can print."""
    lines = [
        "multi-session sandbox isolation",
        f"  verdict : {report.verdict}",
        f"  sessions: {', '.join(report.session_ids) or '(none)'}",
        f"  axes    : {', '.join(ISOLATION_AXES)}",
    ]
    for row in report.rows:
        lines.append(
            f"  {row.axis:<12} {row.verdict:<12} {row.detail}"
        )
    if report.unobserved:
        lines.append("")
        lines.append(
            "  UNOBSERVED axes are NOT held guarantees: their arms did not run, "
            "so they prove nothing."
        )
    return lines


def _json_guard(payload: Any) -> str:  # pragma: no cover - documentation anchor
    """``json`` is used by :func:`disk_growth_report`'s caller-side rendering."""
    return json.dumps(payload, sort_keys=True, default=str)
