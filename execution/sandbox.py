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
PYTHON repos: the image bakes pytest (needed by execution.verify) plus the
repo's requirements.txt — the repo's own package is NEVER pip-installed (an
installed copy would shadow the bind-mounted source and tests would
exercise stale code instead of the agent's edits). JS/TS repos (package.json
without Python markers): a node:22-slim-based image installs the repo's
npm deps at BUILD time under /opt/deps/node_modules; at run time that tree
is mounted read-only over /workspace/node_modules (a named docker volume,
populated once per image — see _js_deps_volume) so tests resolve deps
exactly as in a normal checkout while the source under test stays the
bind-mounted repo. First call for a new repo fingerprint therefore needs
network (image build); later calls are offline-capable.

Fail-loud policy: if the docker daemon is unreachable this module raises
SandboxUnavailableError — it never silently falls back to running commands
directly on the host, because "sandboxed" must mean sandboxed. Callers that
want a no-Docker fallback should keep using harness._stubs.sandbox.

Windows host note: repo_path must live under a drive shared with Docker
Desktop (C:\\Users is shared by default). Mount paths use C:/... form.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from execution.ingress import REDACTION_UNAVAILABLE, cap_for, seal_streams
from shared.types import ExecutionResult

# ---------------------------------------------------------------------------
# Tunables (execution-internal constants; harness-facing knobs flow through
# the function signatures, per the "no hardcoded config" convention).
# ---------------------------------------------------------------------------

BASE_IMAGE = os.environ.get("HARNESS_SANDBOX_BASE_IMAGE", "python:3.10-slim")
BASE_IMAGE_NODE = os.environ.get("HARNESS_SANDBOX_BASE_IMAGE_NODE", "node:22-slim")
IMAGE_PREFIX = "harness-exec"  # base tags: harness-exec:base / :node-base
CONTAINER_PREFIX = "hexec"
MOUNT_POINT = "/workspace"  # repo root inside the container
MAX_OUTPUT_BYTES = 1_000_000  # per-stream capture cap fed back
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

# JS/TS dependency manifests (npm ecosystem). The fingerprint includes
# BOTH package.json (runner + dep specs) and the lockfile (exact versions)
# so any dependency change rebuilds the image.
JS_DEP_MANIFESTS: List[str] = [
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
]

# Timeout exit code follows the GNU `timeout` convention (as does the stub).
TIMEOUT_EXIT_CODE = 124

# Orphan reaping (concurrency hardening): a container's name embeds its
# owning host PID and an environment token (hexec-p<pid>-e<env8>-<uuid>);
# when a worker process is hard-killed (scheduler crash kills, stress-
# test fault injection), its `docker run` CLI dies but the container
# KEEPS RUNNING until its command finishes (--rm only reaps on exit).
# Under 40-50 concurrent tasks those zombies eat the Docker VM's
# memory/CPU budget for the full command duration. Surviving callers
# opportunistically reap: at most once per REAP_INTERVAL_S, list running
# hexec-* containers whose owner PID is dead and kill them (the
# daemon's --rm then removes them).
#
# The env token (Round 7) scopes reaping to the owner's OS environment:
# with Docker Desktop, Windows-host and WSL-host harness processes share
# ONE daemon but have SEPARATE, unrelated PID spaces — a Windows-side
# sweep probing a WSL-owner PID always sees "dead" (no such Windows pid)
# and would SIGKILL a live WSL-owned container mid-run (reproduced
# live: Windows reaper killed a WSL-owned sleep-120 container seconds
# in; this is also what flaked the Linux CI-parity suite runs). Names
# without a recognizable env token of OUR OWN environment (old
# hexec-p<pid>-<uuid> names from Round 3, or another environment's
# names) are NEVER reaped: owner liveness is simply unknowable from a
# foreign PID space.
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


class SandboxDependencyError(RuntimeError):
    """A sandbox dependency image or runtime dependency layer failed."""


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
# Supply-chain pinning + pre-pull (Prompt 13, "Egress and sandbox")
#
# Run-time already forbids surprise pulls with `--pull=never`, which means a
# container can only ever run an image that is already on the host. That is
# necessary but not sufficient: a mutable local tag can be re-pointed at a
# different image between runs, so "the tag existed" is not an artifact
# identity. These helpers resolve the tag to an immutable content digest and
# record it, which is what a run receipt needs in order to say which artifact
# source produced a verified result.
# ---------------------------------------------------------------------------

#: Host directories that must never be bind-mounted into a sandbox container.
#: These are the shapes an attacker reaches for to read credentials off the
#: host: the Docker socket, SSH agent sockets, cloud CLI config (which holds
#: long-lived tokens), and the user's dotfiles/keystore directories.
FORBIDDEN_MOUNT_SUBSTRINGS: Tuple[str, ...] = (
    "docker.sock",
    "dockerdesktop",
    "containerd.sock",
    "podman.sock",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    "id_rsa",
    "credentials",
    ".azure",
    ".config/gcloud",
    "secrets",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
)

#: Socket paths that must not exist inside a sandbox container. A container
#: that can reach the Docker socket can start a privileged sibling and own the
#: host, so this is checked statically on the argv we are about to spawn.
FORBIDDEN_CONTAINER_SOCKETS: Tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/docker/",
    "/run/containerd/",
    "/run/podman/",
    "/var/run/dockershim.sock",
    "/var/run/crio/",
)


def image_digest(image: str) -> str:
    """Return the immutable content digest for a local image tag.

    Returns ``""`` when the daemon cannot be reached or the tag is missing,
    so a caller that treats the digest as required decides what to do rather
    than getting an exception from a helper it called for telemetry. A tag
    already spelled ``name@sha256:...`` is returned unchanged.
    """
    reference = str(image or "").strip()
    if not reference:
        return ""
    if "@sha256:" in reference:
        return reference.split("@", 1)[1]
    try:
        probe = _run_docker(
            [
                "image",
                "inspect",
                "--format",
                "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}",
                reference,
            ],
            check=False,
            timeout_s=60,
        )
    except (SandboxUnavailableError, RuntimeError, OSError):
        return ""
    if probe.returncode != 0:
        return ""
    value = (probe.stdout or probe.stderr or "").strip().splitlines()
    return value[0].strip() if value else ""


def pin_image(image: str, *, required: bool = False) -> str:
    """Return ``name@sha256:...`` for an image, pulling it first if needed.

    A tag that already carries a digest is returned unchanged. Otherwise the
    digest is resolved locally; when the image is absent, a single pre-pull is
    attempted so the pinned reference exists before the run. With
    ``required=True`` an unresolvable image raises rather than returning a
    mutable tag — that is the release-gate shape, where an unpinned artifact
    source must fail the build instead of quietly running.
    """
    reference = str(image or "").strip()
    if not reference:
        if required:
            raise SandboxDependencyError("no sandbox image was supplied to pin")
        return ""
    if "@sha256:" in reference:
        return reference
    digest = image_digest(reference)
    if not digest:
        try:
            pull = _run_docker(["pull", reference], check=False, timeout_s=900)
        except (SandboxUnavailableError, RuntimeError, OSError):
            pull = None
        if pull is not None and pull.returncode == 0:
            digest = image_digest(reference)
    if not digest:
        if required:
            raise SandboxDependencyError(
                f"sandbox image {reference!r} has no resolvable digest; the "
                "artifact source cannot be pinned"
            )
        return reference
    bare = digest.split("@", 1)[-1]
    if bare.startswith("sha256:"):
        return f"{reference}@{bare}"
    return f"{reference}@{bare}"


def prepull_base_image(kind: str = "python") -> str:
    """Pre-pull the sandbox base image and return its pinned reference.

    Best-effort by design: the run path already uses ``--pull=never``, so a
    pre-pull is a latency/availability optimization, not a correctness
    requirement. It never raises — a registry outage must not turn into a
    task failure, and the existing lazy build path still works.
    """
    reference = (
        node_base_image_tag() if str(kind).casefold() == "js" else base_image_tag()
    )
    try:
        _run_docker(["pull", reference], check=False, timeout_s=900)
    except (SandboxUnavailableError, RuntimeError, OSError):
        return ""
    return pin_image(reference)


def _split_volume_spec(spec: str) -> Tuple[str, str, str]:
    """Split a docker volume spec into (source, target, mode).

    A Windows source carries its own drive colon (``C:/repo:/workspace``), so
    splitting on the FIRST colon would yield ``C`` and silently compare a
    drive letter against the allowlist. Split on the LAST colon instead, after
    peeling a trailing ``:ro``/``:rw`` mode.
    """
    text = str(spec or "")
    mode = ""
    match = re.search(r":(ro|rw)$", text, re.IGNORECASE)
    if match:
        mode = match.group(1).lower()
        text = text[: match.start()]
    if ":" not in text:
        return text, "", mode
    source, _, target = text.rpartition(":")
    return source, target, mode


def assert_sandbox_argv_isolated(
    args: List[str], repo_path: str, *, writable_paths: Sequence[str] = ()
) -> None:
    """Fail closed when a `docker run` argv would breach the sandbox policy.

    This is a pre-spawn static check, not a runtime probe: it refuses the
    invocation before the daemon ever sees it, so a future refactor that
    accidentally adds a mount, a capability, or a privilege flag fails here
    instead of shipping a container that can read the host. Checks:

    * no runtime socket mount (Docker/containerd/Podman/CRIO);
    * no host secret-directory mount, beyond the single repository mount and
      the harness's own named ``hexec-*`` dependency volumes;
    * no ``--privileged``, no added capabilities or security options, and no
      host PID/IPC/network/user namespace;
    * every mount of a path INSIDE the repository declares its mode
      explicitly, and a read-write one is confined to a path the containment
      policy declared writable.

    The last rule is what lets a writable root carry read-only subtrees. A
    nested mount has to say which it is, because ``docker run -v src:dst``
    defaults to read-WRITE: without the explicit-mode rule a containment
    overlay that lost its ``:ro`` would silently widen the container instead
    of narrowing it, and the pre-spawn gate -- the thing that is supposed to
    catch exactly that -- would have been the reason it shipped.

    ``writable_paths`` are repository-relative (or absolute) paths the
    containment policy declared writable. It is empty by default, so a
    caller that has not declared any gets the stricter rule for free.

    Raises :class:`SandboxDependencyError` (a fail-loud security refusal, not
    a warning) when any check fails.
    """
    if not args:
        raise SandboxDependencyError("empty docker argv refused by the isolation gate")
    allowed_mount = _win_to_docker(str(repo_path or "")).rstrip("/")
    allowed_mounts = {allowed_mount.casefold()} if allowed_mount else set()

    def _folded_path(value: Any) -> str:
        """Return a comparable form of a host path, absolute or workspace-relative."""
        text = _win_to_docker(str(value or "")).rstrip("/").casefold()
        if not text:
            return ""
        if not allowed_mount:
            return text
        absolute = text.startswith("/") or bool(re.match(r"^[a-z]:/", text))
        return text if absolute else f"{allowed_mount.casefold()}/{text}"

    declared_writable = {
        folded
        for folded in (_folded_path(item) for item in (writable_paths or ()))
        if folded
    }
    for index, token in enumerate(args):
        lowered = str(token).lower()
        if lowered in ("--privileged", "--pid=host", "--ipc=host", "--userns=host"):
            raise SandboxDependencyError(f"sandbox isolation gate refused {token!r}")
        if (
            lowered in ("--network", "--pid")
            and index + 1 < len(args)
            and str(args[index + 1]).casefold() == "host"
        ):
            raise SandboxDependencyError(
                f"sandbox isolation gate refused the host "
                f"{lowered.lstrip('-')} namespace"
            )
        if lowered == "--cap-add":
            raise SandboxDependencyError(
                "sandbox isolation gate refused --cap-add; capabilities are "
                "fixed by the sandbox policy"
            )
        if lowered == "--userns":
            raise SandboxDependencyError(
                "sandbox isolation gate refused --userns; the user namespace "
                "is fixed by the sandbox policy"
            )
        if lowered == "--security-opt":
            # Exactly one security option is part of the baseline policy
            # (no-new-privileges). Anything else could relax a default, so it
            # is refused rather than pattern-matched.
            value = str(args[index + 1]).casefold() if index + 1 < len(args) else ""
            if value not in ("no-new-privileges:true", "no-new-privileges"):
                raise SandboxDependencyError(
                    f"sandbox isolation gate refused the security option {value!r}"
                )
        if lowered in ("--volume", "-v"):
            if index + 1 >= len(args):
                raise SandboxDependencyError(
                    "sandbox isolation gate refused a mount with no target"
                )
            spec = str(args[index + 1])
            source, target, mode = _split_volume_spec(spec)
            if not target:
                raise SandboxDependencyError(
                    f"sandbox isolation gate refused a mount with no target: {spec!r}"
                )
            if not source.startswith(CONTAINER_PREFIX) and not re.search(
                r"[\\/]", source
            ):
                raise SandboxDependencyError(
                    f"sandbox isolation gate refused a relative mount {spec!r}"
                )
            lowered_spec = _win_to_docker(spec).casefold()
            # Name the specific violation first: "you mounted the Docker
            # socket" is actionable, "you mounted outside the workspace" is
            # only a symptom of it.
            for forbidden in FORBIDDEN_CONTAINER_SOCKETS:
                if forbidden in lowered_spec:
                    raise SandboxDependencyError(
                        f"sandbox isolation gate refused a runtime socket mount: {spec!r}"
                    )
            for forbidden in FORBIDDEN_MOUNT_SUBSTRINGS:
                if forbidden in lowered_spec:
                    raise SandboxDependencyError(
                        f"sandbox isolation gate refused a sensitive mount: {spec!r}"
                    )
            folded = _win_to_docker(source).rstrip("/").casefold()
            inside_workspace = any(
                folded == base or folded.startswith(base + "/")
                for base in allowed_mounts
            )
            if inside_workspace and folded not in allowed_mounts:
                # A containment sub-mount (read-only `.git` inside the
                # writable workspace, or a declared writable root). It
                # already reaches nothing new -- it is inside the mount that
                # is already permitted -- so the rule here is about the MODE,
                # not about the location.
                if mode not in ("ro", "rw"):
                    raise SandboxDependencyError(
                        "sandbox isolation gate refused an in-workspace mount "
                        f"with no explicit mode (docker defaults to rw): {spec!r}"
                    )
                if mode == "rw" and not any(
                    folded == allowed or folded.startswith(allowed + "/")
                    for allowed in declared_writable
                ):
                    raise SandboxDependencyError(
                        "sandbox isolation gate refused a read-write in-workspace "
                        f"mount outside every declared writable root: {spec!r}"
                    )
                continue
            if folded not in allowed_mounts and not source.startswith(CONTAINER_PREFIX):
                raise SandboxDependencyError(
                    "sandbox isolation gate refused a host mount outside the "
                    f"workspace: {spec!r}"
                )
    return None


def declared_egress_allowlist() -> Tuple[str, ...]:
    """Return the operator-declared egress allowlist for networked containers.

    Container egress is a network-namespace decision, so this value is a
    DECLARATION recorded on every networked sandbox call, not an enforcement
    point: the host-side fetch path (harness.webfetch) is where the
    deny-by-default allowlist is actually enforced. Recording the declaration
    keeps "which hosts was this task permitted to reach" answerable from the
    trace, and an empty declaration is itself the signal that no operator
    allowlist was configured.
    """
    from shared.egress import EgressPolicy

    raw = os.environ.get("NEO_EGRESS_ALLOWED_HOSTS", "") or ""
    entries = [item for item in raw.replace(",", " ").split() if item]
    if not entries:
        return ()
    policy = EgressPolicy.build(hosts=entries, source="env")
    return tuple(policy.allowed_hosts)


def sandbox_network_availability(
    allow_network: bool = False,
    *,
    config: Optional[Dict[str, Any]] = None,
    declared_hosts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return the honest answer to "does this container have usable network?".

    Additive receipt, computed through :mod:`shared.availability` so the
    sandbox, the web fetcher and any future connector share ONE vocabulary for
    "unavailable and why". It does not change containment: whether the bridge is
    attached is still the containment policy's decision and this function never
    overrides it.

    Three facts this is careful about, because each one used to be a silent
    "it will be fine":

    * **``allow_network=False``** is not an error. The container genuinely has
      no network, that is the design, and the receipt says ``container_network:
      false`` with no apology attached.
    * **A run configured OFFLINE that asks for network** is the interesting
      case. The bridge is still attached (containment is containment), but the
      receipt says the run told us to stay local, so a command that depends on
      the network is going to fail for a reason the operator declared. A
      receipt that only said "network: true" there would be the more convenient
      lie.
    * **An empty declared allowlist is a signal, not a permission.** It is
      reported as ``declared_hosts: []`` so "no operator allowlist was
      configured" stays distinguishable from "the allowlist is empty on
      purpose".
    """
    hosts = list(declared_hosts or declared_egress_allowlist())
    offline, offline_source = _offline_requested(config)
    if not allow_network:
        return {
            "schema_version": 1,
            "container_network": False,
            "container_network_reason": "containment_off",
            "offline": offline,
            "offline_source": offline_source,
            "declared_hosts": hosts,
            "available": False,
            "category": "policy",
            "detail": "the container is networkless by design; no bridge is attached",
        }
    detail = (
        "the run is configured offline, so a container that needs the network will fail "
        f"for a reason the operator declared ({offline_source}); the bridge is still attached "
        "because containment is containment"
        if offline
        else "a bridge is attached; container egress is a namespace decision and is not "
        "filtered per host"
    )
    return {
        "schema_version": 1,
        "container_network": True,
        "container_network_reason": "caller_declared",
        "offline": offline,
        "offline_source": offline_source,
        "declared_hosts": hosts,
        "available": not offline,
        "category": "offline" if offline else "available",
        "detail": detail,
    }


def _offline_requested(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Return ``(offline, source)`` through the one shared answer."""
    try:
        from shared.availability import offline_requested

        return offline_requested(config)
    except Exception:
        return False, "unavailable"


# ---------------------------------------------------------------------------
# AGT-07, axis (a): TECHNICAL CONTAINMENT
#
# Before this section there was exactly one notion of "safe" and it was a
# question about PROMPTING. The container itself did get `--network none`, a
# read-only rootfs and dropped capabilities, but the workspace bind mount was
# unconditionally read-WRITE, so a writable root also meant a writable
# `.git`: an agent could rewrite its own history, forge a commit, and the
# harness's own diff/patch path would read it as repository content.
#
# This section makes the containment a NAMED, DECLARED, REPORTED object with
# one rule per fact:
#
#   * network is OFF unless a caller declares it, and the receipt names the
#     declaration and the hosts;
#   * declared read-only subtrees (`.git` and friends) are mounted read-only
#     INSIDE the writable root, recursively, by re-mounting the directory
#     after the writable parent -- so a writable root no longer implies a
#     writable repository;
#   * writable roots are DECLARED, and when they are declared the workspace
#     mount itself becomes read-only and only those subtrees are writable.
#
# Two properties are load-bearing and both are honesty properties:
#
#   1. A read-only path that does not exist on the host is REPORTED as absent
#      and NOT mounted. `docker run -v` creates a missing host path, so
#      mounting a declared path that is not there would silently create a
#      directory in the user's repository.
#   2. A declared writable root can never re-open a read-only path. The
#      conflict is recorded and the read-only side wins, because a config
#      typo that widens containment is exactly what this axis exists to make
#      impossible.
# ---------------------------------------------------------------------------

#: Subtrees mounted READ-ONLY inside the writable workspace by default. VCS
#: metadata is here because it is the one tree whose forgery changes what a
#: reviewer believes happened; `.neo` is here because it is this product's own
#: per-repository configuration, which no task has a reason to rewrite and
#: every run has a reason to be able to read. An operator extends or replaces
#: the set through ``sandbox_readonly_paths`` -- and an explicit empty list
#: genuinely turns it off, which is why the default is REPORTED on every call.
DEFAULT_READONLY_SUBPATHS: Tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    "_darcs",
    ".neo",
)


@dataclass(frozen=True)
class ContainmentPolicy:
    """What the sandbox PERMITS, as a declared and reportable object.

    This is axis (a) and it is deliberately NOT a question about prompting.
    A caller builds it, the argv builder applies it, and the receipt reports
    it; nothing in this class consults, sets, or infers whether a human was
    asked, and no field here can be widened by an approval.

    ``readonly_subpaths`` are repository-RELATIVE. ``writable_roots`` is empty
    for the historical behaviour (the whole workspace is writable); when it is
    non-empty the workspace mount becomes read-only and only those subtrees
    are writable, so "writable roots declared" is a technical fact rather
    than a comment.
    """

    repo_path: str = ""
    readonly_subpaths: Tuple[str, ...] = DEFAULT_READONLY_SUBPATHS
    writable_roots: Tuple[str, ...] = ()
    network_enabled: bool = False
    network_declaration: str = ""
    declared_hosts: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    @property
    def workspace_readonly(self) -> bool:
        """Return whether the workspace mount itself is read-only."""
        return bool(self.writable_roots)

    def to_dict(self) -> Dict[str, Any]:
        """Return the containment receipt: axis (a) on its own terms.

        The key set is what ``shared.approval.describe_axes`` reads, and it
        carries no decision/prompt field at all -- a containment receipt that
        also said "no prompt was needed" would be the exact conflation this
        section exists to remove.
        """
        return {
            "axis": "containment",
            "network_enabled": bool(self.network_enabled),
            "network_declaration": self.network_declaration or "none",
            "declared_hosts": list(self.declared_hosts),
            "workspace_readonly": self.workspace_readonly,
            "writable_roots": list(self.writable_roots),
            "readonly_declared": list(self.readonly_subpaths),
            "rootfs_readonly": True,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        """Return the one line a human reads for the containment axis alone."""
        return (
            "containment: "
            f"{'network ON (declared by ' + (self.network_declaration or 'caller') + ')' if self.network_enabled else 'network off'}; "
            f"read-only inside the writable root: {', '.join(self.readonly_subpaths) or 'none'}; "
            f"writable roots: {', '.join(self.writable_roots) or 'whole workspace'}"
        )


def _normalize_repo_relative(value: Any) -> str:
    """Return a safe repository-relative POSIX path, or "" when unusable.

    Absolute paths, drive letters, ``..`` and ``.`` are refused rather than
    clamped: a containment path that reached outside the workspace would be a
    mount of something the workspace already does not contain, and a silent
    clamp would report a boundary different from the enforced one.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return ""
    parts: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return ""
        parts.append(part)
    return "/".join(parts)


def resolve_containment(
    repo_path: str,
    *,
    readonly_paths: Optional[Sequence[str]] = None,
    writable_roots: Optional[Sequence[str]] = None,
    allow_network: bool = False,
    network_declaration: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> ContainmentPolicy:
    """Resolve the containment policy for one call, and report what it refused.

    Precedence, and it is the only precedence:

    * ``readonly_paths=None`` (and an absent ``sandbox_readonly_paths`` key)
      means :data:`DEFAULT_READONLY_SUBPATHS`; an explicit ``[]`` means no
      read-only subtree at all, which is a real opt-out rather than a
      no-op. The distinction is the whole reason ``None`` and ``[]`` are not
      the same value here.
    * ``writable_roots`` is a declaration. Non-empty makes the workspace
      mount read-only and mounts exactly those subtrees read-write.
    * network is enabled only by a declaration: the caller's
      ``allow_network=True`` or an explicit policy. It is off by default, and
      the receipt names which declaration turned it on.

    A path that appears in BOTH lists is a conflict: the read-only side wins,
    the conflict is recorded, and nothing about the policy is silently
    widened. An unresolvable path (absolute, traversing, empty) is dropped
    with a note rather than clamped, so a receipt never claims a boundary
    other than the one enforced.

    Never raises for a bad value: a containment slip must not crash a run,
    and every slip is reported in ``notes``.
    """
    values = dict(config or {})
    raw_readonly = (
        values.get("sandbox_readonly_paths")
        if readonly_paths is None
        else readonly_paths
    )
    raw_writable = (
        values.get("sandbox_writable_roots")
        if writable_roots is None
        else writable_roots
    )
    notes: List[str] = []

    readonly: List[str] = []
    for item in (
        DEFAULT_READONLY_SUBPATHS
        if raw_readonly is None
        else (
            raw_readonly
            if isinstance(raw_readonly, (list, tuple, set))
            else [raw_readonly]
        )
    ):
        normalized = _normalize_repo_relative(item)
        if not normalized:
            notes.append(f"refused a read-only path declaration: {item!r}")
            continue
        if normalized not in readonly:
            readonly.append(normalized)

    writable: List[str] = []
    for item in (
        raw_writable
        if isinstance(raw_writable, (list, tuple, set))
        else ([] if raw_writable in (None, "", False) else [raw_writable])
    ):
        normalized = _normalize_repo_relative(item)
        if not normalized:
            notes.append(f"refused a writable-root declaration: {item!r}")
            continue
        if normalized in writable:
            continue
        if any(
            normalized == guard or normalized.startswith(guard + "/")
            for guard in readonly
        ):
            notes.append(
                f"writable root {normalized!r} overlaps a read-only path; the "
                "read-only declaration wins and the root is not writable"
            )
            continue
        writable.append(normalized)

    hosts = tuple(declared_egress_allowlist())
    enabled = bool(allow_network)
    source = network_declaration or ("caller" if enabled else "")
    if enabled and not hosts:
        notes.append(
            "network was declared but no egress allowlist is configured; the "
            "container reaches whatever the bridge offers, so this is a "
            "declaration and not an enforced allowlist"
        )
    return ContainmentPolicy(
        repo_path=str(repo_path or ""),
        readonly_subpaths=tuple(readonly),
        writable_roots=tuple(writable),
        network_enabled=enabled,
        network_declaration=source,
        declared_hosts=hosts,
        notes=tuple(notes),
    )


def readonly_overlays(
    policy: ContainmentPolicy,
) -> Tuple[Tuple[str, str], ...]:
    """Return the ``(host_path, container_path)`` read-only mounts to add.

    Only EXISTING paths are returned, and only directories: a read-only
    overlay is how a subtree becomes recursively read-only, and a missing host
    path would be CREATED by `docker run -v` -- silently adding a directory to
    a user's repository. Absences are reported separately by
    :func:`containment_receipt`, so "no `.git` here" is never rendered as
    "`.git` protected".

    Mount ORDER matters and is handled by the caller: these are appended after
    the writable workspace mount, and Docker applies a deeper target over a
    shallower one, so the read-only mount wins for that subtree only.
    """
    root = str(policy.repo_path or "")
    if not root:
        return ()
    overlays: List[Tuple[str, str]] = []
    for relative in policy.readonly_subpaths:
        host = Path(root) / relative
        try:
            if not host.is_dir():
                continue
        except OSError:
            continue
        overlays.append((str(host), f"{MOUNT_POINT}/{relative}"))
    return tuple(overlays)


def writable_overlays(
    policy: ContainmentPolicy,
) -> Tuple[Tuple[str, str], ...]:
    """Return the ``(host_path, container_path)`` read-WRITE mounts to add.

    Non-empty only when writable roots were DECLARED; then the workspace mount
    itself is read-only and these are the only writable paths in the
    container. A declared root that does not exist is skipped (and reported),
    because mounting a missing path would create it.
    """
    root = str(policy.repo_path or "")
    if not root or not policy.writable_roots:
        return ()
    overlays: List[Tuple[str, str]] = []
    for relative in policy.writable_roots:
        host = Path(root) / relative
        try:
            if not host.exists():
                continue
        except OSError:
            continue
        overlays.append((str(host), f"{MOUNT_POINT}/{relative}"))
    return tuple(overlays)


def containment_receipt(
    policy: ContainmentPolicy, *, applied: Sequence[Tuple[str, str]] = ()
) -> Dict[str, Any]:
    """Return the containment receipt WITH what was actually mounted.

    ``readonly_applied`` is the measured set (the directories that existed and
    were mounted read-only) and ``readonly_absent`` is the declared set minus
    the measured one. A receipt that reported the declaration as the
    enforcement would claim a `.git` boundary in a copy of a repository that
    has no `.git`; a receipt that omitted the absence would claim the
    opposite, that the tree is protected when it is simply not there.

    ``applied`` is the mount list the argv actually carried, so the receipt
    is derived from the argv rather than from the intent that produced it.
    """
    receipt = policy.to_dict()
    readonly = list(policy.readonly_subpaths)
    targets = {str(target) for _, target in (applied or ())}
    measured = [
        relative for relative in readonly if f"{MOUNT_POINT}/{relative}" in targets
    ]
    receipt["readonly_applied"] = measured
    receipt["readonly_absent"] = [item for item in readonly if item not in measured]
    receipt["writable_applied"] = [
        relative
        for relative in policy.writable_roots
        if f"{MOUNT_POINT}/{relative}" in targets
    ]
    receipt["mounts"] = [
        f"{_win_to_docker(source)} -> {target}" for source, target in (applied or ())
    ]
    return receipt


def prune_sandbox_artifacts(
    *,
    images: Optional[List[str]] = None,
    volumes: Optional[List[str]] = None,
    build_cache: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove sandbox container layers, dependency volumes, and build cache.

    The sandbox deliberately leaves images behind (they are expensive to
    rebuild), so nothing reaps them automatically; this is the explicit
    operator/cleanup primitive. It only ever removes artifacts the sandbox
    itself created:

    * images whose tag starts with ``harness-exec`` (base and per-repo dep
      images) or that appear in the explicit ``images`` list;
    * volumes named ``hexec-*`` (the JS dependency volumes);
    * dangling build cache via ``docker builder prune``.

    Never raises and never touches a non-sandbox image or volume: a caller
    cannot accidentally prune a user's own Docker state by calling this.
    """
    report: Dict[str, Any] = {
        "removed_images": [],
        "removed_volumes": [],
        "pruned_build_cache": False,
        "dry_run": bool(dry_run),
        "errors": [],
    }
    try:
        probe = _run_docker(["ps", "-q"], check=False, timeout_s=30)
    except (SandboxUnavailableError, RuntimeError, OSError) as exc:
        report["errors"].append(f"docker unavailable: {exc}")
        return report
    if probe.returncode != 0:
        report["errors"].append("docker ps probe failed")
        return report

    wanted_images = [
        tag
        for tag in (images or [])
        if str(tag).startswith(IMAGE_PREFIX) or str(tag).startswith(CONTAINER_PREFIX)
    ]
    if not dry_run:
        try:
            listing = _run_docker(
                ["images", "--format", "{{.Repository}}:{{.Tag}}"],
                check=False,
                timeout_s=60,
            )
        except (SandboxUnavailableError, RuntimeError, OSError) as exc:
            listing = None
            report["errors"].append(f"image listing failed: {exc}")
        if listing is not None and listing.returncode == 0:
            for line in (listing.stdout or "").splitlines():
                tag = line.strip()
                if tag and (tag.startswith(f"{IMAGE_PREFIX}:") or tag in wanted_images):
                    if tag in report["removed_images"]:
                        continue
                    removed = _run_docker(
                        ["rmi", "-f", tag], check=False, timeout_s=120
                    )
                    if removed.returncode == 0:
                        report["removed_images"].append(tag)
    if not dry_run:
        try:
            volume_listing = _run_docker(
                ["volume", "ls", "--format", "{{.Name}}"], check=False, timeout_s=60
            )
        except (SandboxUnavailableError, RuntimeError, OSError) as exc:
            volume_listing = None
            report["errors"].append(f"volume listing failed: {exc}")
        if volume_listing is not None and volume_listing.returncode == 0:
            for line in (volume_listing.stdout or "").splitlines():
                name = line.strip()
                if not name or not name.startswith(CONTAINER_PREFIX):
                    continue
                removed = _run_docker(
                    ["volume", "rm", "-f", name], check=False, timeout_s=120
                )
                if removed.returncode == 0:
                    report["removed_volumes"].append(name)
    if build_cache and not dry_run:
        try:
            _run_docker(
                ["builder", "prune", "-f", "--filter", "until=0s"],
                check=False,
                timeout_s=300,
            )
            report["pruned_build_cache"] = True
        except (SandboxUnavailableError, RuntimeError, OSError) as exc:
            report["errors"].append(f"build cache prune failed: {exc}")
    return report


# ---------------------------------------------------------------------------
# Docker plumbing
# ---------------------------------------------------------------------------

_docker_ok: Optional[bool] = None
_image_lock = threading.Lock()


def docker_available() -> bool:
    """True if the docker daemon is reachable; positive results are cached.

    A failed probe is not cached so a process can recover after Docker Desktop
    or the daemon is restarted. Never raises.
    """
    global _docker_ok
    if _docker_ok is True:
        return True
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _docker_ok = cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        _docker_ok = False
    return _docker_ok


def _docker_unavailable_error(stderr: str, stdout: str = "") -> bool:
    """Recognize daemon/CLI connection failures independently of exit code."""
    text = f"{stderr}\n{stdout}".lower()
    markers = (
        "cannot connect to the docker daemon",
        "error during connect",
        "dockerdesktoplinuxengine",
        "is the docker daemon running",
    )
    return any(marker in text for marker in markers)


def _docker_environment() -> Dict[str, str]:
    from shared.security import scrub_environment

    return scrub_environment(isolated=False)


def _run_docker(
    args: List[str], timeout_s: Optional[int] = None, check: bool = True
) -> "subprocess.CompletedProcess[str]":
    """Run one docker CLI command with bounded output capture."""
    command = ["docker", *args]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_environment(),
        )
    except OSError as exc:
        raise SandboxUnavailableError(f"docker CLI not runnable: {exc}") from exc
    stdout_capture, stderr_capture, collectors = _start_output_collectors(proc)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _join_output_collectors(collectors)
        raise RuntimeError(f"docker {' '.join(args[:2])} timed out") from exc
    _join_output_collectors(collectors)
    cp = subprocess.CompletedProcess(
        command,
        proc.returncode if proc.returncode is not None else 1,
        stdout_capture.value(),
        stderr_capture.value(),
    )
    if cp.returncode != 0 and _docker_unavailable_error(cp.stderr, cp.stdout):
        global _docker_ok
        _docker_ok = False
        raise SandboxUnavailableError(
            f"docker daemon became unavailable: {(cp.stderr or cp.stdout)[:1000]}"
        )
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
        if p.is_symlink():
            raise SandboxDependencyError(f"dependency manifest is a symlink: {p}")
        return p.read_text(encoding="utf-8", errors="replace")
    except SandboxDependencyError:
        raise
    except OSError:
        return ""


def base_image_tag() -> str:
    """Tag of the harness base image (python:3.10-slim + pytest)."""
    return f"{IMAGE_PREFIX}:base"


def node_base_image_tag() -> str:
    """Tag of the JS/TS harness base image (node:22-slim, git for npm)."""
    return f"{IMAGE_PREFIX}:node-base"


_JS_SOURCE_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_PY_MARKER_FILES = (
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "tox.ini",
    "environment.yml",
    "pytest.ini",
    "conftest.py",
)


def _detect_repo_language(repo_path: str) -> str:
    """Classify a repository using the verifier's shared language policy.

    Python dependency/test markers win over a JavaScript manifest. With no
    Python marker, package.json identifies JavaScript; otherwise a bounded,
    symlink-free source census covers the root and common source/test trees.
    The census is intentionally conservative and never raises.
    """
    try:
        if any(os.path.isfile(os.path.join(repo_path, f)) for f in _PY_MARKER_FILES):
            return "python"
        if os.path.isfile(os.path.join(repo_path, "package.json")):
            return "js"
        inspected = 0
        for _root, dirs, files in os.walk(repo_path, topdown=True, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if name not in {"node_modules", ".git", ".hg", ".svn", "__pycache__"}
                and not name.startswith(".")
            ]
            for name in files:
                inspected += 1
                if inspected > 2048:
                    return "python"
                if name.endswith(_JS_SOURCE_EXTS):
                    return "js"
    except OSError:
        pass
    return "python"


def _dep_image_tag(repo_path: str) -> str:
    """Per-repo image tag: prefix + hash of base tag and dep manifests.

    JS/TS repos hash the node-base tag + package.json + lockfile; Python
    repos the pytest base + Python manifests — the two families can never
    collide (different base-tag component)."""
    lang = _detect_repo_language(repo_path)
    if lang == "js":
        parts: List[str] = [node_base_image_tag()]
        for name in JS_DEP_MANIFESTS:
            p = Path(repo_path, name)
            if p.is_file():
                parts.append(p.name + ":" + _read_text(p))
        return f"{IMAGE_PREFIX}:{_fingerprint(parts)}"
    parts = [base_image_tag()]
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


def _node_base_dockerfile_text() -> str:
    """Dockerfile for the JS/TS harness base image (node + npm + git).

    node:22-slim ships node+npm; git is added because npm ci on some
    repos fetches git-hosted deps, and a few test setups shell out to
    it. npm cache is disabled to keep the image small.
    """
    return (
        f"FROM {BASE_IMAGE_NODE}\n"
        "ENV NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*\n"
        f"WORKDIR {MOUNT_POINT}\n"
    )


def _repo_dockerfile_text(tag: str) -> str:
    """Dockerfile for a Python repo's dependency layer (FROM the base).

    Static: the context always contains harness-deps.txt (requirements.txt
    content, may be empty), pyproject.toml (may be empty), and the extractor
    script. The extractor merges requirements + pyproject [project]
    dependencies into /tmp/all-deps.txt; pip installs only if it is non-empty.
    Richer flows (poetry.lock, conda) need an explicit image build by the
    caller — see execution/AGENTS.md.
    """
    return "\n".join(
        [
            f"FROM {base_image_tag()}",
            f'LABEL harness.dep-image="{tag}"',
            "COPY harness-deps.txt /tmp/harness-deps.txt",
            "COPY pyproject.toml /tmp/pyproject.toml",
            "COPY _extract_pyproject_deps.py /tmp/_extract.py",
            "RUN python /tmp/_extract.py > /tmp/all-deps.txt",
            "RUN sh -c 'if [ -s /tmp/all-deps.txt ]; then "
            "pip install --no-cache-dir --disable-pip-version-check "
            "-r /tmp/all-deps.txt; fi'",
            "",
        ]
    )


def _js_repo_dockerfile_text(
    tag: str, has_lockfile: bool, lockfile_name: Optional[str] = None
) -> str:
    """Dockerfile for a JS/TS dependency layer with lifecycle scripts off.

    The manifest and its selected lockfile are copied into a separate build
    directory, so repository source remains bind-mounted at run time. npm
    lifecycle scripts are disabled because an untrusted repository manifest
    must not execute during a dependency image build.
    """
    selected_lock = lockfile_name
    if selected_lock is None and has_lockfile:
        selected_lock = "package-lock.json"
    if selected_lock in ("package-lock.json", "npm-shrinkwrap.json"):
        install = "npm ci --ignore-scripts --no-audit --no-fund"
    elif selected_lock == "yarn.lock":
        install = "corepack yarn install --immutable --ignore-scripts"
    elif selected_lock == "pnpm-lock.yaml":
        install = "corepack pnpm install --frozen-lockfile --ignore-scripts"
    else:
        install = "npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps"
    lines = [
        f"FROM {node_base_image_tag()}",
        f'LABEL harness.dep-image="{tag}"',
        "RUN mkdir -p /opt/deps",
        "COPY package.json /opt/deps/package.json",
    ]
    if selected_lock:
        lines.append(f"COPY {selected_lock} /opt/deps/{selected_lock}")
    lines += [
        "WORKDIR /opt/deps",
        "RUN corepack enable",
        f"RUN {install}",
        f"WORKDIR {MOUNT_POINT}",
        "",
    ]
    return "\n".join(lines)


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
_node_base_done = False


def _ensure_base_image(kind: str = "python") -> str:
    """Build harness-exec:base (python) or :node-base (js) if missing.

    Assumes docker is available. Process-safe: concurrent worker processes
    race the same cold build too — guarded by a cross-process lockfile
    (see _CrossProcLock). kind='python' and 'js' build their own base
    independently (a Python-only workload never builds the node image
    and vice versa).
    """
    global _base_done, _node_base_done
    if kind == "js":
        if _node_base_done:
            return node_base_image_tag()
        with _ensure_base_lock:
            if _node_base_done:
                return node_base_image_tag()
            tag = node_base_image_tag()
            probe = _run_docker(["image", "inspect", tag], check=False)
            if probe.returncode != 0:
                with _CrossProcLock("hexec-node-base-build") as acquired:
                    if acquired:
                        probe = _run_docker(["image", "inspect", tag], check=False)
                        if probe.returncode != 0:
                            with tempfile.TemporaryDirectory(
                                prefix="hexec-node-base-"
                            ) as ctx:
                                (Path(ctx) / "Dockerfile").write_text(
                                    _node_base_dockerfile_text(), encoding="utf-8"
                                )
                                _run_docker(
                                    ["build", "-t", tag, ctx],
                                    timeout_s=BUILD_TIMEOUT_S,
                                )
                    elif not _wait_for_image(tag, BUILD_TIMEOUT_S + 60):
                        raise SandboxDependencyError(
                            f"timed out waiting for peer to build {tag}"
                        )
            if _run_docker(["image", "inspect", tag], check=False).returncode != 0:
                raise SandboxDependencyError(
                    f"base image is unavailable after build: {tag}"
                )
            _node_base_done = True
        return node_base_image_tag()

    if _base_done:
        return base_image_tag()
    with _ensure_base_lock:
        if _base_done:
            return base_image_tag()
        tag = base_image_tag()
        probe = _run_docker(["image", "inspect", tag], check=False)
        if probe.returncode != 0:
            with _CrossProcLock("hexec-base-build") as acquired:
                if acquired:
                    probe = _run_docker(["image", "inspect", tag], check=False)
                    if probe.returncode != 0:
                        with tempfile.TemporaryDirectory(prefix="hexec-base-") as ctx:
                            (Path(ctx) / "Dockerfile").write_text(
                                _base_dockerfile_text(), encoding="utf-8"
                            )
                            _run_docker(
                                ["build", "-t", tag, ctx], timeout_s=BUILD_TIMEOUT_S
                            )
                elif not _wait_for_image(tag, BUILD_TIMEOUT_S + 60):
                    raise SandboxDependencyError(
                        f"timed out waiting for peer to build {tag}"
                    )
        if _run_docker(["image", "inspect", tag], check=False).returncode != 0:
            raise SandboxDependencyError(
                f"base image is unavailable after build: {tag}"
            )
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
        self._token = uuid.uuid4().hex

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
                payload = json.dumps(
                    {"pid": os.getpid(), "token": self._token},
                    sort_keys=True,
                )
                os.write(fd, payload.encode("utf-8"))
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
        pid, _token = self._read_holder()
        return pid

    def _read_holder(self) -> Tuple[Optional[int], str]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("{"):
                value = json.loads(text)
                return int(value.get("pid", 0) or 0), str(value.get("token") or "")
            return int(text or 0), ""
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, ""

    def _steal_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
            pid, token = self._read_holder()
            stale = age > _BUILD_LOCK_STALE_S or (
                pid is not None and pid > 0 and not _pid_alive(pid)
            )
            if stale:
                current_pid, current_token = self._read_holder()
                if current_pid == pid and current_token == token:
                    self.path.unlink(missing_ok=True)
                    return True
        except OSError:
            return False
        return False

    def __exit__(self, *exc) -> None:
        if self._held:
            try:
                pid, token = self._read_holder()
                if pid == os.getpid() and token == self._token:
                    self.path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def _wait_for_image(tag: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        probe = _run_docker(["image", "inspect", tag], check=False)
        if probe.returncode == 0:
            return True
        time.sleep(0.5)
    probe = _run_docker(["image", "inspect", tag], check=False)
    return probe.returncode == 0


def ensure_image(repo_path: str, rebuild: bool = False) -> str:
    """Build (or reuse) the per-repo dependency image; returns its tag.

    Assumes repo_path is a readable directory and docker is available.
    Python repos get the pytest base + pip deps; JS/TS repos (package.json
    without Python markers) get the node base + npm deps pre-installed
    under /opt/deps/node_modules (mounted over /workspace/node_modules at
    run time — see execute_sandboxed). Idempotent: an existing matching
    image is reused unless rebuild=True. Thread-safe in-process
    (_image_lock) AND process-safe (a lockfile serializes scheduler-
    spawned workers racing the same cold image: one builds, the rest
    wait briefly then hit the image-cache probe).
    """
    lang = _detect_repo_language(repo_path)
    with _image_lock:
        _ensure_base_image("js" if lang == "js" else "python")
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
            if lang == "js":
                return _build_js_image(repo_path, tag)
            req_path = Path(repo_path, "requirements.txt")
            req_text = _read_text(req_path) if req_path.is_file() else ""
            pyproject_path = Path(repo_path, "pyproject.toml")
            pyproject_text = (
                _read_text(pyproject_path) if pyproject_path.is_file() else ""
            )
            with tempfile.TemporaryDirectory(prefix="hexec-build-") as ctx:
                ctx_path = Path(ctx)
                (ctx_path / "harness-deps.txt").write_text(req_text, encoding="utf-8")
                (ctx_path / "pyproject.toml").write_text(
                    pyproject_text, encoding="utf-8"
                )
                (ctx_path / "_extract_pyproject_deps.py").write_text(
                    _EXTRACT_PYPROJECT_DEPS, encoding="utf-8"
                )
                (ctx_path / "Dockerfile").write_text(
                    _repo_dockerfile_text(tag), encoding="utf-8"
                )
                _run_docker(["build", "-t", tag, ctx], timeout_s=BUILD_TIMEOUT_S)
            return tag


def _build_js_image(repo_path: str, tag: str) -> str:
    """Build the JS/TS dependency image (called inside the held build lock)."""
    pkg = Path(repo_path, "package.json")
    lock_names = (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
    )
    lock_name = next(
        (name for name in lock_names if (Path(repo_path) / name).is_file()), None
    )
    with tempfile.TemporaryDirectory(prefix="hexec-jsbuild-") as ctx:
        ctx_path = Path(ctx)
        (ctx_path / "package.json").write_text(
            _read_text(pkg) if pkg.is_file() else "{}", encoding="utf-8"
        )
        if lock_name:
            (ctx_path / lock_name).write_text(
                _read_text(Path(repo_path) / lock_name), encoding="utf-8"
            )
        (ctx_path / "Dockerfile").write_text(
            _js_repo_dockerfile_text(
                tag, has_lockfile=lock_name is not None, lockfile_name=lock_name
            ),
            encoding="utf-8",
        )
        _run_docker(["build", "-t", tag, ctx], timeout_s=BUILD_TIMEOUT_S)
    return tag


# ---------------------------------------------------------------------------
# Container execution
# ---------------------------------------------------------------------------

_js_deps_volumes: Dict[str, str] = {}
_js_deps_lock = threading.Lock()


def _js_volume_ready(volume: str, image: str) -> bool:
    """Return whether a JS dependency volume has a complete marker."""
    try:
        probe = _run_docker(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--cpus",
                "1.0",
                "--pids-limit",
                "512",
                "--volume",
                f"{volume}:/target:ro",
                image,
                "sh",
                "-c",
                "test -f /target/.harness-ready && test -d /target",
            ],
            check=False,
            timeout_s=120,
        )
        return probe.returncode == 0
    except SandboxUnavailableError:
        raise
    except Exception as exc:
        raise SandboxDependencyError(
            f"unable to validate JavaScript dependency volume {volume}: {exc}"
        ) from exc


def _js_deps_volume(repo_path: str, image: str) -> str:
    """Create and validate a per-image node_modules volume atomically."""
    key = image
    vol = f"hexec-node-deps-{_fingerprint([image])[:12]}"
    with _js_deps_lock:
        cached = _js_deps_volumes.get(key)
    if cached:
        probe = _run_docker(["volume", "inspect", cached], check=False)
        if probe.returncode == 0 and _js_volume_ready(cached, image):
            return cached
        with _js_deps_lock:
            _js_deps_volumes.pop(key, None)

    lock = _CrossProcLock(f"hexec-jsdeps-{_fingerprint([image])[:12]}")
    deadline = time.monotonic() + BUILD_TIMEOUT_S + 60
    while time.monotonic() < deadline:
        acquired = False
        try:
            with lock as acquired:
                if not acquired:
                    probe = _run_docker(["volume", "inspect", vol], check=False)
                    if probe.returncode == 0 and _js_volume_ready(vol, image):
                        with _js_deps_lock:
                            _js_deps_volumes[key] = vol
                        return vol
                    if lock.holder_alive():
                        time.sleep(1.0)
                        continue
                    continue

                probe = _run_docker(["volume", "inspect", vol], check=False)
                if probe.returncode == 0:
                    if _js_volume_ready(vol, image):
                        with _js_deps_lock:
                            _js_deps_volumes[key] = vol
                        return vol
                    _run_docker(["volume", "rm", "-f", vol], check=False, timeout_s=60)

                uid = _container_user()
                cp = _run_docker(
                    [
                        "run",
                        "--rm",
                        "--pull=never",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "CHOWN",
                        "--cap-add",
                        "FOWNER",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--tmpfs",
                        "/tmp:rw,exec,size=64m",
                        "--memory",
                        "1g",
                        "--memory-swap",
                        "1g",
                        "--cpus",
                        "1.0",
                        "--pids-limit",
                        "512",
                        "--user",
                        "0:0",
                        "--volume",
                        f"{vol}:/target",
                        "--env",
                        f"HARNESS_UID={uid}",
                        image,
                        "sh",
                        "-c",
                        "set -eu; "
                        "mkdir -p /target; "
                        "if [ -d /opt/deps/node_modules ]; then "
                        "cp -a /opt/deps/node_modules/. /target/; fi; "
                        'chown -R "$HARNESS_UID" /target; '
                        "printf ready > /target/.harness-ready",
                    ],
                    timeout_s=600,
                )
                if cp.returncode != 0 or not _js_volume_ready(vol, image):
                    _run_docker(["volume", "rm", "-f", vol], check=False, timeout_s=60)
                    raise SandboxDependencyError(
                        f"JavaScript dependency volume setup failed for {image}"
                    )
                with _js_deps_lock:
                    _js_deps_volumes[key] = vol
                return vol
        except SandboxUnavailableError:
            raise
        except SandboxDependencyError:
            raise
        except Exception as exc:
            raise SandboxDependencyError(
                f"JavaScript dependency volume setup failed for {image}: {exc}"
            ) from exc
    raise SandboxDependencyError(
        f"timed out preparing JavaScript dependency volume for {image}"
    )


def _scrub_container_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Drop credential-like variables before passing caller env to Docker."""
    if not env:
        return {}
    from shared.security import scrub_environment

    scrubbed = scrub_environment(env, isolated=False)
    url_userinfo = re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@")
    return {
        str(key): str(value)
        for key, value in scrubbed.items()
        if not url_userinfo.search(str(value))
    }


def scrub_env(
    environ: Optional[Dict[str, str]] = None,
    **kwargs: object,
) -> Dict[str, str]:
    """Return the shared child-process environment scrubber's result."""
    from execution.workspace import scrub_env as _scrub_env

    if environ is None:
        return _scrub_env(**kwargs)
    return _scrub_env(environ, **kwargs)


def _docker_run_args(
    image: str,
    repo_path: str,
    timeout_s: int,
    allow_network: bool,
    env: Optional[Dict[str, str]],
    mem_limit: str,
    cpu_limit: float,
    pids_limit: int,
    js_deps: Optional[str] = None,
    *,
    workspace_readonly: bool = False,
    readonly_mounts: Optional[Sequence[Tuple[str, str]]] = None,
    writable_mounts: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[str]:
    """Assemble the `docker run` argv prefix (before the shell command).

    Pure function (unit-testable without a daemon): the same inputs always
    produce the same flags. repo_path is NOT resolved here — pass the
    already-normalized absolute path.

    js_deps (JS/TS repos) names the docker VOLUME holding the repo's
    npm dependencies: it is mounted at /workspace/node_modules ON TOP of
    the repo bind mount. The dependency volume is read-only; the verifier's
    runner commands disable runner caches so no writable nested mount is
    needed and one task cannot mutate dependencies used by another task.

    The three containment arguments are AGT-07 axis (a) and all three
    default to the historical argv, byte for byte:

    * ``workspace_readonly`` adds ``:ro`` to the workspace bind mount, which
      is what a declared set of writable roots means;
    * ``readonly_mounts`` / ``writable_mounts`` are pre-resolved
      ``(host, container)`` pairs from
      :func:`readonly_overlays` / :func:`writable_overlays`. They are
      appended AFTER the workspace mount on purpose: Docker applies a deeper
      mount target over a shallower one, so ``/workspace/.git:ro`` narrows a
      writable ``/workspace`` instead of being shadowed by it.
    """
    mount = f"{_win_to_docker(repo_path)}:{MOUNT_POINT}"
    if workspace_readonly:
        mount += ":ro"
    args: List[str] = [
        "run",
        "--rm",
        "--name",
        f"{_container_name_prefix()}-{uuid.uuid4().hex[:12]}",
        "--pull=never",
        "--volume",
        mount,
        "--workdir",
        MOUNT_POINT,
        "--user",
        _container_user(),
        "--memory",
        mem_limit,
        "--memory-swap",
        mem_limit,  # equal to --memory => no swap
        "--cpus",
        str(cpu_limit),
        "--pids-limit",
        str(pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,size=256m",
        "--env",
        "HOME=/tmp",
    ]
    if js_deps:
        args += ["--volume", f"{js_deps}:/workspace/node_modules:ro"]
    for source, target in readonly_mounts or ():
        args += ["--volume", f"{_win_to_docker(source)}:{target}:ro"]
    for source, target in writable_mounts or ():
        args += ["--volume", f"{_win_to_docker(source)}:{target}:rw"]
    if not allow_network:
        args += ["--network", "none"]
    if env:
        for key, value in _scrub_container_env(env).items():
            args += ["--env", f"{key}={value}"]
    args.append(image)
    return args


def _env_token() -> str:
    """Stable per-OS-environment token for container names (Round 7).

    Identifies the PID SPACE a container's owner lives in: with Docker
    Desktop, Windows-host and WSL-host processes share one daemon but
    have unrelated PID spaces, so an orphan sweep must only judge owners
    in its own environment. The token is 8 hex chars of SHA-256 over a
    stable machine identifier (Windows MachineGuid via reg; Linux
    /etc/machine-id; hostname fallback) — stable across processes and
    reboots, different between a Windows host and its WSL distros.
    Never raises; unknown environments degrade to a hostname-based
    token (still distinguishes the spaces, just less stable).
    """
    try:
        raw = ""
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        else:
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    raw = Path(path).read_text(encoding="utf-8").strip()
                    if raw:
                        break
                except OSError:
                    continue
        if not raw:
            raw = f"{os.name}:{socket.gethostname()}"
        return _fingerprint([f"env-token:{os.name}", raw])[:8]
    except Exception:
        try:
            return _fingerprint([f"env-fallback:{socket.gethostname()}"])[:8]
        except Exception:
            # absolute last resort: constant token — reaping stays
            # same-env-only within this host anyway (all processes here
            # share the fallback), just not stable across reboots
            return _fingerprint(["env-unavailable"])[:8]


_env_token_cached: Optional[str] = None
_env_token_lock = threading.Lock()


def _container_name_prefix() -> str:
    """Container-name prefix for THIS process's containers.

    Round-7 format: hexec-e<env8>-p<pid> — the ENVIRONMENT token comes
    FIRST, before the PID. Ordering matters for compatibility with
    PRE-FIX sweeps still running in other processes (long sessions
    started before this code landed): their name regexes anchor on
    `hexec-p<pid>` and simply don't match `hexec-e...` names, so they
    can never parse (and thus never misjudge) our PID — foreign-PID-
    space kills are impossible even from stale code. This process's own
    containers are further told apart by the full prefix + uuid suffix.
    """
    global _env_token_cached
    if _env_token_cached is None:
        with _env_token_lock:
            if _env_token_cached is None:
                _env_token_cached = _env_token()
    return f"{CONTAINER_PREFIX}-e{_env_token_cached}-p{os.getpid()}"


def own_container_filter() -> str:
    """Docker name-filter string matching THIS process's containers.

    Public for tests and tooling: `docker ps --filter name=<this>`.
    The prefix form survives uuid suffixes (`docker ps` name filters
    are substring matches).
    """
    return _container_name_prefix() + "-"


def _container_pid_from_name(name: str) -> Optional[int]:
    """Owner PID encoded in an hexec-* container name (Round-7 format),
    if any.

    Pre-Round-7 names (hexec-p<pid>-<uuid>, hexec-<uuid>) return None:
    their owner's liveness can't be judged from here (old-format names
    carry no environment token, so a foreign-PID-space probe can't be
    distinguished from a same-space one) — never reaped.
    """
    m = re.match(rf"^{CONTAINER_PREFIX}-e[0-9a-f]{{8}}-p(\d+)(?:-|$)", name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _container_env_from_name(name: str) -> Optional[str]:
    """Environment token encoded in an hexec-* container name, if any.

    Names without an env token (the Round-3 hexec-p<pid>-<uuid> format
    and older) return None: owner liveness in a foreign PID space is
    unknowable, so they are never reaped (the conservative policy that
    already protected pre-PID names).
    """
    m = re.match(rf"^{CONTAINER_PREFIX}-e([0-9a-f]{{8}})-p\d+(?:-|$)", name)
    return m.group(1) if m else None


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
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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

    PID-SPACE SCOPING (Round 7): containers whose name carries a
    DIFFERENT environment token than this process's are never judged —
    with Docker Desktop, Windows-host and WSL-host harness processes
    share one daemon but have unrelated PID spaces, so a foreign PID
    can't be probed for liveness (a Windows sweep on a WSL pid always
    reads "dead" and would kill a LIVE container; reproduced live).
    Containers with no env token (Round-3-format names) are never
    reaped either, same as pre-PID names: unknowable owner, skip.

    Assumes docker is available (caller in execute_sandboxed already
    checked). include_stale_names=True also kills RUNNING containers
    with the old pre-PID name format — only use that when no other
    harness processes could legitimately own one. Returns the names of
    containers killed (empty when none or dry_run=True). Never raises:
    reaping is best-effort self-healing, not a correctness gate.
    """
    names: List[str] = []
    try:
        # this process's env token (computes + caches on first use)
        my_env = _container_env_from_name(_container_name_prefix() + "-x")
        if my_env is None:  # cannot happen by construction; be safe
            return names
        ls = _run_docker(
            ["ps", "--filter", f"name={CONTAINER_PREFIX}-", "--format", "{{.Names}}"],
            check=False,
            timeout_s=30,
        )
        if ls.returncode != 0:
            return names
        for name in ls.stdout.split():
            pid = _container_pid_from_name(name)
            if pid is None:
                if include_stale_names:
                    names.append(name)
                continue  # old-format name: owner unknowable
            env_tok = _container_env_from_name(name)
            if env_tok is None or env_tok != my_env:
                continue  # foreign PID space (or pre-env-token name):
                # liveness of that owner is UNKNOWABLE here — never reap
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
    """Return a validated numeric container uid:gid pair."""
    forced = os.environ.get("HARNESS_SANDBOX_UID")
    value = forced or (
        "1000:1000" if os.name == "nt" else f"{os.getuid()}:{os.getgid()}"
    )
    if not re.fullmatch(r"[0-9]+:[0-9]+", value):
        raise ValueError("HARNESS_SANDBOX_UID must be numeric uid:gid")
    return value


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Keep head and tail with an omission marker (mirrors tools.truncate)."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n[... {omitted} chars omitted ...]\n" + text[-tail:]


class _BoundedCapture:
    """Keep bounded head and tail bytes while a child stream is drained."""

    def __init__(self, limit: int = MAX_OUTPUT_BYTES) -> None:
        self.limit = max(0, int(limit))
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, data: bytes) -> None:
        """Add one stream chunk without retaining the complete stream."""
        if not data or self.limit == 0:
            self.total += len(data)
            return
        self.total += len(data)
        if len(self.head) < self.head_limit:
            take = min(len(data), self.head_limit - len(self.head))
            self.head.extend(data[:take])
            data = data[take:]
        if data:
            self.tail.extend(data)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def value(self) -> str:
        """Decode the retained head/tail with an explicit omission marker."""
        if self.total <= self.limit:
            raw = bytes(self.head) + bytes(self.tail)
        else:
            omitted = self.total - self.limit
            raw = (
                bytes(self.head)
                + f"\n[... {omitted} chars omitted ...]\n".encode()
                + bytes(self.tail)
            )
        return raw.decode("utf-8", errors="replace")


def _drain_stream(stream, capture: _BoundedCapture) -> None:
    """Drain one binary pipe into a bounded capture until EOF."""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            capture.feed(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _start_output_collectors(proc, cap_bytes: int = MAX_OUTPUT_BYTES) -> tuple:
    """Start bounded stdout/stderr readers for a Popen object.

    ``cap_bytes`` is the INGRESS cap for the declared purpose
    (``execution.ingress.cap_for``), passed through so the collector and the
    redactor are bounded by the SAME number. If the collector kept its own
    1 MB while the ingress allowed 4 MB for a verification run, the raised
    verification cap would be a receipt describing a boundary the capture
    never had — which is the exact "receipt that lies" failure this module
    exists to prevent.
    """
    stdout_capture = _BoundedCapture(cap_bytes)
    stderr_capture = _BoundedCapture(cap_bytes)
    threads = []
    for stream, capture in (
        (proc.stdout, stdout_capture),
        (proc.stderr, stderr_capture),
    ):
        thread = threading.Thread(
            target=_drain_stream, args=(stream, capture), daemon=True
        )
        thread.start()
        threads.append(thread)
    return stdout_capture, stderr_capture, threads


def _join_output_collectors(threads: list) -> None:
    """Wait briefly for pipe readers after the child has exited."""
    for thread in threads:
        thread.join(timeout=5)


def _seal_error_text(text: str) -> str:
    """Redact a bounded slice of a docker failure message for an exception.

    A `SandboxUnavailableError` message is displayed to a human AND carried in
    a trace row, so it is an egress like any other. `_docker_unavailable_error`
    only classifies; this is what keeps the classified text from carrying a
    credential out of the sandbox's own diagnostics.
    """
    from execution.ingress import seal_output

    try:
        sealed, _report = seal_output(text, cap_bytes=1000)
        return sealed
    except Exception:  # pragma: no cover - seal_output is total by construction
        return "(output withheld: redaction unavailable)"


def _sealed_result(result: ExecutionResult, purpose: str = "") -> ExecutionResult:
    """Pass one ExecutionResult through the single subprocess-output ingress.

    Every return from :func:`execute_sandboxed` goes through here, so the
    invariant "no subprocess byte reaches a tool result without passing the
    ingress redaction" is structural rather than a convention someone has to
    remember. A developer adding a sixth return site cannot forget: the
    function's own return type is the sealed type, and the raw captures are
    local variables that never escape this function.

    A redaction failure is a VALUE, not a raise (:func:`seal_output` returns
    ``ok=False`` with ``(output withheld: redaction unavailable)``), because a
    redactor fault must not change a sandboxed command's exit code — that
    would report a machine fault as a code failure, which is precisely the
    "unavailable rendered as a result" shape this repo forbids.
    """
    try:
        sealed, _reports = seal_streams(result, purpose=purpose)
    except Exception:  # pragma: no cover - seal_streams is total by construction
        return ExecutionResult(
            result.exit_code,
            REDACTION_UNAVAILABLE,
            REDACTION_UNAVAILABLE,
            result.timed_out,
        )
    return sealed


def _cleanup_container(proc, name: str) -> None:
    """Stop the named container and the local Docker CLI process."""
    if name:
        for args in (["kill", name], ["rm", "-f", name]):
            try:
                _run_docker(args, check=False, timeout_s=30)
            except Exception:
                pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass


def _token_cancelled(token: object) -> bool:
    """Return whether a cancellation token or event has been signalled."""
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    checker = getattr(token, "is_set", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


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
    cancel_event: Optional[object] = None,
    cancellation_token: Optional[object] = None,
    containment: Optional[ContainmentPolicy] = None,
    readonly_paths: Optional[Sequence[str]] = None,
    writable_roots: Optional[Sequence[str]] = None,
    purpose: str = "",
    secrets: Sequence[str] = (),
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
    --network none for this call; env adds non-secret container variables;
    mem/cpu/pids limits override the defaults; cancel_event or
    cancellation_token stops the container and returns exit 130.

    AGT-07 axis (a) adds three more, and they are all about WHAT THE
    SANDBOX PERMITS, never about whether a human was asked:

    * ``containment`` supplies a resolved :class:`ContainmentPolicy` instead
      of letting this function resolve one;
    * ``readonly_paths`` overrides the default read-only subtrees.
      ``None`` keeps :data:`DEFAULT_READONLY_SUBPATHS`; ``[]`` genuinely
      disables them;
    * ``writable_roots`` declares which subtrees are writable. Non-empty
      makes the workspace mount itself read-only.

    P0/W1 adds two more, and they are about the OUTPUT rather than the
    boundary:

    * ``purpose`` declares what this run is FOR. It selects the ingress
      output cap (``execution.ingress.cap_for``): the default
      :data:`~execution.ingress.OUTPUT_CAP_BYTES` for ordinary tool
      commands, and the raised
      :data:`~execution.ingress.VERIFICATION_OUTPUT_CAP_BYTES` when the
      caller passes ``"verification"``. The verifier is how this harness
      diagnoses a failure, and a truncated traceback is not a diagnostic.
      An UNRECOGNISED purpose resolves to the DEFAULT cap, so a typo can
      never widen anything.
    * ``secrets`` are already-known literal secret values to redact on top
      of the pattern-based pass. Empty by default; the environment scrub
      (``_docker_environment``) is what actually keeps them out of the
      child, so this is a second line, not the first.

    Every returned :class:`ExecutionResult` passes through
    :func:`execution.ingress.seal_streams`, which is the single subprocess-
    output ingress: cap first (a flood cannot exhaust the redactor), then
    ``shared.security.redact_text``, fail-closed to
    ``(output withheld: redaction unavailable)`` if the redactor raises or
    returns a non-string. This is what makes "no subprocess byte reaches a
    tool result without passing the ingress redaction" true by
    construction rather than by inspection.

    The resolved policy is applied to the argv, re-checked by
    :func:`assert_sandbox_argv_isolated` with the declared writable roots,
    and recorded in the call's trace event, so "which mounts did this command
    actually get" is answerable from the trace.

    Returns ExecutionResult; timeout yields exit_code=124 and timed_out=True
    (GNU timeout convention, matching the local stub). Raises
    SandboxUnavailableError if docker is unusable — never silently runs
    unsandboxed.
    """
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError("timeout_s must be a positive number")
    if cancellation_token is not None and cancel_event is not None:
        raise ValueError("pass cancel_event or cancellation_token, not both")
    token = cancellation_token if cancellation_token is not None else cancel_event
    if _token_cancelled(token):
        return _sealed_result(
            ExecutionResult(130, "", "[sandbox] cancelled", False), purpose
        )
    if not Path(repo_path).is_dir():
        raise FileNotFoundError(f"repo_path is not a directory: {repo_path}")
    if not docker_available():
        raise SandboxUnavailableError(
            "docker daemon not reachable; refusing to run unsandboxed"
        )
    repo_abs = str(Path(repo_path).expanduser().resolve(strict=True))
    output_cap = cap_for(purpose)
    # P1/W1: every phase of one call is timed, because an unreported latency is
    # the defect this wave is fixing. `_phases` rides the ExecutionResult as an
    # additive attribute (`ExecutionResult` is shared/types.py's and has four
    # fields, none of which is a duration), so no shared type changes and no
    # existing consumer sees a new required field.
    _phases: Dict[str, float] = {}
    _mark = time.perf_counter
    _t0 = _mark()
    image = ensure_image(repo_abs)
    _phases["image"] = _mark() - _t0
    if _token_cancelled(token):
        return _sealed_result(
            ExecutionResult(130, "", "[sandbox] cancelled", False), purpose
        )
    _t0 = _mark()
    _maybe_reap()
    _phases["reap"] = _mark() - _t0

    js_vol: Optional[str] = None
    _t0 = _mark()
    if _detect_repo_language(repo_abs) == "js":
        js_vol = _js_deps_volume(repo_abs, image)
    _phases["js_deps_volume"] = _mark() - _t0
    if _token_cancelled(token):
        return _sealed_result(
            ExecutionResult(130, "", "[sandbox] cancelled", False), purpose
        )

    _t0 = _mark()
    policy = containment or resolve_containment(
        repo_abs,
        readonly_paths=readonly_paths,
        writable_roots=writable_roots,
        allow_network=allow_network,
    )
    if allow_network and not policy.network_enabled:
        # An explicit caller declaration always wins over a policy that did
        # not carry it; the receipt names which declaration was used.
        policy = ContainmentPolicy(
            repo_path=policy.repo_path,
            readonly_subpaths=policy.readonly_subpaths,
            writable_roots=policy.writable_roots,
            network_enabled=True,
            network_declaration="caller",
            declared_hosts=policy.declared_hosts,
            notes=policy.notes,
        )
    if (
        policy.repo_path
        and _win_to_docker(policy.repo_path).rstrip("/").casefold()
        != _win_to_docker(repo_abs).rstrip("/").casefold()
    ):
        raise ValueError(
            "the containment policy names a different repository than the "
            "one being executed; a receipt must describe the call it governs"
        )
    readonly_mounts = readonly_overlays(policy)
    writable_mounts = writable_overlays(policy)
    applied = (*readonly_mounts, *writable_mounts)
    _phases["containment"] = _mark() - _t0

    _t0 = _mark()
    full_argv = [
        "docker",
        *_docker_run_args(
            image,
            repo_abs,
            timeout_s,
            policy.network_enabled,
            env,
            mem_limit,
            cpu_limit,
            pids_limit,
            js_deps=js_vol,
            workspace_readonly=policy.workspace_readonly,
            readonly_mounts=readonly_mounts,
            writable_mounts=writable_mounts,
        ),
        "bash",
        "-c",
        command,
    ]
    # Pre-spawn isolation gate: refuse the invocation outright if it would
    # mount a runtime socket or a host secret directory, add a capability, or
    # share a host namespace. This is a fail-closed static check, so a future
    # change to the argv builder cannot quietly weaken the sandbox. The
    # declared writable roots travel with it, so a read-write in-workspace
    # mount is allowed exactly where the containment policy said so.
    assert_sandbox_argv_isolated(
        full_argv[1:], repo_abs, writable_paths=policy.writable_roots
    )
    # Pin the artifact source: record the immutable digest of the image the
    # container will actually run, so a run receipt can name it and a mutable
    # local tag cannot be re-pointed unnoticed between runs.
    image_ref = pin_image(image)
    name = _container_name(full_argv)
    _emit_trace(
        repo_abs,
        command,
        timeout_s,
        allow_network=policy.network_enabled,
        image=image_ref,
        mem_limit=mem_limit,
        cpu_limit=cpu_limit,
        pids_limit=pids_limit,
        egress_allowlist=list(policy.declared_hosts) if policy.network_enabled else [],
        containment=containment_receipt(policy, applied=applied),
        network_availability=sandbox_network_availability(
            policy.network_enabled,
            declared_hosts=policy.declared_hosts,
        ),
    )
    _phases["argv_and_trace"] = _mark() - _t0
    _t0 = _mark()
    try:
        proc = subprocess.Popen(
            full_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_environment(),
        )
    except OSError as exc:
        raise SandboxUnavailableError(f"docker run could not start: {exc}") from exc
    collectors_started = False
    try:
        stdout_capture, stderr_capture, collectors = _start_output_collectors(
            proc, output_cap
        )
        collectors_started = True
        started = time.monotonic()
        cancelled = False
        timed_out = False
        while proc.poll() is None:
            if _token_cancelled(token):
                cancelled = True
                _cleanup_container(proc, name)
                break
            if time.monotonic() - started >= float(timeout_s):
                timed_out = True
                _cleanup_container(proc, name)
                break
            time.sleep(0.05)
        if cancelled or timed_out:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            _join_output_collectors(collectors)
            stdout = stdout_capture.value()
            stderr = stderr_capture.value()
            if cancelled:
                _emit_trace_result(repo_abs, 130, timed_out=False)
                _phases["container"] = _mark() - _t0
                return _with_phases(
                    ExecutionResult(
                        exit_code=130,
                        stdout=_truncate(stdout, output_cap),
                        stderr=_truncate(stderr + "\n[sandbox] cancelled", output_cap),
                        timed_out=False,
                    ),
                    _phases,
                    purpose,
                )
            _emit_trace_result(repo_abs, TIMEOUT_EXIT_CODE, timed_out=True)
            _phases["container"] = _mark() - _t0
            return _with_phases(
                ExecutionResult(
                    exit_code=TIMEOUT_EXIT_CODE,
                    stdout=_truncate(stdout, output_cap),
                    stderr=_truncate(
                        stderr + f"\n[sandbox] timed out after {timeout_s}s",
                        output_cap,
                    ),
                    timed_out=True,
                ),
                _phases,
                purpose,
            )
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            _cleanup_container(proc, name)
            raise RuntimeError(
                "docker run did not exit after process completion"
            ) from exc
        _join_output_collectors(collectors)
        exit_code = proc.returncode if proc.returncode is not None else 1
        stdout = stdout_capture.value()
        stderr = stderr_capture.value()
        if exit_code != 0 and _docker_unavailable_error(stderr):
            _cleanup_container(proc, name)
            raise SandboxUnavailableError(
                f"docker daemon became unavailable: {_seal_error_text((stderr or stdout)[:1000])}"
            )
        _emit_trace_result(repo_abs, exit_code, timed_out=False)
        _phases["container"] = _mark() - _t0
        return _with_phases(
            ExecutionResult(
                exit_code=exit_code,
                stdout=_truncate(stdout, output_cap),
                stderr=_truncate(stderr, output_cap),
                timed_out=False,
            ),
            _phases,
            purpose,
        )
    except BaseException:
        _cleanup_container(proc, name)
        if collectors_started:
            _join_output_collectors(locals().get("collectors", []))
        raise


# -- unified tracing helpers (shared.tracing) ------------------------------


def _with_phases(
    result: ExecutionResult,
    phases: Dict[str, float],
    purpose: str = "",
) -> ExecutionResult:
    """Seal one call's output and attach its per-phase timing, additively.

    **The order is load-bearing and is the first thing to get wrong here.**
    :func:`execution.ingress.seal_streams` returns a NEW dataclass - it rebuilds
    field-by-field so no field is dropped, which is the right call for a shared
    type other owners construct. That rebuild also DROPS instance attributes, so
    stamping the timing before sealing silently discards it. The timing is
    therefore applied to whatever object the ingress returned, including the
    ``REDACTION_UNAVAILABLE`` substitute, because a call whose timing cannot be
    read is the same defect as a call whose timing was never measured.

    ``ExecutionResult`` has four fields (``exit_code``, ``stdout``, ``stderr``,
    ``timed_out``) and none of them is a duration, and it is
    ``shared/types.py``'s dataclass, so the timing rides as an instance
    attribute rather than as a field. ``execution.flake_gate.attach_evidence``
    set the precedent for exactly this route.

    Two published numbers, and they are not interchangeable:

    * ``result.elapsed_s`` - TOTAL wall time of the call, spawn included.
    * ``result.container_s`` - the spawn-to-exit span, which INCLUDES the
      caller's own command runtime.
    * ``result.overhead_s`` - ``elapsed_s - container_s``: everything the
      sandbox cost that was not the command. This is the number a latency rail
      needs, computed here so no consumer has to know which phases to add up.

    A phase the call did not reach is ABSENT from the dict rather than recorded
    as ``0.0``. A zero reads as "we measured it and it was free"; a missing key
    reads as "we did not measure it". Same rule as every other absent value in
    this repo.
    """
    sealed = _sealed_result(result)
    try:
        if "container" in phases:
            container = float(phases.get("container") or 0.0)
            elapsed = sum(float(value) for value in phases.values())
            sealed.phases = {
                key: round(float(value), 6) for key, value in phases.items()
            }
            sealed.container_s = round(container, 6)
            sealed.overhead_s = round(max(0.0, elapsed - container), 6)
            sealed.elapsed_s = round(elapsed, 6)
    except (AttributeError, TypeError, ValueError):
        # A timing annotation must never change whether a command ran or
        # whether its output was sealed.
        pass
    return sealed


def _trace_task_id(repo_abs: str) -> str:
    """Task id for the unified trace stream, from the mounted repo path.

    The harness mounts logs/{task_id}/work (or .../pristine): the task
    id is the parent directory's name. Backslashes are NORMALIZED to
    forward slashes first (Windows hosts pass native paths — rejecting
    them outright would disable execution-layer tracing on Win32, which
    is exactly what happened before the fix; the traversal hazard is
    caught by the '..'/'.' component check on the normalized split, and
    win32 backslash-join can never smuggle a traversal past it). The
    extracted segment then goes through shared.tracing.safe_segment (the
    tracer's own gate). A repo mounted from anywhere else, or any
    hostile shape, yields "" (emit no-ops). Never raises.
    """
    try:
        from shared.tracing import safe_segment

        raw = str(repo_abs)
        # normalize separators for STRUCTURE parsing (Windows hosts pass
        # native backslash paths — rejecting them outright would disable
        # execution-layer tracing on Win32), but the task-id SEGMENT
        # itself must not contain a backslash (that would be a separator
        # smuggled into one segment; safe_segment also rejects it).
        parts = [p for p in raw.replace("\\", "/").split("/") if p]
        if ".." in parts or "." in parts:
            return ""
        p = Path(raw.replace("\\", "/"))
        if p.name not in ("work", "pristine"):
            return ""
        tid = p.parent.name or ""
        return tid if safe_segment(tid) else ""
    except Exception:
        return ""


def _emit_trace(
    repo_abs: str,
    command: str,
    timeout_s: int,
    *,
    allow_network: bool = False,
    image: str = "",
    mem_limit: str = "",
    cpu_limit: float = 0.0,
    pids_limit: int = 0,
    egress_allowlist: Optional[List[str]] = None,
    containment: Optional[Dict[str, Any]] = None,
    network_availability: Optional[Dict[str, Any]] = None,
) -> None:
    """Trace the sandbox call start and its explicit policy inputs.

    ``containment`` is axis (a) as a receipt, and it is emitted as its own
    field rather than folded into ``allow_network``: a reader asking "was
    this contained" and a reader asking "was anyone asked" are different
    questions, and a single blended flag is what made them one.

    ``network_availability`` is a third field for the same reason: "a bridge
    is attached" and "this run can actually use the network" are different
    facts, and conflating them is how an offline run reports a network
    failure as a container bug.
    """
    try:
        from shared import tracing

        tid = _trace_task_id(repo_abs)
        if tid:
            tracing.emit(
                "execution",
                "sandbox_call",
                task_id=tid,
                command=(command or "")[:200],
                timeout_s=timeout_s,
                allow_network=bool(allow_network),
                image=image,
                egress_allowlist=list(egress_allowlist or []),
                mem_limit=mem_limit,
                cpu_limit=cpu_limit,
                pids_limit=pids_limit,
                containment=dict(containment or {}),
                network_availability=dict(network_availability or {}),
            )
    except Exception:
        pass


def _emit_trace_result(repo_abs: str, exit_code: int, timed_out: bool) -> None:
    """Trace the sandbox call result (best-effort; never raises)."""
    try:
        from shared import tracing

        tid = _trace_task_id(repo_abs)
        if tid:
            tracing.emit(
                "execution",
                "sandbox_result",
                task_id=tid,
                exit_code=exit_code,
                timed_out=timed_out,
            )
    except Exception:
        pass


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
    parser.add_argument(
        "--network", action="store_true", help="allow network for this command"
    )
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
