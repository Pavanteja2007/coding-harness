"""ADVERSARIAL sandbox testing (Round 6, Terminal 2): deliberately hostile
commands against the sandbox — resource exhaustion, escape attempts, and
cross-container interference — sequentially (Task A) and under real
concurrent load (Task B). NOT part of the pytest suite (spins dozens of
real containers, some intentionally hostile); run explicitly:

    python -m execution.sandbox_adversarial                   # Task A
    python -m execution.sandbox_adversarial --concurrency 8  # Task B

Design: every attack is a self-verifying probe. Python payloads are
written as FILES into a disposable fixture repo (no nested shell
quoting to get wrong) and print a "VULN-..." marker iff the attack
SUCCEEDS from the attacker's point of view. Bash probes echo the marker
under the same rule. Verdicts:

  blocked   — the marker must NOT appear (container stopped the attack);
  killed    — a resource limit must stop the attack: nonzero exit
              (OOM kill / pids collapse / dd ENOSPC) or timeout(124),
              never a clean exit 0;
  contained — the attack runs to completion INSIDE the box (exit 0) but
              a check() verifies it stayed bounded (cpu ratio, output
              size, pid count, env cleanliness);
  info      — outcome recorded for the audit trail; not a break either
              way (e.g. unshare userns is non-escalating by itself —
              see AGENTS.md analysis);
  design    — expected outcome IS the "bad" one and is accepted by
              design with an explanation (the RW bind mount reaching
              host disk with no quota — the mount is the product).

Host-side invariants: after the run no hexec-* container remains
(polled, teardown-async aware), and fixture repos are cleaned up.
Report: logs/sandbox-adversarial/<ts>/*.json. Exit 0 = everything held
(accepted-design findings don't fail the run); 1 = >=1 real finding;
2 = environment problem.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from execution import sandbox as sb  # noqa: E402

MARK = "VULN"

# ---------------------------------------------------------------------------
# Python payloads (written to files in the fixture repo, run as
# `python <name>.py` inside the container)
# ---------------------------------------------------------------------------

PY_HOSTMOUNTS = (
    r'''
"""Enumerate every mount visible in-container; flag any mount whose
source looks like a HOST directory other than our own /workspace."""
import re
bad = []
for line in open("/proc/self/mountinfo", encoding="utf-8", errors="replace"):
    try:
        rest = line.split(" - ", 1)[1]
        fstype, source = rest.split()[0], rest.split()[1]
    except (IndexError, ValueError):
        continue
    mountpoint = line.split()[4]
    hostish = ("grpcfuse" in line or "osxfs" in fstype or "virtiofs" in fstype
               or re.match(r"^[A-Za-z]:", source))
    if hostish and mountpoint != "/workspace":
        bad.append((mountpoint, source, fstype))
print("EXTRA-HOST-MOUNTS", bad)
print("'''
    + MARK
    + r"""-HOSTMOUNT" if bad else "only-own-workspace-mounted")
"""
)

PY_SOCKET = (
    r'''
"""Try to reach the docker daemon socket (unix)."""
import socket
try:
    s = socket.socket(socket.AF_UNIX)
    s.connect("/var/run/docker.sock")
    print("'''
    + MARK
    + r"""-SOCKET")
except Exception as e:
    print("docker-socket-unreachable", type(e).__name__)
"""
)

PY_NET = (
    r'''
"""Try to reach the outside network (must be blocked by --network none)."""
import socket
for host, port in [("example.com", 80), ("8.8.8.8", 53), ("1.1.1.1", 443)]:
    try:
        socket.create_connection((host, port), 4)
        print("'''
    + MARK
    + r"""-NET", host)
    except Exception as e:
        print("net-blocked", host, type(e).__name__)
"""
)

PY_IFACES = (
    r'''
"""What network interfaces exist in here? (cross-container reachability
needs an interface besides loopback; --network none must leave only lo.)"""
import socket
try:
    import fcntl
    import array
    import struct
    names = []
    with open("/proc/net/dev", encoding="utf-8") as fh:
        fh.readline(); fh.readline()
        for line in fh:
            names.append(line.split(":")[0].strip())
    print("INTERFACES", names)
    real = [n for n in names if n != "lo"]
    print("'''
    + MARK
    + r"""-REAL-IFACE" if real else "loopback-only")
except Exception as e:
    print("iface-probe-error", e)
"""
)

PY_PIDNS = (
    r'''
"""PID-namespace isolation: pid 1 must be THIS container's own entry
process (bash), not the host/VM init, and the pid list must be tiny."""
import os
comm = open("/proc/1/comm", encoding="utf-8").read().strip()
pids = [p for p in os.listdir("/proc") if p.isdigit()]
print("PID1-COMM", repr(comm), "NPIDS", len(pids))
if comm not in ("bash", "sh", "dash", "python", "python3") or len(pids) > 50:
    print("'''
    + MARK
    + r"""-PIDNS")
else:
    print("pidns-isolated")
"""
)

PY_ENV_LEAK = (
    r'''
"""Flag env vars NOT baked into the sandbox image / not passed by the
harness — i.e., anything actually leaked from the HOST or CALLER. (The
python image ships PATH/GPG_KEY/LANG/PYTHON_*; bash sets PWD/SHLVL/_/
OLDPWD; the python image also ships PYTHON_SHA256; harness passes HOME.)
"""
import os

BASELINE = {
    "PATH", "HOSTNAME", "HOME", "GPG_KEY", "LANG",
    "PYTHON_VERSION", "PYTHON_PIP_VERSION", "PYTHON_GET_PIP_URL",
    "PYTHON_GET_PIP_SHA256", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PWD", "SHLVL", "_", "OLDPWD", "PYTHON_SHA256",
}
leaked = {k: v[:40] for k, v in sorted(os.environ.items())
          if k not in BASELINE}
print("ENV-NOT-IN-BASELINE", leaked or "none")
print("'''
    + MARK
    + r"""-ENV" if leaked else "env-clean")
"""
)

PY_MEM_BOMB = r'''
"""Instant 2GB allocation vs the 1g memory limit."""
a = bytearray(2 * 1024 * 1024 * 1024)
print("allocated-2gb")  # unreachable under the limit
'''

PY_MEM_LEAK = r'''
"""Gradual leak (~100MB/s to 2GB) vs the 1g memory limit."""
import time
chunks = []
for i in range(32):
    chunks.append(bytearray(64 * 1024 * 1024))
    time.sleep(0.3)
print("leaked-2gb")  # unreachable under the limit
'''

PY_HOSTFS = (
    r'''
"""Reads of paths that would expose HOST files if any escape existed.
- /etc/shadow etc.: must be PermissionError (non-root).
- /proc/1/root/...: must fail (own pidns, pid1 is our bash).
- /proc/kcore: must not yield >0 bytes to non-root.
- HOST-ONLY DIRS (Users/Windows/Program Files/pavan): must not exist
  anywhere in the container root or above the /workspace mount — that
  would mean a real host filesystem is visible.
"""
import os

hits = []
for p in ["/etc/shadow", "/proc/1/root/etc/shadow", "/proc/kcore"]:
    try:
        with open(p, "rb") as f:
            data = f.read(32)
            if data:
                hits.append((p, data))
    except OSError:
        print("no-access", p)

host_only_names = ("Users", "Windows", "Program Files", "ProgramData",
                   "pavan")
for root in ("/", "/workspace/.."):
    try:
        entries = os.listdir(root)
    except OSError as e:
        print("no-list", root, type(e).__name__)
        continue
    found = [e for e in entries if e in host_only_names]
    print("LISTING", root, "->", sorted(entries)[:20])
    if found:
        hits.append((root, found))

print("HOSTFS-HITS", hits)
print("'''
    + MARK
    + r"""-HOSTFS" if hits else "no-host-files-readable")
"""
)


# ---------------------------------------------------------------------------
# Attack corpus (sequential, Task A)
# ---------------------------------------------------------------------------


def _bash(marker_name: str, inner: str) -> str:
    """Wrap one bash probe: echo the VULN marker iff `inner` succeeds."""
    return f'sh -c "{inner} && echo {MARK}-{marker_name} || true"'


def _check_cpu_ratio(res) -> Optional[str]:
    """cpu-limit check: 4 parallel spinners must take ~4x one spinner."""
    t1 = t4 = None
    for tok in res.stdout.replace("ms", " ").split():
        if tok.startswith("CPU-T1="):
            t1 = float(tok.split("=")[1])
        if tok.startswith("CPU-T4="):
            t4 = float(tok.split("=")[1])
    if not t1 or not t4 or t1 < 200:
        return f"cpu probe unparseable: t1={t1} t4={t4} out={res.stdout[-200:]!r}"
    ratio = t4 / t1
    print(
        f"    cpu ratio T4/T1 = {ratio:.2f} (limit holds if >= ~2.5; "
        f"unlimited 12-core box would give ~1.0)"
    )
    if ratio < 2.5:
        return (
            f"cpu limit NOT biting: 4 parallel spinners took only "
            f"{ratio:.2f}x single-spinner time (t1={t1}ms t4={t4}ms)"
        )
    return None


def _check_output_size(res) -> Optional[str]:
    if len(res.stdout) > 2_000_000:
        return f"stdout flooded to {len(res.stdout)} bytes (cap ~1MB)"
    return None


def _check_pidns(res) -> Optional[str]:
    if MARK in res.stdout:
        return f"pid namespace not isolated: {res.stdout[-300:]!r}"
    return None


def _check_env(res) -> Optional[str]:
    if MARK in res.stdout:
        return f"host secrets leaked into container env: {res.stdout[-400:]!r}"
    return None


def _check_hostmounts(res) -> Optional[str]:
    if MARK in res.stdout:
        return f"extra host mount visible: {res.stdout[-400:]!r}"
    return None


_SPIN = (
    "spin() { local i=0; while [ $i -lt 2000000 ]; do i=$((i+1)); done; }; "
    "t0=$(date +%s%N); spin; t1=$(date +%s%N); "
    "spin & spin & spin & spin & wait; t2=$(date +%s%N); "
    'echo "CPU-T1=$(( (t1-t0)/1000000 ))ms CPU-T4=$(( (t2-t1)/1000000 ))ms"'
)

ATTACKS: List[Dict] = [
    # ---------------- Escape attempts ---------------------------------
    dict(
        name="escape-host-mounts",
        expect="blocked",
        note="any mount reaching a host dir besides our own /workspace",
        py_files={"probe.py": PY_HOSTMOUNTS},
        command="python probe.py",
        check=_check_hostmounts,
    ),
    dict(
        name="escape-host-fs-read",
        expect="blocked",
        note="read host OS files (/etc/shadow, /proc/1/root, dotdot out of mount)",
        py_files={"probe.py": PY_HOSTFS},
        command="python probe.py",
        check=lambda res: (
            f"host files readable: {res.stdout[-300:]!r}"
            if MARK in res.stdout
            else None
        ),
    ),
    dict(
        name="escape-docker-socket",
        expect="blocked",
        note="reach the docker daemon socket (container/host takeover)",
        py_files={"probe.py": PY_SOCKET},
        command=_bash("SOCKBASH", "test -S /var/run/docker.sock") + "; python probe.py",
        check=None,
    ),
    dict(
        name="escape-network-default",
        expect="blocked",
        note="reach outside network without allow_network=True",
        py_files={"probe.py": PY_NET},
        command="python probe.py",
        check=lambda res: (
            f"network open: {res.stdout[-200:]!r}" if MARK in res.stdout else None
        ),
    ),
    dict(
        name="escape-su-root",
        expect="blocked",
        note="su to root inside the container",
        command=_bash("SU", "su root -c true") + "; true",
    ),
    dict(
        name="escape-chown-root",
        expect="blocked",
        note="chown a file to root (SUID-plant precondition; needs CAP_CHOWN)",
        command=_bash("CHOWN", "touch /tmp/f && chown 0:0 /tmp/f"),
    ),
    dict(
        name="escape-mount-proc",
        expect="blocked",
        note="mount a fresh procfs (needs CAP_SYS_ADMIN)",
        command=_bash("MOUNT", "mkdir -p /tmp/mnt && mount -t proc none /tmp/mnt"),
    ),
    dict(
        name="escape-unshare-userns",
        expect="info",
        note="unshare a new user+mount+pid namespace (non-escalating by "
        "itself — records whether seccomp permits userns creation)",
        command=_bash(
            "UNSHARE", "unshare --user --map-root-user --mount --pid --fork true"
        ),
    ),
    dict(
        name="escape-nsenter-pid1",
        expect="info",
        note="nsenter into pid 1's namespaces (a no-op if pidns is "
        "isolated — the escape only matters if pid 1 were the HOST init)",
        command=_bash("NSENTER", "nsenter -t 1 -m -u -i -n true"),
    ),
    dict(
        name="escape-pid-namespace",
        expect="contained",
        note="pid namespace isolation (pid1 must be the container's own bash)",
        py_files={"probe.py": PY_PIDNS},
        command="python probe.py",
        check=_check_pidns,
    ),
    dict(
        name="escape-env-leak",
        expect="contained",
        note="no host secrets (keys/tokens/harness vars) in container env",
        py_files={"probe.py": PY_ENV_LEAK},
        command="python probe.py",
        check=_check_env,
    ),
    dict(
        name="escape-workspace-siblings",
        expect="blocked",
        note="traverse out of the bind mount: no HOST directory names "
        "(Users/Windows/Program Files/pavan) may be listable at / or "
        "above the /workspace mount",
        command=_bash(
            "PARENT",
            "ls /workspace/.. | grep -qE '^(Users|Windows|Program Files|pavan)$'"
            " || ls / | grep -qE '^(Users|Windows|Program Files|pavan)$'",
        ),
    ),
    # ---------------- Resource exhaustion ------------------------------
    dict(
        name="resource-fork-bomb",
        expect="killed",
        note="classic bash fork bomb vs --pids-limit 512: the bomb must "
        "collapse quickly (fork: Cannot allocate memory at the pid "
        "cap) and never survive to the harness timeout — collapse "
        "may exit 0/1/2; surviving 90s would mean no limit bit",
        command="t0=$(date +%s); bomb() { bomb | bomb & }; bomb; wait; "
        'echo "bomb-survived-$(( $(date +%s) - t0 ))s"',
        check=lambda res: (
            "fork bomb survived to harness timeout — pids-limit did NOT bite"
            if res.timed_out
            else None
        ),
    ),
    dict(
        name="resource-mem-bomb",
        expect="killed",
        note="instant 2GB allocation vs 1g memory limit",
        py_files={"bomb.py": PY_MEM_BOMB},
        command="python bomb.py",
    ),
    dict(
        name="resource-mem-leak",
        expect="killed",
        note="gradual ~64MB/0.3s leak to 2GB vs 1g memory limit",
        py_files={"leak.py": PY_MEM_LEAK},
        command="python leak.py",
    ),
    dict(
        name="resource-disk-tmpfs",
        expect="killed",
        note="1GB dd into /tmp vs 256m tmpfs cap: dd must be stopped at "
        "~256MB (ENOSPC), not complete 1GB; exit is reported raw "
        "(pipe exit codes vary) — the SIZE is the assertion",
        command="dd if=/dev/zero of=/tmp/fill bs=1M count=1024 2>/tmp/dd.err; "
        'echo "dd-exit=$?"; tail -1 /tmp/dd.err; '
        'sz=$(stat -c %s /tmp/fill 2>/dev/null || echo 0); echo "fill-size=$sz"; '
        '[ "$sz" -le 268435456 ] || echo FILL-OVER-CAP',
        check=lambda res: (
            "tmpfs cap bypassed: fill over 256MiB"
            if "FILL-OVER-CAP" in res.stdout
            else None
        ),
    ),
    dict(
        name="resource-disk-workspace",
        expect="design",
        note="256MB dd into /workspace reaches the HOST disk unquota'd — "
        "bind mounts have no size limit in docker; RW mount is the "
        "product contract (T1 diffs host-side). Recorded as accepted "
        "design exposure, cleaned up after.",
        command="dd if=/dev/zero of=/workspace/fill bs=1M count=256 2>&1 | tail -1; "
        "echo \"dd-exit=$?\"; ls -l /workspace/fill 2>/dev/null | awk '{print $5}'; "
        "rm -f /workspace/fill",
    ),
    dict(
        name="resource-cpu-limit",
        expect="contained",
        note="--cpus 1.0: 4 parallel spin loops must take ~4x single-loop time",
        command=_SPIN,
        check=_check_cpu_ratio,
    ),
    dict(
        name="resource-output-flood",
        expect="contained",
        note="100MB stdout flood must come back bounded (~1MB cap)",
        command='yes "0123456789ABCDEFGH" | head -c 104857600; echo; echo FLOOD-DONE',
        check=_check_output_size,
    ),
    dict(
        name="resource-cpu-infinite",
        expect="killed",
        note="infinite spin must be stopped by the timeout, not run forever",
        command="while :; do :; done",
    ),
]


# ---------------------------------------------------------------------------
# Machinery
# ---------------------------------------------------------------------------


def _mk_fixture(
    parent: Path, tag: str, py_files: Optional[Dict[str, str]] = None
) -> Path:
    """Disposable tiny repo; py payloads land in it as files."""
    repo = parent / f"adv-{tag}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "mymod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
    )
    for name, src in (py_files or {}).items():
        (repo / name).write_text(src, encoding="utf-8")
    return repo


def _verdict(atk: Dict, res) -> Dict:
    row = dict(
        name=atk["name"],
        note=atk.get("note", ""),
        expect=atk["expect"],
        exit_code=res.exit_code,
        timed_out=res.timed_out,
        marker_seen=MARK in (res.stdout + res.stderr),
        stdout_tail=res.stdout[-400:],
        stderr_tail=res.stderr[-400:],
    )
    finding: Optional[str] = None
    if atk["expect"] == "blocked":
        if row["marker_seen"]:
            finding = f"attack succeeded — {MARK} marker present"
    elif atk["expect"] == "killed":
        if atk.get("check"):
            # check() is authoritative: e.g. dd's final SIZE vs the cap,
            # or "must not survive to the harness timeout".
            pass
        elif res.timed_out:
            pass  # harness timeout stopped it — the time limit bit
        elif res.exit_code == 0:
            finding = "attack completed CLEANLY (exit 0) — limit did not bite"
    elif atk["expect"] == "contained":
        if res.exit_code != 0 and not res.timed_out:
            finding = f"expected a contained run, got exit {res.exit_code}"
    elif atk["expect"] in ("info", "design"):
        pass  # recorded, never a finding by itself
    if not finding and atk.get("check"):
        finding = atk["check"](res)
    row["finding"] = finding
    row["verdict"] = (
        "HELD"
        if not finding
        else "CONFIRMED-BY-DESIGN"
        if atk["expect"] == "design"
        else "FINDING"
    )
    return row


def _run_one(atk: Dict, parent: Path, tag: str, timeout_s: int = 90) -> Dict:
    repo = _mk_fixture(parent, tag, atk.get("py_files"))
    t0 = time.time()
    try:
        res = sb.execute_sandboxed(str(repo), atk["command"], timeout_s)
    except Exception as exc:
        return dict(
            name=atk["name"],
            expect=atk["expect"],
            exception=f"{type(exc).__name__}: {exc}"[:300],
            verdict="FINDING",
            finding="execute_sandboxed raised",
        )
    row = _verdict(atk, res)
    row["elapsed_s"] = round(time.time() - t0, 1)
    expected = {"mymod.py", "pyproject.toml", "tests"} | set(atk.get("py_files") or {})
    row["stray_files"] = sorted(
        p.name for p in repo.iterdir() if p.name not in expected
    )
    shutil.rmtree(repo, ignore_errors=True)
    return row


def _no_hexec_residue(timeout_s: float = 60.0) -> List[str]:
    """Poll until no hexec-* container remains (teardown-async aware)."""
    deadline = time.time() + timeout_s
    while True:
        ls = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=hexec-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        names = ls.stdout.split()
        if not names or time.time() > deadline:
            return names
        time.sleep(2.0)


# ---------------------------------------------------------------------------
# Cross-container interference scenarios (need custom orchestration)
# ---------------------------------------------------------------------------


def _cross_scenarios(out_dir: Path, timeout_s: int = 60) -> List[Dict]:
    rows: List[Dict] = []
    workdir = out_dir / "repos-cross"
    workdir.mkdir(parents=True, exist_ok=True)

    # Scenario 1: filesystem — victim repo's marker must be invisible to a
    # concurrently-running attacker container mounted elsewhere.
    victim = _mk_fixture(workdir, "fs-victim")
    marker = f"VICTIM-MARKER-{os.urandom(4).hex()}"
    (victim / marker).write_text("secret", encoding="utf-8")
    attacker = _mk_fixture(workdir, "fs-attacker")
    res = sb.execute_sandboxed(
        str(attacker),
        f'count=$(find / -name "{marker}" 2>/dev/null | wc -l); '
        f'echo "victim-marker-hits=$count"; '
        f'[ "$count" -gt 0 ] && echo {MARK}-FS-CROSS || echo fs-isolated',
        timeout_s,
    )
    rows.append(
        dict(
            name="cross-filesystem-visibility",
            expect="blocked",
            note="concurrent task's repo files must be invisible to another task",
            exit_code=res.exit_code,
            timed_out=res.timed_out,
            marker_seen=MARK in res.stdout,
            stdout_tail=res.stdout[-300:],
            verdict="FINDING" if MARK in res.stdout else "HELD",
            finding=None,
        )
    )
    shutil.rmtree(victim, ignore_errors=True)
    shutil.rmtree(attacker, ignore_errors=True)

    # Scenario 2: network — a networkless victim sleeps while a networkless
    # attacker checks it has only loopback (nothing to reach anyone with),
    # and an OPTED-IN (allow_network=True) attacker confirms it cannot find
    # the networkless victim either (victim isn't on any network).
    victim = _mk_fixture(workdir, "net-victim")
    victim_res: Dict = {}
    victim_done = threading.Event()

    def run_victim():
        r = sb.execute_sandboxed(str(victim), "sleep 20; echo victim-done", 40)
        victim_res["res"] = r
        victim_done.set()

    th = threading.Thread(target=run_victim)
    th.start()
    time.sleep(4.0)  # let the victim container actually be Up

    attackerA = _mk_fixture(workdir, "net-attackerA", {"ifaces.py": PY_IFACES})
    resA = sb.execute_sandboxed(str(attackerA), "python ifaces.py", timeout_s)
    rows.append(
        dict(
            name="cross-network-default",
            expect="blocked",
            note="default (networkless) containers have no interface to reach "
            "other tasks with",
            exit_code=resA.exit_code,
            timed_out=resA.timed_out,
            marker_seen=MARK in (resA.stdout + resA.stderr),
            stdout_tail=resA.stdout[-300:],
            verdict="FINDING" if MARK in (resA.stdout + resA.stderr) else "HELD",
            finding=None,
        )
    )

    attackerB = _mk_fixture(
        workdir,
        "net-attackerB",
        {
            "scan.py": r'''
"""Opted-in (allow_network=True) container: enumerate the bridge subnet
and try to find the networkless VICTIM (it must not be reachable — it is
not attached to any network). Prints VULN if any host other than the
gateway/ourselves answers on common ports."""
import socket, concurrent.futures as cf

def probe(ip):
    for port in (22, 80, 443, 8000, 5000):
        try:
            s = socket.create_connection((ip, port), 0.5)
            s.close()
            return ip, port
        except OSError:
            continue
    return None

# our own ip + default gateway
own = None
try:
    t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t.connect(("192.0.2.1", 9))  # no traffic sent; picks a route
    own = t.getsockname()[0]
    t.close()
except OSError:
    pass
print("OWN-IP", own)
prefix = ".".join(own.split(".")[:3]) if own else None
found = []
if prefix:
    with cf.ThreadPoolExecutor(max_workers=32) as pool:
        for r in pool.map(probe, [f"{prefix}.{i}" for i in range(2, 255)]):
            if r:
                found.append(r)
print("BRIDGE-HITS", found)
others = [ip for ip, _ in found if own and ip != own]
print("'''
            + MARK
            + """-BRIDGE-PEER" if others else "no-other-harness-container-reachable")
"""
        },
    )
    resB = sb.execute_sandboxed(
        str(attackerB), "python scan.py", timeout_s, allow_network=True
    )
    rows.append(
        dict(
            name="cross-network-optin-vs-victim",
            expect="blocked",
            note="opted-in network container scans the bridge subnet: must "
            "find nothing but itself/gateway (networkless victim is not "
            "attachable); opt-in<->opt-in visibility is an inherent "
            "allow_network caveat, recorded in AGENTS.md",
            exit_code=resB.exit_code,
            timed_out=resB.timed_out,
            marker_seen=MARK in (resB.stdout + resB.stderr),
            stdout_tail=resB.stdout[-400:],
            verdict="FINDING" if MARK in (resB.stdout + resB.stderr) else "HELD",
            finding=None,
        )
    )
    shutil.rmtree(attackerA, ignore_errors=True)
    shutil.rmtree(attackerB, ignore_errors=True)

    victim_done.wait(timeout=60)
    th.join(timeout=60)
    vres = victim_res.get("res")
    rows.append(
        dict(
            name="cross-victim-undisturbed",
            expect="contained",
            note="the sleeping victim task finished normally despite "
            "concurrent attack containers (no interference)",
            exit_code=vres.exit_code if vres else None,
            timed_out=vres.timed_out if vres else None,
            stdout_tail=(vres.stdout if vres else "")[-200:],
            verdict="HELD" if vres and vres.exit_code == 0 else "FINDING",
            finding=None if vres and vres.exit_code == 0 else "victim disturbed",
        )
    )
    shutil.rmtree(victim, ignore_errors=True)
    return rows


# ---------------------------------------------------------------------------
# Task A — sequential
# ---------------------------------------------------------------------------


def run_sequential(out_dir: Path, timeout_s: int = 90) -> int:
    if not sb.docker_available():
        print("docker daemon not reachable — aborting")
        return 2
    workdir = out_dir / "repos"
    workdir.mkdir(parents=True, exist_ok=True)

    print("== Task A: adversarial attacks, sequential ==")
    rows: List[Dict] = []
    t_start = time.time()
    for atk in ATTACKS:
        print(f"  [{atk['name']}] expect={atk['expect']} ...", end=" ", flush=True)
        row = _run_one(atk, workdir, atk["name"], timeout_s)
        rows.append(row)
        print(
            f"{row['verdict']} exit={row.get('exit_code')} "
            f"t={row.get('elapsed_s')}s marker={row.get('marker_seen')}"
        )

    print("== Task A: cross-container interference scenarios ==")
    rows += _cross_scenarios(out_dir, timeout_s)
    for row in rows:
        if row["name"].startswith("cross-"):
            print(f"  [{row['name']}] {row['verdict']} marker={row.get('marker_seen')}")

    residue = _no_hexec_residue()
    if residue:
        rows.append(
            dict(
                name="container-residue",
                verdict="FINDING",
                finding=f"containers left: {residue}",
            )
        )

    findings = [r for r in rows if r["verdict"] == "FINDING"]
    design = [r for r in rows if r["verdict"] == "CONFIRMED-BY-DESIGN"]
    print(
        f"\nsequential: {len(rows)} attacks — "
        f"{len(rows) - len(findings) - len(design)} held, "
        f"{len(design)} confirmed-by-design, {len(findings)} FINDINGS, "
        f"{time.time() - t_start:.0f}s"
    )
    for r in findings:
        print(f"  FINDING {r['name']}: {r.get('finding')}")
    (out_dir / "adversarial_sequential.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8"
    )
    print(f"report: {out_dir / 'adversarial_sequential.json'}")
    return 1 if findings else 0


# ---------------------------------------------------------------------------
# Task B — concurrent adversarial load
# ---------------------------------------------------------------------------

CONCURRENT_ATTACKS: List[Dict] = [
    dict(
        name="fork-bomb",
        expect="killed",
        command="t0=$(date +%s); bomb() { bomb | bomb & }; bomb; wait; "
        'echo "bomb-survived-$(( $(date +%s) - t0 ))s"',
        check=lambda res: (
            "fork bomb survived to harness timeout — pids-limit did NOT bite"
            if res.timed_out
            else None
        ),
    ),
    dict(
        name="mem-bomb",
        expect="killed",
        py_files={"bomb.py": PY_MEM_BOMB},
        command="python bomb.py",
    ),
    dict(
        name="mem-leak",
        expect="killed",
        py_files={"leak.py": PY_MEM_LEAK},
        command="python leak.py",
    ),
    dict(
        name="disk-tmpfs",
        expect="killed",
        command="dd if=/dev/zero of=/tmp/fill bs=1M count=1024 2>/dev/null; "
        "sz=$(stat -c %s /tmp/fill 2>/dev/null || echo 0); "
        'echo "fill-size=$sz"; [ "$sz" -le 268435456 ] || echo FILL-OVER-CAP',
        check=lambda res: (
            "tmpfs cap bypassed: fill over 256MiB"
            if "FILL-OVER-CAP" in res.stdout
            else None
        ),
    ),
    dict(
        name="cpu-spin",
        expect="contained",
        command="t0=$(date +%s); end=$((t0+8)); "
        "while [ $(date +%s) -lt $end ]; do :; done; echo spin-ok",
    ),
    dict(
        name="net-probe",
        expect="blocked",
        py_files={"net.py": PY_NET},
        command="python net.py",
    ),
    dict(
        name="escape-attempt",
        expect="blocked",
        command=_bash("SU", "su root -c true")
        + "; "
        + _bash("MOUNT", "mkdir -p /tmp/m && mount -t proc none /tmp/m")
        + "; "
        + _bash("CHOWN", "touch /tmp/f && chown 0:0 /tmp/f")
        + "; true",
    ),
    dict(
        name="output-flood",
        expect="contained",
        command="yes 0123456789ABCDEF | head -c 52428800; echo; echo FLOOD-DONE",
        check=_check_output_size,
    ),
]


# Canary: a NORMAL, well-behaved task that must complete cleanly while
# surrounded by hostile neighbors (proves adversarial load doesn't break
# innocent tasks' isolation/limits).
def _canary_attack() -> Dict:
    return dict(
        name="canary-normal-task",
        expect="contained",
        note="well-behaved task amid adversarial load",
        command="echo canary-start && python -m pytest -q tests && echo canary-done",
    )


def _mk_canary_fixture(parent: Path, tag: str) -> Path:
    repo = _mk_fixture(parent, tag)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_add.py").write_text(
        "from mymod import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "def test_add2():\n    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )
    return repo


def run_concurrent(
    out_dir: Path, width: int = 8, rounds: int = 2, timeout_s: int = 90
) -> int:
    if not sb.docker_available():
        print("docker daemon not reachable — aborting")
        return 2
    workdir = out_dir / "repos-conc"
    workdir.mkdir(parents=True, exist_ok=True)

    # Pre-warm the fixture dep image: attacks measure attack behavior,
    # not first-build latency.
    warm = _mk_canary_fixture(workdir, "warm")
    print(f"warming fixture image: {sb._dep_image_tag(str(warm))} ...", flush=True)
    sb.ensure_image(str(warm))
    shutil.rmtree(warm, ignore_errors=True)

    print(
        f"== Task B: {width} hostile containers simultaneously, "
        f"{rounds} rounds (canaries interleaved) =="
    )
    rows: List[Dict] = []
    t0 = time.time()

    def job(i: int) -> Dict:
        # one canary per wave (i % width == 0), every attack otherwise —
        # the canary slots deliberately DISPLACE one attack instance per
        # wave, rotating which one so all attacks get exercised across
        # rounds (width 8, rounds 2: slots 0,8 canary; attacks 1..15 hit
        # all 8 corpus entries).
        if i % width == 0:
            atk = _canary_attack()
            repo = _mk_canary_fixture(workdir, f"c{i}")
        else:
            k = (i - 1 - (i // width)) % len(CONCURRENT_ATTACKS)
            atk = CONCURRENT_ATTACKS[k]
            repo = _mk_fixture(workdir, f"c{i}", atk.get("py_files"))
        start = time.time()
        try:
            res = sb.execute_sandboxed(str(repo), atk["command"], timeout_s)
        except Exception as exc:
            return dict(
                name=atk["name"],
                expect=atk["expect"],
                slot=i,
                exception=f"{type(exc).__name__}: {exc}"[:300],
                verdict="FINDING",
                finding="execute_sandboxed raised",
            )
        row = _verdict(atk, res)
        row.update(slot=i, elapsed_s=round(time.time() - start, 1))
        shutil.rmtree(repo, ignore_errors=True)
        return row

    n = width * rounds
    with ThreadPoolExecutor(max_workers=width) as pool:
        futs = [pool.submit(job, i) for i in range(n)]
        for fut in as_completed(futs):
            row = fut.result()
            rows.append(row)
            print(
                f"  [{row['name']} #{row.get('slot')}] {row['verdict']} "
                f"exit={row.get('exit_code')} t={row.get('elapsed_s')}s "
                f"marker={row.get('marker_seen')}"
            )

    residue = _no_hexec_residue()
    if residue:
        rows.append(
            dict(
                name="container-residue",
                verdict="FINDING",
                finding=f"containers left: {residue}",
            )
        )

    findings = [r for r in rows if r["verdict"] == "FINDING"]
    canaries = [r for r in rows if r["name"] == "canary-normal-task"]
    canary_ok = [r for r in canaries if r.get("exit_code") == 0]
    print(
        f"\nconcurrent: {len(rows)} runs at width {width} — "
        f"{len(rows) - len(findings)} held, {len(findings)} FINDINGS, "
        f"{time.time() - t0:.0f}s total"
    )
    print(
        f"canaries: {len(canary_ok)}/{len(canaries)} clean (exit 0, real pytest inside)"
    )
    for r in findings:
        print(f"  FINDING {r['name']}: {r.get('finding')}")
    if canaries and len(canary_ok) != len(canaries):
        print("  FINDING canary: well-behaved task disturbed by adversarial neighbors")
    (out_dir / "adversarial_concurrent.json").write_text(
        json.dumps(
            dict(width=width, rounds=rounds, rows=rows, residue=residue),
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"report: {out_dir / 'adversarial_concurrent.json'}")
    return 1 if findings else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m execution.sandbox_adversarial")
    ap.add_argument(
        "--concurrency",
        type=int,
        default=0,
        help="0 = Task A sequential; N>0 = Task B concurrent (width N)",
    )
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or Path("logs") / "sandbox-adversarial" / ts)
    out.mkdir(parents=True, exist_ok=True)
    if args.concurrency > 0:
        return run_concurrent(out, args.concurrency, args.rounds, args.timeout)
    return run_sequential(out, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
