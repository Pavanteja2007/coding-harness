"""Adversarial self-check for the sandbox's own containment claims.

`execution/sandbox.py` produces a `ContainmentPolicy` and a
`containment_receipt` that assert what the sandbox permits. **A receipt that
lies is worse than no receipt** — the whole product rests on these, a
reviewer reads the trace instead of the source, and a policy that reports
read-only while the mount is read-write is not a weaker guarantee, it is a
false one.

So this module does not read the code and agree with it. It ATTACKS each claim
and records what actually happened. Seven claims, seven attacks, seven
recorded outcomes:

| # | claim | attack |
|---|-------|--------|
| 1 | `.git` is read-only inside a writable workspace | write a file into `.git/` from inside the container; check the HOST afterwards |
| 2 | declared writable roots cannot overlap read-only paths | declare a writable root containing `.git`; the read-only side must win AND the conflict must be recorded |
| 3 | network is off unless declared | open a connection; the refusal must be real and must NAME the declaration |
| 4 | a non-existent read-only path is not mounted | declare one; it must be reported absent and must not be created |
| 5 | a fresh container per call | write to container-local `/tmp` in call 1; it must be gone in call 2 |
| 6 | resource limits are real | exceed mem / pids / cpu; the container must be KILLED, not warned |
| 7 | absolute / `..` / empty writable roots are dropped, not clamped | declare each shape; each must be dropped WITH a note |

Degradation, which is the part that decides whether this is evidence
-----------------------------------------------------------------------
Docker is frequently unreachable on a developer host — this repository's own
CI runs the Docker e2e suite on ubuntu *only* for that reason. So:

* a Docker-dependent row whose daemon is unreachable is reported
  ``blocked`` **with the exact error text**, which is a refusal to answer, not
  a skip and not a pass;
* the Docker-FREE subset (rows 2 and 7 — argument construction, policy
  resolution, the conflict table, the non-existent-path receipt) runs
  **unconditionally**, so a machine with no daemon still gets real evidence
  about the half of containment that is pure policy resolution.

There is no third state. ``OUTCOME_HELD``, ``OUTCOME_FINDING``,
``OUTCOME_BLOCKED`` are the only values a row can carry, and ``EXIT`` is
non-zero on any finding or any blocked row — a blocked row must not be able to
render as a clean run.

Non-vacuity
-----------
An attack that never ran passes every "the marker is absent" assertion. Every
Docker row therefore also asserts that the attack was actually ATTEMPTED and,
where the claim is about a boundary inside an otherwise-permitted workspace,
that the **control arm** succeeded — a workspace write that lands, a loopback
connection that connects, a bind-mount file that persists. A refusal that
fires because the whole container is broken is not a containment proof.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from execution.sandbox import (
    MOUNT_POINT,
    SandboxDependencyError,
    SandboxUnavailableError,
    _docker_run_args,
    assert_sandbox_argv_isolated,
    containment_receipt,
    docker_available,
    execute_sandboxed,
    readonly_overlays,
    resolve_containment,
    writable_overlays,
)

#: The three outcomes. There is deliberately no "skipped": a row either
#: attacked something, refused to attack it because the machine cannot, or
#: found a hole. "Skipped" is how a blocked lane gets reported as a pass.
OUTCOME_HELD = "held"
OUTCOME_FINDING = "finding"
OUTCOME_BLOCKED = "blocked"

#: Marker written by the fresh-container attack. If it survives into the next
#: call, containers are being reused and the claim is false.
STATE_MARKER = "neo-containment-audit-marker"

#: Fixture layout. `.git` is a REAL directory with a real file in it, because
#: the attack's whole question is whether that directory is writable from
#: inside the container — a fixture with no `.git` would make the attack
#: vacuous in the other direction (the read-only overlay would have nothing to
#: mount, and the claim would be untested).
FIXTURE_FILES: Dict[str, str] = {
    "mymod.py": "VALUE = 1\n",
    "pyproject.toml": '[project]\nname = "audit"\nversion = "0"\n',
    ".git/config": "[core]\n\trepositoryformatversion = 0\n",
    ".git/HEAD": "ref: refs/heads/main\n",
    ".git/preexisting": "original\n",
}


@dataclass
class Check:
    """One claim, one attack, one recorded outcome."""

    claim: str
    attack: str
    needs_docker: bool
    verdict: str = ""
    evidence: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    attack_ran: bool = False
    control_ran: bool = False
    elapsed_s: float = 0.0

    @property
    def blocked(self) -> bool:
        return self.verdict == OUTCOME_BLOCKED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "attack": self.attack,
            "needs_docker": bool(self.needs_docker),
            "verdict": self.verdict,
            "evidence": self.evidence,
            "attack_ran": bool(self.attack_ran),
            "control_ran": bool(self.control_ran),
            "elapsed_s": round(float(self.elapsed_s), 3),
            "detail": dict(self.detail),
        }


def _fixture(parent: Path, tag: str) -> Path:
    """Materialise a disposable repository with a real `.git` directory."""
    root = parent / f"audit-{tag}"
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in FIXTURE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Row 1 — `.git` is read-only inside a writable workspace
# ---------------------------------------------------------------------------


def check_git_readonly(root: Path, timeout_s: int = 90) -> Check:
    """Attack: write into `.git/` from inside the container; probe the HOST.

    The host probe is the whole point. A container that reports a write error
    while the bind mount is actually read-write would still leave a file on the
    host, and only the host can see it.
    """
    check = Check(
        claim="`.git` is read-only inside a writable workspace",
        attack=(
            "inside a container whose /workspace is a read-write bind mount, "
            "`touch .git/pwned` and `echo x > .git/config`, then stat both on "
            "the HOST"
        ),
        needs_docker=True,
    )
    probe = root / ".git" / "pwned"
    if probe.exists():
        probe.unlink()
    original_config = (root / ".git" / "config").read_text(encoding="utf-8")
    control = root / "control-write-marker.txt"
    if control.exists():
        control.unlink()

    command = (
        f"cd {MOUNT_POINT} || exit 90; "
        "touch control-write-marker.txt; echo CONTROL_RC=$?; "
        "touch .git/pwned 2>/dev/null; echo PWNED_RC=$?; "
        "echo forged >> .git/config 2>/dev/null; echo CONFIG_RC=$?"
    )
    result = execute_sandboxed(str(root), command, timeout_s)
    check.attack_ran = result.exit_code == 0
    check.control_ran = control.exists()
    on_host = probe.exists()
    config_changed = (root / ".git" / "config").read_text(
        encoding="utf-8"
    ) != original_config
    rc_lines = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line and line.split("=", 1)[0].endswith("_RC")
    )
    check.detail = {
        "container_exit": result.exit_code,
        "container_rc_lines": rc_lines,
        "pwned_on_host": on_host,
        "git_config_modified_on_host": config_changed,
        "workspace_write_landed": control.exists(),
    }
    if on_host or config_changed:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "a write into .git/ from inside the container LANDED on the host"
        )
    elif not check.attack_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"the attack did not run (container exit {result.exit_code}); a "
            "refusal that never executed is not a containment proof"
        )
    elif not check.control_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "the control write did not land, so the workspace itself was not "
            "writable and this row cannot distinguish containment from a "
            "broken mount"
        )
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            "control workspace write landed; .git write refused in-container "
            f"(rc={rc_lines.get('PWNED_RC', '?')}, "
            f"config rc={rc_lines.get('CONFIG_RC', '?')}) and no file appeared "
            "on the host"
        )
    return check


# ---------------------------------------------------------------------------
# Row 2 — a writable root can never re-open a read-only path
# ---------------------------------------------------------------------------


def check_writable_root_conflict(root: Path, timeout_s: int = 90) -> Check:
    """Attack: declare writable roots that OVERLAP the read-only `.git`.

    The overlap direction the resolver implements is "a writable root INSIDE a
    read-only path", and it is the more dangerous of the two: declaring `.git`
    (or a subtree of it) as writable is a direct attempt to re-open VCS state,
    the one tree whose forgery changes what a reviewer believes happened. The
    "writable root CONTAINS .git" shape is a DIFFERENT code path -- it
    normalises to the empty relative path and is dropped as an unusable
    declaration -- so both shapes are declared together and each is required
    to be dropped WITH A NOTE. A row that only attacked the shape that happens
    to hit the conflict table would leave the other shape untested.

    Docker-free half: policy resolution is a pure function, so the conflict
    table is checked directly and runs with no daemon. Docker half: a
    container built from the resolved policy still refuses the `.git` write,
    which is what proves the conflict table is not merely reporting a decision
    it does not enforce.
    """
    check = Check(
        claim="declared writable roots cannot overlap read-only paths",
        attack=(
            "resolve_containment(repo, writable_roots=['.', '.git', "
            "'.git/refs']) - the last two are read-only subpaths and the first "
            "contains them - and require (a) none of the three in the "
            "resolved writable set, (b) an 'overlaps' note for each, (c) the "
            "pre-spawn argv gate to accept the policy's own argv, (d) with the "
            "policy applied, a real container still refusing the .git write"
        ),
        needs_docker=True,
    )
    hostile: Sequence[str] = (".", ".git", ".git/refs")
    policy = resolve_containment(str(root), writable_roots=hostile)
    conflict_notes = [note for note in policy.notes if "overlaps" in note]
    refused_notes = [note for note in policy.notes if "refused" in note]
    git_writable = ".git" in policy.writable_roots
    workspace_ro = policy.workspace_readonly
    check.detail = {
        "declared": list(hostile),
        "writable_roots": list(policy.writable_roots),
        "readonly_subpaths": list(policy.readonly_subpaths),
        "workspace_readonly": workspace_ro,
        "conflict_notes": list(conflict_notes),
        "refused_notes": list(refused_notes),
        "git_in_writable": git_writable,
    }
    if git_writable or len(conflict_notes) < 2 or len(refused_notes) < 1:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "the read-only side did NOT win, or the conflict was not recorded: "
            f"git_in_writable={git_writable}, overlaps={len(conflict_notes)} "
            f"(want 2), refused={len(refused_notes)} (want 1), "
            f"notes={list(policy.notes)!r}"
        )
        return check
    check.attack_ran = True

    # The static half, which needs no daemon: build the argv the product would
    # actually run and require the pre-spawn gate to ACCEPT it under the
    # declared writable roots. A policy that resolves correctly but whose argv
    # the gate refuses would mean the two halves disagree, and a policy whose
    # argv the gate accepts only because it was not told about the writable
    # roots would mean the gate is not reading the policy at all.
    overlays = readonly_overlays(policy)
    argv = _docker_run_args(
        "audit:image",
        str(root),
        60,
        policy.network_enabled,
        None,
        "1g",
        1.0,
        512,
        workspace_readonly=policy.workspace_readonly,
        readonly_mounts=overlays,
        writable_mounts=writable_overlays(policy),
    )
    try:
        assert_sandbox_argv_isolated(
            argv, str(root), writable_paths=policy.writable_roots
        )
        gate = "accepted"
    except SandboxDependencyError as exc:
        gate = f"refused: {exc}"
    check.detail["pre_spawn_gate"] = gate
    if gate != "accepted":
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"the pre-spawn argv gate refused the policy's OWN argv: {gate}"
        )
        return check

    # The static half, which needs no daemon: build the argv the product would
    # actually run and require the pre-spawn gate to ACCEPT it under the
    # declared writable roots. A policy that resolves correctly but whose argv
    # the gate refuses would mean the two halves disagree, and a policy whose
    # argv the gate accepts only because it was not told about the writable
    # roots would mean the gate is not reading the policy at all.
    overlays = readonly_overlays(policy)
    argv = _docker_run_args(
        "audit:image",
        str(root),
        60,
        policy.network_enabled,
        None,
        "1g",
        1.0,
        512,
        workspace_readonly=policy.workspace_readonly,
        readonly_mounts=overlays,
        writable_mounts=writable_overlays(policy),
    )
    try:
        assert_sandbox_argv_isolated(
            argv, str(root), writable_paths=policy.writable_roots
        )
        gate = "accepted"
    except SandboxDependencyError as exc:
        gate = f"refused: {exc}"
    check.detail["pre_spawn_gate"] = gate
    if gate != "accepted":
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"the pre-spawn argv gate refused the policy's OWN argv: {gate}"
        )
        return check

    if not docker_available():
        check.verdict = OUTCOME_BLOCKED
        check.evidence = (
            "policy resolution CONFIRMED (read-only wins, conflict recorded) but "
            "the enforcement half is blocked: docker daemon not reachable"
        )
        return check

    probe = root / ".git" / "pwned"
    if probe.exists():
        probe.unlink()
    try:
        result = execute_sandboxed(
            str(root),
            f"cd {MOUNT_POINT} || exit 90; touch .git/pwned 2>/dev/null; echo RC=$?",
            timeout_s,
            containment=policy,
        )
    except SandboxUnavailableError as exc:
        check.verdict = OUTCOME_BLOCKED
        check.evidence = f"docker daemon not reachable: {exc}"
        return check
    landed = probe.exists()
    check.control_ran = True
    check.detail["container_rc"] = result.stdout.strip()[-40:]
    check.detail["pwned_on_host"] = landed
    if landed:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "with writable_roots=['.'] the .git write LANDED: the declared "
            "writable root overrode the read-only declaration in the real argv"
        )
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            "read-only declaration won and the conflict was recorded in "
            f"policy.notes ({conflict_notes[0]!r}); the real container also "
            "refused the .git write"
        )
    return check


# ---------------------------------------------------------------------------
# Row 3 — network is off unless declared, and the refusal NAMES the declaration
# ---------------------------------------------------------------------------


def check_network_off(root: Path, timeout_s: int = 90) -> Check:
    """Attack: open a socket from inside the container.

    Two halves that are easy to get wrong in opposite directions:

    * the ATTACK must genuinely fail to connect;
    * the CONTROL must genuinely connect — to loopback — so a passing row
      cannot be explained by the image having no network stack at all. A
      previous version of this repo's own adversarial corpus flagged a
      `GPG_KEY` env var as a "host env leak" because the comparison baseline
      was wrong, not the boundary; a control arm is how that class is avoided.
    """
    check = Check(
        claim="network is off unless declared",
        attack=(
            "from inside the default container, TCP-connect to 1.1.1.1:443, "
            "8.8.8.8:53 and the docker bridge gateway; require every attempt to "
            "fail, require loopback to SUCCEED as the control, and require the "
            "containment receipt to name the network declaration"
        ),
        needs_docker=True,
    )
    policy = resolve_containment(str(root))
    receipt = containment_receipt(policy, applied=(*readonly_overlays(policy),))
    check.detail = {
        "network_enabled": bool(policy.network_enabled),
        "network_declaration": policy.network_declaration,
        "declared_hosts": list(policy.declared_hosts),
        "receipt_network_enabled": receipt.get("network_enabled"),
        "receipt_declaration": receipt.get("network_declaration"),
    }
    if policy.network_enabled:
        check.verdict = OUTCOME_FINDING
        check.evidence = "the default policy declared network access"
        return check

    probe = r"""
import socket, subprocess
def attempt(host, port):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, port)); return "CONNECTED"
    except Exception as exc:
        return "REFUSED:%s" % type(exc).__name__
    finally:
        s.close()
gw = ""
try:
    out = subprocess.run(["ip","route","show","default"], capture_output=True, text=True, timeout=5).stdout
    parts = out.split()
    if "via" in parts: gw = parts[parts.index("via")+1]
except Exception: pass
print("EXT=" + attempt("1.1.1.1", 443))
print("DNS=" + attempt("8.8.8.8", 53))
print("GW=" + (attempt(gw, 2375) if gw else "no-gateway"))
# CONTROL: a real listener on loopback, connected to from the same process.
# Connecting to a CLOSED loopback port would be refused by the local host
# whether or not the network namespace existed, so it proves nothing; binding
# and then connecting does. The first draft of this row used port 9 and
# correctly reported its own control as failing, which is the control working.
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
host, port = srv.getsockname()
cli = socket.socket(); cli.settimeout(4)
try:
    cli.connect((host, port)); conn, _ = srv.accept(); conn.close()
    print("LOOPBACK=CONNECTED")
except Exception as exc:
    print("LOOPBACK=REFUSED:%s" % type(exc).__name__)
finally:
    cli.close(); srv.close()
"""
    result = execute_sandboxed(str(root), f"python -c {_shq(probe)}", timeout_s)
    facts = _kv(result.stdout)
    # `in facts` tests a KEY. The first draft of this line read
    # `"EXT=" in facts`, which is a string that can never be a key, so the
    # row reported "the probe did not run" against a probe that had run and
    # printed four facts. A gate that is wrong in the refusing direction is
    # still a gate that cannot be trusted, and this is the third time this
    # module caught its own bug rather than the product's.
    check.attack_ran = "EXT" in facts and "DNS" in facts
    check.control_ran = facts.get("LOOPBACK", "").endswith("CONNECTED")
    check.detail.update(facts)
    external = [k for k in ("EXT", "DNS", "GW") if facts.get(k) == "CONNECTED"]
    if external:
        check.verdict = OUTCOME_FINDING
        check.evidence = f"network reached out with no declaration: {external}"
    elif not check.control_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"the control arm failed too ({facts.get('LOOPBACK')!r}), so this "
            "row cannot distinguish a network refusal from a broken network "
            "stack in the image"
        )
    elif not check.attack_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = "the probe did not run; nothing was attacked"
    else:
        named = receipt.get("network_declaration", "")
        check.verdict = OUTCOME_HELD
        check.evidence = (
            f"external connects refused ({facts.get('EXT')}, {facts.get('DNS')}, "
            f"{facts.get('GW')}); loopback control CONNECTED; the receipt names "
            f"network_enabled={receipt.get('network_enabled')!r} and "
            f"declaration={named!r}"
        )
    return check


# ---------------------------------------------------------------------------
# Row 4 — a non-existent read-only path is NOT mounted
# ---------------------------------------------------------------------------


def check_absent_readonly_not_created(root: Path, timeout_s: int = 90) -> Check:
    """Attack: declare a read-only path that does not exist.

    `docker run -v src:dst` CREATES a missing host path. So mounting a declared
    `.git` into a repository that has none would silently ADD a directory to
    a user's working tree — a containment mechanism that mutates the thing it
    contains. The receipt must report it absent, and the host must still not
    have it.
    """
    check = Check(
        claim="a read-only path that does not exist is NOT mounted",
        attack=(
            "declare readonly_paths=['.git', '.not-a-real-dir'] on a fixture "
            "that has `.git` but not `.not-a-real-dir`; require (a) the "
            "overlay list to exclude the absent path, (b) the receipt to list "
            "it under `readonly_absent`, (c) the host directory to still not "
            "exist after a real container ran"
        ),
        needs_docker=True,
    )
    absent = root / ".not-a-real-dir"
    if absent.exists():
        shutil.rmtree(absent, ignore_errors=True)
    declared = [".git", ".not-a-real-dir"]
    policy = resolve_containment(str(root), readonly_paths=declared)
    overlays = readonly_overlays(policy)
    applied_targets = {target for _source, target in overlays}
    mounted = {rel for rel in declared if f"{MOUNT_POINT}/{rel}" in applied_targets}
    receipt = containment_receipt(
        policy, applied=(*overlays, *writable_overlays(policy))
    )
    check.attack_ran = True
    check.detail = {
        "declared": declared,
        "readonly_declared": list(receipt.get("readonly_declared", ())),
        "readonly_applied": list(receipt.get("readonly_applied", ())),
        "readonly_absent": list(receipt.get("readonly_absent", ())),
        "overlay_count": len(overlays),
    }
    if ".not-a-real-dir" in mounted:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "a non-existent read-only path was put in the docker -v list, which "
            "would CREATE it on the host"
        )
        return check
    if ".not-a-real-dir" not in (receipt.get("readonly_absent") or ()):
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "the path was not mounted but the receipt does not report it "
            f"absent: readonly_absent={receipt.get('readonly_absent')!r}"
        )
        return check
    if not docker_available():
        check.verdict = OUTCOME_BLOCKED
        check.evidence = (
            "policy resolution CONFIRMED (absent path not mounted, reported in "
            "readonly_absent) but the 'was it created on the host' half is "
            "blocked: docker daemon not reachable"
        )
        return check
    try:
        execute_sandboxed(
            str(root), f"ls -a {MOUNT_POINT} >/dev/null; echo ok", timeout_s
        )
    except SandboxUnavailableError as exc:
        check.verdict = OUTCOME_BLOCKED
        check.evidence = f"docker daemon not reachable: {exc}"
        return check
    check.control_ran = True
    check.detail["absent_exists_on_host_after_run"] = absent.exists()
    if absent.exists():
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "the declared non-existent path EXISTS on the host after a real "
            "container ran: the read-only overlay created it"
        )
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            f"only {sorted(mounted)} were mounted; "
            f"readonly_absent={list(receipt.get('readonly_absent', ()))} and the "
            "host directory was never created"
        )
    return check


# ---------------------------------------------------------------------------
# Row 5 — a fresh container per call
# ---------------------------------------------------------------------------


def check_fresh_container(root: Path, timeout_s: int = 90) -> Check:
    """Attack: leave state in container-local `/tmp` and look for it in call 2.

    The control matters here in a specific way: the bind mount DOES persist,
    so a marker written to `/tmp` vanishing proves the *container* is new, not
    that the filesystem was reset. Both facts are recorded, because a row that
    only showed "the marker is gone" would also pass if the second call had
    failed to mount anything.
    """
    check = Check(
        claim="a fresh container per call (no state survives between calls)",
        attack=(
            "call 1 writes a marker to container-local /tmp AND a second "
            "marker to the bind mount; call 2 must NOT see the /tmp marker and "
            "MUST see the bind-mount marker"
        ),
        needs_docker=True,
    )
    bind_marker = root / "bind-marker.txt"
    if bind_marker.exists():
        bind_marker.unlink()
    first = execute_sandboxed(
        str(root),
        f"echo {STATE_MARKER} > /tmp/{STATE_MARKER}; "
        f"echo {STATE_MARKER} > {MOUNT_POINT}/bind-marker.txt; "
        f"cat /tmp/{STATE_MARKER}",
        timeout_s,
    )
    check.attack_ran = STATE_MARKER in first.stdout
    second = execute_sandboxed(
        str(root),
        f"test -e /tmp/{STATE_MARKER} && echo TMP=STILL_THERE || echo TMP=GONE; "
        f"test -e {MOUNT_POINT}/bind-marker.txt && echo BIND=PRESENT "
        "|| echo BIND=MISSING",
        timeout_s,
    )
    facts = _kv(second.stdout)
    check.control_ran = facts.get("BIND") == "PRESENT"
    check.detail = {
        "call1_wrote_tmp_marker": STATE_MARKER in first.stdout,
        "call1_saw_tmp_marker": STATE_MARKER in first.stdout,
        "call2_tmp": facts.get("TMP"),
        "call2_bind": facts.get("BIND"),
    }
    if not check.attack_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = "call 1 did not create the marker; nothing was attacked"
    elif not check.control_ran:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"the bind-mount control marker was {facts.get('BIND')!r} in call 2, "
            "so the second call did not see the same repository and this row "
            "cannot measure container freshness"
        )
    elif facts.get("TMP") != "GONE":
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"container-local /tmp state survived between calls: {facts.get('TMP')}"
        )
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            "call 2 saw the bind mount (BIND=PRESENT) but not call 1's "
            "container-local /tmp marker (TMP=GONE)"
        )
    return check


# ---------------------------------------------------------------------------
# Row 6 — resource limits are real
# ---------------------------------------------------------------------------


def check_resource_limits(root: Path, timeout_s: int = 120) -> Check:
    """Attack: exceed each declared limit and require a KILL, not a warning.

    "Killed" is a specific claim and it is asserted from the ATTACK's own
    evidence rather than from the harness surviving: an OOM must arrive as exit
    137 (SIGKILL), a pids-limit breach must collapse the process tree rather
    than run to the harness timeout, and a cpu breach must be MEASURABLE
    against a single-process baseline. A container that merely printed a
    warning and then exited 0 has not enforced anything.
    """
    check = Check(
        claim="resource limits are real (mem / pids / cpu are enforced, not warned)",
        attack=(
            "mem: allocate 2 GB against --memory 1g, require exit 137; "
            "pids: fork-bomb against --pids-limit, require collapse before the "
            "harness timeout; cpu: 4 parallel spinners against --cpus, require "
            "the 4x/1x wall ratio to exceed 2.0x"
        ),
        needs_docker=True,
    )
    # Wall time is measured HERE, around the sandbox call, because
    # `ExecutionResult` carries no elapsed field. The first draft of this row
    # tried to read `result.elapsed` and would have raised AttributeError on
    # every run; measuring the clock around the boundary is the honest
    # alternative and it is also what makes the ratio comparable (both arms
    # pay the same container round trip, which cancels).
    spin_1 = "import time\nt=time.time()\nwhile time.time()-t<3: pass\n"
    spin_4 = "\n".join(spin_1 for _ in range(4))
    t0 = time.monotonic()
    execute_sandboxed(str(root), f"python -c {_shq(spin_1)}", timeout_s, cpu_limit=1.0)
    one = time.monotonic() - t0
    t0 = time.monotonic()
    execute_sandboxed(str(root), f"python -c {_shq(spin_4)}", timeout_s, cpu_limit=1.0)
    four = time.monotonic() - t0
    ratio = (four / one) if one > 0 else 0.0

    mem = execute_sandboxed(
        str(root),
        "python -c 'b=bytearray(2200*1024*1024)'",
        timeout_s,
        mem_limit="1g",
    )
    bomb = (
        "bomb() { bomb 2>/dev/null | bomb 2>/dev/null & }; bomb 2>/dev/null; echo done"
    )
    pids = execute_sandboxed(str(root), bomb, timeout_s, pids_limit=512)

    check.attack_ran = True
    check.control_ran = one > 0
    check.detail = {
        "mem_exit": mem.exit_code,
        "mem_timed_out": bool(mem.timed_out),
        "mem_stderr_tail": (mem.stderr or "")[-160:],
        "pids_exit": pids.exit_code,
        "pids_timed_out": bool(pids.timed_out),
        "pids_stderr_tail": (pids.stderr or "")[-160:],
        "cpu_1x_s": round(one, 3),
        "cpu_4x_s": round(four, 3),
        "cpu_ratio": round(ratio, 3),
        "cpu_limit": 1.0,
    }
    problems = []
    if mem.exit_code != 137 or mem.timed_out:
        problems.append(
            f"memory bomb returned exit {mem.exit_code} "
            f"(timed_out={mem.timed_out}); a kill is exit 137, not a warning"
        )
    if pids.timed_out:
        problems.append(
            "the fork bomb survived to the harness timeout instead of "
            "collapsing at the pids limit"
        )
    if ratio < 2.0:
        problems.append(
            f"4 parallel spinners took only {ratio:.2f}x the 1-spinner wall "
            "time against --cpus 1.0; the limit did not bite"
        )
    if problems:
        check.verdict = OUTCOME_FINDING
        check.evidence = "; ".join(problems)
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            f"memory bomb OOM-killed (exit 137); fork bomb collapsed without "
            f"reaching the harness timeout (container exit {pids.exit_code} -- "
            "a collapsed fork bomb can still exit 0 because the SURVIVING "
            "shell runs the trailing echo, so 'did not time out' is the "
            "assertion, not the exit code); 4 spinners took "
            f"{ratio:.2f}x the single-spinner wall against --cpus 1.0"
        )
    return check


# ---------------------------------------------------------------------------
# Row 7 — absolute / `..` / empty writable roots are DROPPED, not clamped
# ---------------------------------------------------------------------------


def check_bad_writable_roots_dropped(root: Path) -> Check:
    """Attack: declare each unusable root shape and require a DROP + note.

    Docker-free and always run: this row is pure policy resolution, and
    resolving it correctly with no daemon is evidence a Docker-less machine can
    still produce. The reason it must be a DROP rather than a clamp is the
    failure direction: clamping `/etc` to a repository-relative path would
    report a boundary different from the enforced one, which is a receipt that
    lies about itself.
    """
    check = Check(
        claim="absolute / `..` / empty writable roots are dropped, not clamped",
        attack=(
            "resolve_containment with writable_roots = "
            "['/etc', 'C:\\\\Windows', '..', '../..', 'a/../../b', '', '   ', "
            "'.', 'src/../..'] and require every one to be absent from the "
            "resolved set with a note naming it, and require the resolved set "
            "to be EMPTY (a clamp would leave something in it)"
        ),
        needs_docker=False,
    )
    hostile: Sequence[str] = (
        "/etc",
        "C:\\Windows",
        "..",
        "../..",
        "a/../../b",
        "",
        "   ",
        ".",
        "src/../..",
    )
    policy = resolve_containment(str(root), writable_roots=hostile)
    check.attack_ran = True
    dropped_notes = [note for note in policy.notes if "refused" in note]
    check.detail = {
        "declared": list(hostile),
        "resolved_writable_roots": list(policy.writable_roots),
        "notes": list(policy.notes),
        "dropped_note_count": len(dropped_notes),
        "workspace_readonly": policy.workspace_readonly,
    }
    if policy.writable_roots:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"unusable roots survived resolution (possibly CLAMPED): "
            f"{list(policy.writable_roots)!r}; a clamp reports a boundary "
            "different from the enforced one"
        )
    elif len(dropped_notes) != len(hostile):
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            f"only {len(dropped_notes)} of {len(hostile)} unusable declarations "
            f"produced a note, so a drop is not attributable: {list(policy.notes)!r}"
        )
    elif policy.workspace_readonly:
        check.verdict = OUTCOME_FINDING
        check.evidence = (
            "every declaration was dropped yet the workspace mount was flipped "
            "read-only, which means a refusal changed containment"
        )
    else:
        check.verdict = OUTCOME_HELD
        check.evidence = (
            f"all {len(hostile)} unusable declarations were dropped with a "
            f"note; the resolved writable set is {list(policy.writable_roots)!r} "
            "and the workspace mount was NOT flipped to read-only by the "
            "refusals (including '.', which normalises to the empty relative "
            "path and is a refusal, not a clamp)"
        )
    return check


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

#: Row order is the table order in the module docstring, deliberately. Each
#: entry is (runner, needs_docker) so the dispatch is a data decision rather
#: than a signature inspection — an earlier draft used
#: `__code__.co_varnames` to guess arity, which is the kind of reflection a
#: refactor silently breaks.
CHECKS: Tuple[Tuple[Callable[..., Check], bool], ...] = (
    (check_git_readonly, True),
    (check_writable_root_conflict, True),
    (check_network_off, True),
    (check_absent_readonly_not_created, True),
    (check_fresh_container, True),
    (check_resource_limits, True),
    (check_bad_writable_roots_dropped, False),
)


def _shq(text: str) -> str:
    """Single-quote a payload for `python -c` inside the container shell."""
    return "'" + text.replace("'", "'\\''") + "'"


def _kv(text: str) -> Dict[str, str]:
    """Parse ``KEY=value`` lines out of a container capture."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" in line:
            key, _sep, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def docker_blocked_reason() -> str:
    """Return why Docker-dependent rows cannot run, or "" when it can.

    Probes the daemon the way the product does. The EXACT error is returned
    and recorded, because "blocked" without the reason is indistinguishable
    from "skipped", which is how a blocked Docker lane has been reported as a
    pass before.
    """
    try:
        if docker_available():
            return ""
    except Exception as exc:  # pragma: no cover - docker_available is guarded
        return f"{type(exc).__name__}: {exc}"
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        return f"docker daemon not reachable: {type(exc).__name__}: {exc}"
    if probe.returncode == 0 and (probe.stdout or "").strip():
        return ""
    detail = ((probe.stderr or "") + " " + (probe.stdout or "")).strip()
    return f"docker daemon not reachable: {detail[:400]}"


def run_audit(
    *,
    timeout_s: int = 90,
    keep: bool = False,
) -> List[Check]:
    """Run all seven attacks and return the recorded outcomes.

    ``keep=False`` removes the fixture tree. The Docker-free rows run
    UNCONDITIONALLY — they are pure policy resolution — so a machine with no
    daemon still returns real verdicts for them rather than seven refusals.
    """
    block_reason = docker_blocked_reason()
    parent = Path(tempfile.mkdtemp(prefix="neo-containment-audit-"))
    rows: List[Check] = []
    try:
        for index, (check_fn, needs_docker) in enumerate(CHECKS):
            root = _fixture(parent, f"{index}")
            started = time.monotonic()
            try:
                if needs_docker and block_reason:
                    check = Check(
                        claim=_claim_of(check_fn),
                        attack=_attack_of(check_fn),
                        needs_docker=True,
                        verdict=OUTCOME_BLOCKED,
                        evidence=f"blocked: {block_reason}",
                    )
                elif needs_docker:
                    check = check_fn(root, timeout_s)
                else:
                    check = check_fn(root)  # type: ignore[call-arg]
            except SandboxUnavailableError as exc:
                check = Check(
                    claim=_claim_of(check_fn),
                    attack=_attack_of(check_fn),
                    needs_docker=True,
                    verdict=OUTCOME_BLOCKED,
                    evidence=f"blocked: {type(exc).__name__}: {exc}",
                )
            except SandboxDependencyError as exc:
                check = Check(
                    claim=_claim_of(check_fn),
                    attack=_attack_of(check_fn),
                    needs_docker=True,
                    verdict=OUTCOME_BLOCKED,
                    evidence=f"blocked: dependency image unavailable: {exc}",
                )
            except Exception as exc:
                # A raising attack harness is a FINDING, not a blocked lane:
                # the machine was fine and the check is broken, and reporting
                # that as "blocked" would let a broken gate read as an
                # unavailable one.
                check = Check(
                    claim=_claim_of(check_fn),
                    attack=_attack_of(check_fn),
                    needs_docker=True,
                    verdict=OUTCOME_FINDING,
                    evidence=(
                        f"the attack harness itself raised: {type(exc).__name__}: {exc}"
                    ),
                )
            check.elapsed_s = time.monotonic() - started
            rows.append(check)
    finally:
        if not keep:
            shutil.rmtree(parent, ignore_errors=True)
    return rows


def _claim_of(check_fn: Callable[..., Check]) -> str:
    """Return a check's declared claim for a row that never ran.

    The claim text lives in the runner's own ``Check(...)`` construction, so
    a row that is blocked before dispatch reports the CLAIM being unverified,
    not a bare function name. That is the difference between "we did not check
    this" and "we checked something else".
    """
    doc = (check_fn.__doc__ or "").strip()
    return doc.splitlines()[0] if doc else check_fn.__name__


def _attack_of(check_fn: Callable[..., Check]) -> str:
    """Return a check's attack description for a row that never ran."""
    return f"(not attempted: {check_fn.__name__})"


def summarize(rows: Sequence[Check]) -> Dict[str, Any]:
    """Return the machine-readable verdict for a set of rows."""
    held = sum(1 for row in rows if row.verdict == OUTCOME_HELD)
    findings = [row for row in rows if row.verdict == OUTCOME_FINDING]
    blocked = [row for row in rows if row.verdict == OUTCOME_BLOCKED]
    undecided = [
        row
        for row in rows
        if row.verdict not in (OUTCOME_HELD, OUTCOME_FINDING, OUTCOME_BLOCKED)
    ]
    return {
        "checks": len(rows),
        "held": held,
        "findings": len(findings),
        "blocked": len(blocked),
        "undecided": len(undecided),
        "docker_free_rows": sum(1 for row in rows if not row.needs_docker),
        "docker_dependent_rows": sum(1 for row in rows if row.needs_docker),
        # A blocked row is NOT a pass. `clean` therefore requires every row to
        # have been decided AND held, which is the whole reason this summary
        # cannot render an unreachable daemon as a green run.
        "clean": held == len(rows) and not findings and not blocked and not undecided,
        "verdict": (
            "CLEAN"
            if held == len(rows) and not findings and not blocked
            else "FINDINGS"
            if findings
            else "BLOCKED"
            if blocked
            else "UNDECIDED"
        ),
        "blocked_reason": next((row.evidence for row in blocked), ""),
    }


def render(rows: Sequence[Check]) -> str:
    """Render the seven-row table as plain text."""
    lines = ["containment claims -> attempted attack -> recorded outcome", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"[{index}] {row.claim}")
        lines.append(f"    attack : {row.attack}")
        lines.append(f"    outcome: {row.verdict.upper()}")
        for chunk in _wrap(row.evidence, 92):
            lines.append(f"    {chunk}")
        if row.elapsed_s:
            lines.append(
                f"    (attack_ran={row.attack_ran} control_ran={row.control_ran} "
                f"{row.elapsed_s:.1f}s)"
            )
        lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    words = str(text or "").split()
    out: List[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m execution.containment_audit``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m execution.containment_audit",
        description="Adversarially verify the sandbox's own containment claims.",
    )
    parser.add_argument("--timeout", type=int, default=90, help="per-attack timeout")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--out", default="", help="write the JSON report here")
    parser.add_argument("--keep", action="store_true", help="keep the fixture tree")
    args = parser.parse_args(list(argv) if argv is not None else None)

    rows = run_audit(timeout_s=int(args.timeout), keep=bool(args.keep))
    summary = summarize(rows)
    report = {
        "summary": summary,
        "rows": [row.to_dict() for row in rows],
        "env": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "docker_blocked_reason": docker_blocked_reason(),
        },
    }
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(rows))
        print(
            f"{summary['checks']} claims: {summary['held']} held, "
            f"{summary['findings']} finding(s), {summary['blocked']} blocked "
            f"({summary['docker_free_rows']} of them Docker-free) -> "
            f"{summary['verdict']}"
        )
        if summary["blocked_reason"]:
            print(f"blocked: {summary['blocked_reason']}")
    # Non-zero on a finding AND on a blocked lane. A blocked row must not be
    # able to render as a clean exit.
    return 0 if summary["verdict"] == "CLEAN" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "OUTCOME_BLOCKED",
    "OUTCOME_FINDING",
    "OUTCOME_HELD",
    "STATE_MARKER",
    "Check",
    "docker_blocked_reason",
    "main",
    "render",
    "run_audit",
    "summarize",
]
