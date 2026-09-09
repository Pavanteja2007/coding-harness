"""Sandboxed command execution in Docker (INTERFACES.md Boundary 1).

Contract: execute_sandboxed(repo_path, command, timeout_s) -> ExecutionResult.
The command runs in a FRESH container per call with:
- the repo bind-mounted READ-WRITE at /workspace (edits made inside must
  persist to the host — Terminal 1 diffs pristine/work dirs on the host
  after the agent's commands run here, so copy-in/copy-out would break it);
- CPU / memory / pid limits, read-only root filesystem, tmpfs /tmp,
- --cap-drop ALL and no-new-privileges;
- NO network by default (--network none); opt in per call with
  allow_network=True for tasks that legitimately need it.

Dependencies: the contract signature has no "setup" step, so each repo gets
a lazily-built dependency image (see ensure_image). The fingerprint hashes
only the repo's dependency manifests, so code edits never trigger rebuilds.
The image bakes pytest (needed by execution.verify) plus the repo's
requirements.txt. The repo's own package is NEVER pip-installed: an installed
copy would shadow the bind-mounted source and tests would exercise stale
code instead of the agent's edits. First call for a new repo fingerprint
therefore needs network (image build); later calls are offline-capable.

Fail-loud policy: if the docker daemon is unreachable this module raises
SandboxUnavailableError — it never silently falls back to running commands
directly on the host, because "sandboxed" must mean sandboxed. Callers that
want a no-Docker fallback should keep using harness._stubs.sandbox.

Windows host note: repo_path must live under a drive shared with Docker
Desktop (C:\\Users is shared by default). Mount paths use C:/... form.
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from shared.types import ExecutionResult

# ---------------------------------------------------------------------------
# Tunables (execution-internal constants; harness-facing knobs flow through
# the function signatures, per the "no hardcoded config" convention).
# ---------------------------------------------------------------------------

BASE_IMAGE = os.environ.get("HARNESS_SANDBOX_BASE_IMAGE", "python:3.10-slim")
IMAGE_PREFIX = "harness-exec"          # base tag: harness-exec:base
CONTAINER_PREFIX = "hexec"
MOUNT_POINT = "/workspace"             # repo root inside the container
MAX_OUTPUT_BYTES = 1_000_000           # per-stream capture cap fed back
DEFAULT_TIMEOUT_S = 120
DEFAULT_MEM_LIMIT = "1g"
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_PIDS_LIMIT = 512
BUILD_TIMEOUT_S = 1800

# Manifests whose contents determine a repo's dependency image.
DEP_MANIFESTS: List[str] = [
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "tox.ini",
    "environment.yml",
]

# Timeout exit code follows the GNU `timeout` convention (as does the stub).
TIMEOUT_EXIT_CODE = 124

# Orphan reaping (concurrency hardening): a container's name embeds its
# owning host PID (hexec-p<pid>-<uuid>); when a worker process is
# hard-killed (scheduler crash kills, stress-test fault injection), its
# `docker run` CLI dies but the container KEEPS RUNNING until its command
# finishes (--rm only reaps on exit). Under 40-50 concurrent tasks those
# zombies eat the Docker VM's memory/CPU budget for the full command
# duration. Surviving callers opportunistically reap: at most once per
# REAP_INTERVAL_S, list running hexec-* containers whose owner PID is
# dead and kill them (the daemon's --rm then removes them).
REAP_INTERVAL_S = 30.0

# Cross-process image build lock: the scheduler spawns one worker process
# per task, so the in-process _image_lock does not stop N workers from
# racing the SAME cold image build (duplicate builds burn network + VM
# resources; each takes minutes). A lockfile in temp serializes builders
# process-wide; the winner builds, losers wait then hit the image-cache
# probe. Stale locks (killed builders) expire after LOCK_STALE_S.
_BUILD_LOCK_STALE_S = 1800  # >= BUILD_TIMEOUT_S so a live builder never
#                     gets its lock stolen mid-build


class SandboxUnavailableError(RuntimeError):
    """Docker is unusable (missing CLI or unreachable daemon).

    Raised instead of ever silently running a command outside the sandbox.
    """


# ---------------------------------------------------------------------------
# Host <-> container path handling (Windows-aware)
# ---------------------------------------------------------------------------

def _win_to_docker(path: str) -> str:
    """Convert an absolute host path to a form the docker CLI accepts.

    Windows accepts C:/Users/... (forward slashes, drive letter); POSIX
    paths pass through unchanged. Assumes path is absolute.
    """
    if os.name != "nt":
        return path
    p = str(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return p
    return p  # UNC paths bind as-is under Docker Desktop


def _fingerprint(parts: List[str]) -> str:
    """Stable short hash of the given strings (image tag component)."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Docker plumbing
# ---------------------------------------------------------------------------

_docker_ok: Optional[bool] = None
_image_lock = threading.Lock()


def docker_available() -> bool:
    """True if the docker daemon is reachable (cached per process).

    Used by tests to gate integration tests and by callers deciding whether
    the real sandbox can be used at all. Never raises.
    """
    global _docker_ok
    if _docker_ok is not None:
        return _docker_ok
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        )
        _docker_ok = cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        _docker_ok = False
    return _docker_ok


def _run_docker(args: List[str], timeout_s: Optional[int] = None,
                check: bool = True) -> "subprocess.CompletedProcess[str]":
    """Run one docker CLI command (args excludes the leading 'docker').

    Raises SandboxUnavailableError if docker itself cannot run; RuntimeError
    on command failure when check=True. Output is decoded leniently.
    """
    try:
        cp = subprocess.run(
            ["docker"] + args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"docker {' '.join(args[:2])} timed out") from exc
    except OSError as exc:
        raise SandboxUnavailableError(f"docker CLI not runnable: {exc}") from exc
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args[:3])} failed (exit {cp.returncode}): "
            f"{(cp.stderr or cp.stdout)[:2000]}"
        )
    return cp


def _dep_manifest_paths(repo_path: str) -> List[Path]:
    """Existing dependency manifests at the repo root, canonical order."""
    out: List[Path] = []
    for name in DEP_MANIFESTS:
        p = Path(repo_path, name)
        if p.is_file():
            out.append(p)
    return out


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def base_image_tag() -> str:
    """Tag of the harness base image (python:3.10-slim + pytest)."""
    return f"{IMAGE_PREFIX}:base"


def _dep_image_tag(repo_path: str) -> str:
    """Per-repo image tag: prefix + hash of base tag and dep manifests."""
    parts: List[str] = [base_image_tag()]
    for p in _dep_manifest_paths(repo_path):
        parts.append(p.name + ":" + _read_text(p))
    return f"{IMAGE_PREFIX}:{_fingerprint(parts)}"


def _base_dockerfile_text() -> str:
    """Dockerfile for the one-time harness base image (pytest + tomli)."""
    return (
        f"FROM {BASE_IMAGE}\n"
        "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\n"
        "RUN pip install --no-cache-dir --disable-pip-version-check pytest tomli\n"
        f"WORKDIR {MOUNT_POINT}\n"
    )


def _repo_dockerfile_text(tag: str) -> str:
    """Dockerfile for a repo's dependency layer (FROM the harness base).

    Static: the context always contains harness-deps.txt (requirements.txt
    content, may be empty), pyproject.toml (may be empty), and the extractor
    script. The extractor merges requirements + pyproject [project]
    dependencies into /tmp/all-deps.txt; pip installs only if it is non-empty.
    Richer flows (poetry.lock, conda) need an explicit image build by the
    caller — see execution/AGENTS.md.
    """
    return "\n".join([
        f"FROM {base_image_tag()}",
        f"LABEL harness.dep-image=\"{tag}\"",
        "COPY harness-deps.txt /tmp/harness-deps.txt",
        "COPY pyproject.toml /tmp/pyproject.toml",
        "COPY _extract_pyproject_deps.py /tmp/_extract.py",
        "RUN python /tmp/_extract.py > /tmp/all-deps.txt",
        "RUN sh -c 'if [ -s /tmp/all-deps.txt ]; then "
        "pip install --no-cache-dir --disable-pip-version-check "
        "-r /tmp/all-deps.txt; fi'",
        "",
    ])


_EXTRACT_PYPROJECT_DEPS = r'''
"""
Baked into the build context by execution/sandbox.py.

Reads /tmp/harness-deps.txt (requirements.txt content or "") and
/tmp/pyproject.toml, prints a merged dependency list: one PEP 508 string
per line. Runs INSIDE the build container during ensure_image(); this
script is never imported from the host environment.
"""
import os
import re

out = []
for line in open("/tmp/harness-deps.txt", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#"):
        out.append(line)

tomllib = None
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # py3.10 base image ships tomli
    except ModuleNotFoundError:
        tomllib = None

if tomllib is not None and os.path.exists("/tmp/pyproject.toml"):
    with open("/tmp/pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project") or {}
    for dep in project.get("dependencies") or []:
        dep = dep.strip()
        if dep:
            out.append(dep)
    for group in (project.get("optional-dependencies") or {}).values():
        for dep in group:
            dep = dep.strip()
            if dep:
                out.append(dep)

elif tomllib is None and os.path.exists("/tmp/pyproject.toml"):
    # No TOML parser available: regex fallback for PEP 621 [project] tables.
    # Quote characters are written as \x22/\x27 so that no quote literal
    # appears in the regex source (this file is generated by a Python string
    # template in sandbox.py — direct quotes there corrupt the regex).
    # Poetry sections are deliberately ignored (see AGENTS.md fallback).
    text = open("/tmp/pyproject.toml", encoding="utf-8").read()
    m = re.search(r"^\[project\][^\[]*?dependencies\s*=\s*\[(.*?)\]",
                  text, re.DOTALL | re.MULTILINE)
    if m:
        for dep in re.findall(r"[\x22\x27]([^\x22\x27]+)[\x22\x27]", m.group(1)):
            dep = dep.strip()
            if dep:
                out.append(dep)

seen = set()
for dep in out:
    if dep not in seen:
        seen.add(dep)
        print(dep)
'''


_ensure_base_lock = threading.Lock()
_base_done = False


def _ensure_base_image() -> str:
    """Build harness-exec:base if missing. Assumes docker is available.

    Process-safe: concurrent worker processes race the same cold build
    too — guarded by a cross-process lockfile (see _CrossProcLock).
    """
    global _base_done
    tag = base_image_tag()
    if _base_done:
        return tag
    with _ensure_base_lock:
        if _base_done:
            return tag
        probe = _run_docker(["image", "inspect", tag], check=False)
        if probe.returncode != 0:
            with _CrossProcLock("hexec-base-build") as acquired:
                if acquired:
                    probe = _run_docker(["image", "inspect", tag], check=False)
                    if probe.returncode != 0:
                        with tempfile.TemporaryDirectory(prefix="hexec-base-") as ctx:
                            (Path(ctx) / "Dockerfile").write_text(
                                _base_dockerfile_text(), encoding="utf-8")
                            _run_docker(["build", "-t", tag, ctx],
                                        timeout_s=BUILD_TIMEOUT_S)
        _base_done = True
    return tag


class _CrossProcLock:
    """Advisory cross-process file lock with stale-lock expiry.

    O_EXCL create is atomic on all hosts; the holder writes its PID so
    a lock left by a killed builder can be stolen after LOCK_STALE_S.
    Context manager yields True when the lock is HELD; False means a
    live peer holds it — the caller should just proceed to the cache
    probe (the peer is building the same image).
    """

    def __init__(self, name: str) -> None:
        self.path = Path(tempfile.gettempdir()) / f"{name}.lock"
        self._held = False
        self._peer_pid: Optional[int] = None

    def holder_alive(self) -> bool:
        """True if the process currently holding the lock is alive.

        Only meaningful after a failed acquire (the context manager
        returned False) — reads the holder PID recorded at that point.
        Never raises; a vanished lock means the holder is gone.
        """
        pid = self._peer_pid
        if pid is None:
            return False
        return _pid_alive(pid)

    def __enter__(self) -> bool:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._held = True
                return True
            except FileExistsError:
                self._peer_pid = self._read_holder_pid()
                if self._steal_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    return False  # a live peer owns the build; let them
                time.sleep(0.25)

    def _read_holder_pid(self) -> Optional[int]:
        try:
            return int(self.path.read_text(encoding="utf-8",
                                           errors="replace").strip() or 0)
        except (OSError, ValueError):
            return None

    def _steal_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
            pid_txt = self.path.read_text(encoding="utf-8", errors="replace")
            pid = int(pid_txt.strip() or 0)
            if age > _BUILD_LOCK_STALE_S or (pid > 0 and not _pid_alive(pid)):
                self.path.unlink(missing_ok=True)
                return True
        except (OSError, ValueError):
            return False
        return False

    def __exit__(self, *exc) -> None:
        if self._held:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def ensure_image(repo_path: str, rebuild: bool = False) -> str:
    """Build (or reuse) the per-repo dependency image; returns its tag.

    Assumes repo_path is a readable directory and docker is available.
    Idempotent: an existing matching image is reused unless rebuild=True.
    Thread-safe in-process (_image_lock) AND process-safe (a lockfile
    serializes scheduler-spawned workers racing the same cold image:
    one builds, the rest wait briefly then hit the image-cache probe).
    """
    with _image_lock:
        _ensure_base_image()
        tag = _dep_image_tag(repo_path)
        if not rebuild:
            probe = _run_docker(["image", "inspect", tag], check=False)
            if probe.returncode == 0:
                return tag
        lock = _CrossProcLock(f"hexec-img-{tag.split(':')[-1]}")
        with lock as acquired:
            if acquired:
                # won the cross-process build slot (or the lock was
                # busy-waited out): re-probe, someone may have finished.
                probe = _run_docker(["image", "inspect", tag], check=False)
                if probe.returncode == 0 and not rebuild:
                    return tag
            else:
                # A live peer is building this exact image right now.
                # Poll the image cache (peer success) AND the lock holder
                # (peer death — a killed builder's lock goes stale and
                # its PID stops answering) so a failed peer never costs
                # the full wait. As a last resort, build it ourselves.
                deadline = time.monotonic() + BUILD_TIMEOUT_S + 60
                while time.monotonic() < deadline:
                    time.sleep(2.0)
                    probe = _run_docker(["image", "inspect", tag], check=False)
                    if probe.returncode == 0:
                        return tag
                    if not lock.holder_alive():
                        break  # peer died: take over the build now
            req_path = Path(repo_path, "requirements.txt")
            req_text = _read_text(req_path) if req_path.is_file() else ""
            pyproject_path = Path(repo_path, "pyproject.toml")
            pyproject_text = (_read_text(pyproject_path)
                              if pyproject_path.is_file() else "")
            with tempfile.TemporaryDirectory(prefix="hexec-build-") as ctx:
                ctx_path = Path(ctx)
                (ctx_path / "harness-deps.txt").write_text(req_text, encoding="utf-8")
                (ctx_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
                (ctx_path / "_extract_pyproject_deps.py").write_text(
                    _EXTRACT_PYPROJECT_DEPS, encoding="utf-8")
                (ctx_path / "Dockerfile").write_text(
                    _repo_dockerfile_text(tag), encoding="utf-8")
                _run_docker(["build", "-t", tag, ctx], timeout_s=BUILD_TIMEOUT_S)
            return tag


# ---------------------------------------------------------------------------
# Container execution
# ---------------------------------------------------------------------------

def _docker_run_args(
    image: str,
    repo_path: str,
    timeout_s: int,
    allow_network: bool,
    env: Optional[Dict[str, str]],
    mem_limit: str,
    cpu_limit: float,
    pids_limit: int,
) -> List[str]:
    """Assemble the `docker run` argv prefix (before the shell command).

    Pure function (unit-testable without a daemon): the same inputs always
    produce the same flags. repo_path is NOT resolved here — pass the
    already-normalized absolute path.
    """
    mount = f"{_win_to_docker(repo_path)}:{MOUNT_POINT}"
    args: List[str] = [
        "run",
        "--rm",
        "--name", f"{_container_name_prefix()}-{uuid.uuid4().hex[:12]}",
        "--pull=never",
        "--volume", mount,
        "--workdir", MOUNT_POINT,
        "--user", _container_user(),
        "--memory", mem_limit,
        "--memory-swap", mem_limit,       # equal to --memory => no swap
        "--cpus", str(cpu_limit),
        "--pids-limit", str(pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=256m",
        "--env", "HOME=/tmp",
    ]
    if not allow_network:
        args += ["--network", "none"]
    if env:
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
    args.append(image)
    return args


def _container_name_prefix() -> str:
    """Container-name prefix for THIS process's containers.

    Embeds the owner PID so reaping can distinguish live owners from dead
    ones: hexec-p<pid>-<uuid> for this process's own containers. On
    non-POSIX hosts os.getpid() works too; the prefix just needs to be
    unique per process and carry the PID as its second dash-separated
    token (kept plain — no uuid4 hex can look like a small int).
    """
    return f"{CONTAINER_PREFIX}-p{os.getpid()}"


def _container_pid_from_name(name: str) -> Optional[int]:
    """Owner PID encoded in an hexec-* container name, if any.

    Old-format names (hexec-<uuid>, from before PID embedding) return
    None — we can't prove their owner dead, so we never reap them.
    """
    m = re.match(rf"^{CONTAINER_PREFIX}-p(\d+)(?:-|$)", name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _pid_alive(pid: int) -> bool:
    """True iff a host process with this PID exists AND is running.

    On Windows, OpenProcess succeeds even for a terminated process whose
    object is still referenced (e.g. a zombie `docker run` CLI child
    keeps its dead parent's handle table alive briefly) — so the exit
    code is the actual liveness signal: STILL_ACTIVE (259) means live,
    anything else means dead. POSIX uses the classic kill(pid, 0) probe.
    Never raises for pid<=0.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, ImportError):
        return False


def reap_orphaned_containers(
    include_stale_names: bool = False,
    dry_run: bool = False,
) -> List[str]:
    """Kill hexec-* containers whose owning host process is dead.

    A hard-killed worker (TerminateProcess / SIGKILL) cannot clean up
    after itself: its container runs until the command finishes and only
    then is --rm reaped. This function lets SURVIVING processes collect
    those zombies: every running hexec-* container whose name-embedded
    owner PID no longer exists is killed (the daemon removes it thanks
    to --rm).

    Assumes docker is available (caller in execute_sandboxed already
    checked). include_stale_names=True also kills RUNNING containers
    with the old pre-PID name format — only use that when no other
    harness processes could legitimately own one. Returns the names of
    containers killed (empty when none or dry_run=True). Never raises:
    reaping is best-effort self-healing, not a correctness gate.
    """
    names: List[str] = []
    try:
        ls = _run_docker(
            ["ps", "--filter", f"name={CONTAINER_PREFIX}-", "--format", "{{.Names}}"],
            check=False, timeout_s=30,
        )
        if ls.returncode != 0:
            return names
        for name in ls.stdout.split():
            pid = _container_pid_from_name(name)
            if pid is None:
                if include_stale_names:
                    names.append(name)
                continue  # old-format name: owner unknowable
            if not _pid_alive(pid):
                names.append(name)
        if dry_run or not names:
            return names
        for name in names:
            _run_docker(["kill", name], check=False, timeout_s=30)
    except Exception:  # best-effort: never break a real execution
        pass
    return names


_last_reap_ts = 0.0
_reap_lock = threading.Lock()


def _maybe_reap() -> None:
    """Rate-limited opportunistic reap from inside execute_sandboxed.

    At most one reap pass per process per REAP_INTERVAL_S: under heavy
    concurrency (40-50 simultaneous containers) a per-call docker ps
    sweep would add measurable overhead and N-fold daemon load — the
    sweep only needs SOMEONE to run it within a bounded window.
    Never raises.
    """
    global _last_reap_ts
    try:
        if not _reap_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if now - _last_reap_ts < REAP_INTERVAL_S:
                return
            _last_reap_ts = now
        finally:
            _reap_lock.release()
        reap_orphaned_containers()
    except Exception:
        pass


def _container_user() -> str:
    """Container uid:gid. Docker Desktop (Win/Mac) mounts host dirs as
    writable by any container uid, so 1000 is safe there; on Linux hosts we
    match the current user so bind-mount writes keep working. Override with
    HARNESS_SANDBOX_UID."""
    forced = os.environ.get("HARNESS_SANDBOX_UID")
    if forced:
        return forced
    if os.name == "nt":
        return "1000:1000"
    return f"{os.getuid()}:{os.getgid()}"


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Keep head and tail with an omission marker (mirrors tools.truncate)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n[... {omitted} chars omitted ...]\n" + text[-tail:]


def execute_sandboxed(
    repo_path: str,
    command: str,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    *,
    allow_network: bool = False,
    env: Optional[Dict[str, str]] = None,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    cpu_limit: float = DEFAULT_CPU_LIMIT,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
) -> ExecutionResult:
    """Run `command` in a fresh sandboxed container with cwd=repo root.

    Assumes repo_path is an absolute path to an existing directory that
    Docker can bind-mount (under a Docker-Desktop-shared drive on Windows),
    and command is a single shell command line (bash syntax; it runs via
    `bash -c`). The repo is mounted READ-WRITE at /workspace so file edits
    persist to the host. Dependencies come from the repo's image (built on
    first use — needs network once; see module docstring).

    Keyword-only extras beyond the INTERFACES.md contract (all default to
    safe values, so contract callers are unaffected): allow_network drops
    --network none for this call; env adds container env vars; mem/cpu/pids
    limits override the defaults.

    Returns ExecutionResult; timeout yields exit_code=124 and timed_out=True
    (GNU timeout convention, matching the local stub). Raises
    SandboxUnavailableError if docker is unusable — never silently runs
    unsandboxed.
    """
    if not Path(repo_path).is_dir():
        raise FileNotFoundError(f"repo_path is not a directory: {repo_path}")
    if not docker_available():
        raise SandboxUnavailableError(
            "docker daemon not reachable; refusing to run unsandboxed"
        )
    repo_abs = os.path.abspath(repo_path)
    image = ensure_image(repo_abs)
    _maybe_reap()

    full_argv = (
        ["docker"]
        + _docker_run_args(
            image, repo_abs, timeout_s, allow_network, env,
            mem_limit, cpu_limit, pids_limit,
        )
        + ["bash", "-c", command or "true"]
    )

    proc = subprocess.Popen(
        full_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=_truncate(stdout or ""),
            stderr=_truncate(stderr or ""),
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        # Killing the docker CLI does not stop the container — kill it by
        # name (the --rm flag then removes it).
        name = _container_name(full_argv)
        _run_docker(["kill", name], check=False, timeout_s=30)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - CLI gone
            stdout, stderr = "", ""
        return ExecutionResult(
            exit_code=TIMEOUT_EXIT_CODE,
            stdout=_truncate(stdout or ""),
            stderr=_truncate((stderr or "") + f"\n[sandbox] timed out after {timeout_s}s"),
            timed_out=True,
        )


def _container_name(argv: List[str]) -> str:
    """Extract the --name value from a docker run argv (for killing)."""
    for i, token in enumerate(argv):
        if token == "--name" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


# ---------------------------------------------------------------------------
# Debug CLI: python -m execution.sandbox --repo . echo hi
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m execution.sandbox",
        description="Run one command inside the harness Docker sandbox.",
    )
    parser.add_argument("--repo", required=True, help="repo directory to mount")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--network", action="store_true",
                        help="allow network for this command")
    parser.add_argument("command", help="bash command line to run")
    args = parser.parse_args()
    try:
        res = execute_sandboxed(
            args.repo, args.command, args.timeout, allow_network=args.network
        )
    except (SandboxUnavailableError, RuntimeError, FileNotFoundError) as exc:
        print(f"sandbox error: {exc}", file=sys.stderr)
        return 2
    print(f"exit={res.exit_code} timed_out={res.timed_out}")
    print("--- stdout ---")
    print(res.stdout, end="")
    print("--- stderr ---")
    print(res.stderr, end="")
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(_main())
