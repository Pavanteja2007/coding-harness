"""P4/W1 T2.W1.5.4 — per-session PORT allocation, and why it has to exist here.

Two end-to-end checks that each start a development server on ``localhost:
8000`` collide, and the collision looks exactly like a flaky test: the second
server fails to bind, the check fails, and a reader concludes the feature is
broken. Nothing in the sandbox published a port at all before this phase (there
is no ``--publish`` anywhere in ``execution/sandbox.py``), so a repository that
served on a fixed port had exactly one safe port and no way to learn which.

This module allocates a **disjoint block of host ports per session**, probes a
port for real reachability before handing it out, and REPORTS the allocation on
every receipt, because an allocation nobody can read is an allocation nobody can
diagnose.

--------------------------------------------------------------------------
WHY THE GUARD HOME IGNORES THE ENVIRONMENT
--------------------------------------------------------------------------

Phase 3a/W2 measured this exact defect for the session-tree guard: an allocator
that resolved its lock home from ``NEO_HOME``/``HARNESS_HOME`` let a second
session relocate the guard and then admit a port the first session already held
— and every process honestly reported ``held``, because each was reading a
different file. :func:`guard_home` therefore honours an EXPLICIT ``home=``
argument and otherwise reads **no environment variable at all**. The port block
of a session must not be a thing the ambient environment can move.

This is asserted structurally (``test_the_module_reads_no_environment_at_all``
in ``execution/test_p4w1_t2_pins.py``), because a behavioural env-var list
cannot cover a read whose name nobody guessed.

--------------------------------------------------------------------------
ALLOCATING A PORT IS NOT BINDING ONE
--------------------------------------------------------------------------

Handing out a number that is free at allocation time and taken by the time the
container starts is the classic TOCTOU. :func:`probe_port_free` therefore binds
the port itself, closes it, and reports what happened; the answer is reported
as ``verified_by_bind`` or, when the probe could not run,
``verified: false`` with the reason. A port reported as verified-free and a port
whose probe could not run are DIFFERENT facts and the receipt keeps them apart.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "GUARD_DIRNAME",
    "MAX_PUBLISHED_PORTS",
    "PORT_BASE_MAX",
    "PORT_BASE_MIN",
    "PORT_BLOCK_SIZE",
    "PORT_HOST",
    "PORT_PROBE_TIMEOUT_S",
    "PortAllocationError",
    "PortBlock",
    "acquire_port_block",
    "active_blocks",
    "allocate_port",
    "block_for_session",
    "blocks_overlap",
    "guard_home",
    "next_free_port",
    "port_report",
    "probe_port_free",
    "release_port_block",
    "render_port_report",
]

#: Where the cross-process allocation records live. Deliberately NOT under
#: ``NEO_HOME``: see the module docstring.
GUARD_DIRNAME = "neo-port-blocks"

#: The allocation window. A narrow window raises collision probability; a wide
#: one makes the receipts unreadable and walks into privileged ports. This band
#: is the ephemeral-user range Docker and Node both leave alone on every
#: platform this project supports.
PORT_BASE_MIN = 41_000
PORT_BASE_MAX = 49_999
PORT_BLOCK_SIZE = 64

PORT_PROBE_TIMEOUT_S = 0.25

#: Loopback only. A published port that binds ``0.0.0.0`` exposes a
#: container's service to the whole LAN, and this module's only consumer is a
#: check running on the same machine.
PORT_HOST = "127.0.0.1"

#: A bound on how many ports one container may publish. See
#: ``execution/sandbox.py::assert_sandbox_argv_isolated``, which enforces it at
#: the argv level; this constant is the value both halves agree on.
MAX_PUBLISHED_PORTS = 4


class PortAllocationError(RuntimeError):
    """A port block could not be allocated. Never degrades to a shared block."""


def guard_home(home: Optional[str] = None) -> Path:
    """Return the directory holding the port-allocation records.

    An explicit ``home=`` is honoured. With no explicit home the environment is
    **not consulted at all** — not ``NEO_HOME``, not ``HARNESS_HOME``, not
    ``TMPDIR``. An ambient variable that can relocate the guard is an ambient
    variable that can hide one session's allocation from another.
    """
    if home:
        return Path(home).expanduser()
    return Path(tempfile.gettempdir()) / GUARD_DIRNAME


def _machine_token() -> str:
    """A stable per-machine token, used only to spread blocks across machines.

    ``os.uname`` does not exist on Windows, so the hostname is read through
    ``platform.node()`` — the stdlib's own cross-platform answer — rather than
    through a probe that raises on half the platforms this project supports.
    """
    try:
        import platform

        node = str(platform.node() or "")
    except Exception:  # pragma: no cover - defensive
        node = ""
    material = f"{os.name}|{node}|{tempfile.gettempdir()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def _block_path(home: Path, session_id: str) -> Path:
    return home / f"block-{_session_digest(session_id)[:16]}.json"


def _window_size() -> int:
    return max(PORT_BLOCK_SIZE, PORT_BASE_MAX - PORT_BASE_MIN + 1)


def _block_base(digest: str, token: str) -> int:
    """Deterministically place a session's block inside the window.

    Deterministic so two processes deriving a block for the same session agree
    without coordinating; spread by the session digest so two sessions rarely
    collide; and multiplied through by the machine token so two machines sharing
    a logs root do not march in lockstep.
    """
    spread = _window_size()
    material = hashlib.sha256(f"{digest}|{token}".encode("utf-8")).hexdigest()
    raw = int(material[:12], 16)
    usable_blocks = max(1, spread // PORT_BLOCK_SIZE)
    block_index = raw % usable_blocks
    return PORT_BASE_MIN + block_index * PORT_BLOCK_SIZE


def blocks_overlap(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    """Whether two inclusive port ranges overlap."""
    return left[0] <= right[1] and right[0] <= left[1]


@dataclass(frozen=True)
class PortBlock:
    """One session's reserved window of host ports."""

    session_id: str
    base: int
    size: int = PORT_BLOCK_SIZE
    host: str = PORT_HOST
    reason: str = ""
    allocated_at: float = 0.0
    pid: int = 0
    released: bool = False

    @property
    def ports(self) -> Tuple[int, ...]:
        return tuple(range(self.base, self.base + self.size))

    @property
    def span(self) -> Tuple[int, int]:
        """The inclusive range, for overlap arithmetic."""
        return (self.base, self.base + self.size - 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "host": self.host,
            "base": self.base,
            "size": self.size,
            "span": [self.span[0], self.span[1]],
            "reason": self.reason,
            "allocated_at": self.allocated_at,
            "pid": self.pid,
            "released": bool(self.released),
            "port_count": len(self.ports),
        }


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(path))


def active_blocks(*, home: Optional[str] = None) -> Tuple[PortBlock, ...]:
    """Every block recorded on this machine, for any session.

    Read-only and total: an unreadable or corrupt record is reported as an
    ``released`` block rather than skipped, so a reader can see that a record
    exists and could not be read instead of seeing nothing.
    """
    directory = guard_home(home)
    if not directory.is_dir():
        return ()
    found: List[PortBlock] = []
    for path in sorted(directory.glob("block-*.json")):
        payload = _read_json(path)
        if payload is None:
            found.append(
                PortBlock(
                    session_id=f"<unreadable:{path.name}>",
                    base=0,
                    reason="the allocation record exists but could not be read",
                    released=True,
                )
            )
            continue
        found.append(
            PortBlock(
                session_id=str(payload.get("session_id") or ""),
                base=int(payload.get("base") or 0),
                size=int(payload.get("size") or PORT_BLOCK_SIZE),
                host=str(payload.get("host") or PORT_HOST),
                reason=str(payload.get("reason") or ""),
                allocated_at=float(payload.get("allocated_at") or 0.0),
                pid=int(payload.get("pid") or 0),
                released=bool(payload.get("released")),
            )
        )
    return tuple(found)


def acquire_port_block(
    session_id: str,
    *,
    size: int = PORT_BLOCK_SIZE,
    reason: str = "",
    home: Optional[str] = None,
    now: Optional[float] = None,
    conflict_check: bool = True,
    search: bool = True,
) -> PortBlock:
    """Reserve a window for ``session_id`` and record it.

    The derived window can collide: the base is a hash of the session digest,
    and a hash can repeat. Two sessions sharing a window is precisely the
    cross-session leak this module exists to prevent, so the allocator never
    returns an overlapping block.

    With ``search=True`` (the default) it **searches forward through the window**
    for the first block that overlaps no LIVE block and takes it. That is why
    the refusal below is reserved for the genuine case — the window is full —
    rather than firing on a hash collision, which would mean a second session
    simply could not run a server.

    Raises :class:`PortAllocationError` when the whole window is exhausted.
    """
    text = str(session_id or "").strip()
    if not text:
        raise PortAllocationError(
            "a port block needs a session id; an unnamed session cannot be told "
            "apart from another unnamed session, which is the collision"
        )
    if int(size) < 1:
        raise PortAllocationError(f"port block size must be >= 1, got {size!r}")

    directory = guard_home(home)
    directory.mkdir(parents=True, exist_ok=True)
    digest = _session_digest(text)
    derived = _block_base(digest, _machine_token())
    stamp = float(now if now is not None else time.time())

    live = (
        [
            block
            for block in active_blocks(home=home)
            if not block.released and block.base and block.session_id != text
        ]
        if conflict_check
        else []
    )

    candidates: List[int] = []
    span_count = max(1, _window_size() // PORT_BLOCK_SIZE)
    offsets = [0, *range(1, span_count)] if search else [0]
    for offset in offsets:
        base = derived + offset * PORT_BLOCK_SIZE
        if base + PORT_BLOCK_SIZE - 1 > PORT_BASE_MAX:
            continue
        span = (base, base + int(size) - 1)
        if not any(blocks_overlap(span, block.span) for block in live):
            candidates.append(base)
            break

    if not candidates:
        first_span = (derived, derived + int(size) - 1)
        raise PortAllocationError(
            f"no free port window left for session {text!r}. The derived window "
            f"{first_span[0]}-{first_span[1]} overlaps "
            f"{len(live)} live block(s) and the search reached the end of "
            f"{PORT_BASE_MIN}-{PORT_BASE_MAX}. Two sessions sharing a window is "
            f"the cross-session collision this allocator exists to prevent, so "
            f"it refuses rather than overlapping; raise the window or release a "
            f"session."
        )

    block = PortBlock(
        session_id=text,
        base=candidates[0],
        size=int(size),
        reason=str(reason or ""),
        allocated_at=stamp,
        pid=os.getpid(),
    )
    _write_json(_block_path(directory, text), block.to_dict())
    return block


def release_port_block(
    session_id: str,
    *,
    home: Optional[str] = None,
) -> bool:
    """Mark a session's block released. Idempotent; ``True`` if it existed."""
    directory = guard_home(home)
    path = _block_path(directory, session_id)
    payload = _read_json(path)
    if payload is None:
        return False
    payload["released"] = True
    payload["released_at"] = time.time()
    _write_json(path, payload)
    return True


def block_for_session(
    session_id: str,
    *,
    home: Optional[str] = None,
) -> Optional[PortBlock]:
    """Read a session's block, or ``None`` when it has none.

    A released block is returned as ``released=True`` rather than as ``None``:
    "this session had a window and gave it up" and "this session never had one"
    are different facts.
    """
    path = _block_path(guard_home(home), session_id)
    payload = _read_json(path)
    if payload is None:
        return None
    return PortBlock(
        session_id=str(payload.get("session_id") or session_id),
        base=int(payload.get("base") or 0),
        size=int(payload.get("size") or PORT_BLOCK_SIZE),
        host=str(payload.get("host") or PORT_HOST),
        reason=str(payload.get("reason") or ""),
        allocated_at=float(payload.get("allocated_at") or 0.0),
        pid=int(payload.get("pid") or 0),
        released=bool(payload.get("released")),
    )


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def probe_port_free(port: int, *, host: str = PORT_HOST) -> Dict[str, Any]:
    """Bind ``port`` and report whether it was free.

    Returns a dict with ``verified: bool`` and a reason. ``verified: False`` is
    NOT "the port is taken" — it is "the probe did not run", which is a
    different fact and the receipt names which one applies.
    """
    number = int(port)
    receipt: Dict[str, Any] = {
        "port": number,
        "host": host,
        "verified": False,
        "free": False,
        "verified_by_bind": False,
        "reason": "",
    }
    if not (1 <= number <= 65535):
        receipt["reason"] = f"port {number} is outside the valid range 1-65535"
        return receipt
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(PORT_PROBE_TIMEOUT_S)
        sock.bind((host, number))
        receipt["verified"] = True
        receipt["free"] = True
        receipt["verified_by_bind"] = True
        receipt["reason"] = f"bound {host}:{number} successfully, so it was free"
        return receipt
    except OSError as exc:
        code = getattr(exc, "errno", None)
        taken = code in {errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL}
        receipt["free"] = not taken
        receipt["reason"] = (
            f"bind on {host}:{number} failed ({type(exc).__name__}: {exc}); "
            + ("the port is in use" if taken else "the probe could not run")
        )
        return receipt
    finally:
        try:
            sock.close()
        except OSError:  # pragma: no cover - defensive
            pass


def next_free_port(
    block: PortBlock,
    *,
    host: str = PORT_HOST,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the first port in ``block`` that probes free.

    The receipt names WHICH port, WHICH block, which session, how many were
    probed, and whether the probe itself worked. A caller that gets ``None``
    back needs all of that to tell "every port in the window was taken" from
    "the probe could not bind anything".
    """
    ports = list(block.ports)
    ceiling = int(limit) if limit is not None else len(ports)
    attempts: List[Dict[str, Any]] = []
    chosen: Optional[int] = None
    for port in ports[: max(0, ceiling)]:
        probe = probe_port_free(port, host=host)
        attempts.append(probe)
        if probe.get("free"):
            chosen = port
            break
    return {
        "session_id": block.session_id,
        "block": block.to_dict(),
        "host": host,
        "port": chosen,
        "allocated": chosen is not None,
        "probed": len(attempts),
        "window_size": len(ports),
        "verified_by_bind": any(item.get("verified_by_bind") for item in attempts),
        "reason": (
            f"port {chosen} in session block "
            f"{block.base}-{block.base + block.size - 1} probed free"
            if chosen is not None
            else (
                f"no free port in session block "
                f"{block.base}-{block.base + block.size - 1} after probing "
                f"{len(attempts)} port(s); last probe said: "
                f"{attempts[-1].get('reason') if attempts else 'nothing was probed'}"
            )
        ),
        "attempts": attempts[:8],
    }


def allocate_port(
    session_id: str,
    *,
    size: int = PORT_BLOCK_SIZE,
    reason: str = "",
    home: Optional[str] = None,
    acquire: bool = True,
) -> Dict[str, Any]:
    """Acquire (or read) a session's block and hand out one free port.

    The one-call shape a check runner uses. Everything the caller needs to
    diagnose a port problem later is on the returned receipt, because the
    alternative is re-deriving it from a container name that has already gone.
    """
    block = (
        acquire_port_block(session_id, size=size, reason=reason, home=home)
        if acquire
        else block_for_session(session_id, home=home)
    )
    if block is None or not block.base:
        return {
            "session_id": str(session_id),
            "allocated": False,
            "port": None,
            "reason": (
                f"session {session_id!r} has no port block and none was "
                f"acquired; read it with acquire=True or pass an explicit home"
            ),
            "block": None,
        }
    outcome = next_free_port(block)
    outcome["acquired"] = bool(acquire)
    return outcome


def port_report(
    session_id: Optional[str] = None,
    *,
    home: Optional[str] = None,
) -> Dict[str, Any]:
    """The full allocation picture: every block, plus the named session's."""
    blocks = active_blocks(home=home)
    live = [block for block in blocks if not block.released and block.base]
    overlaps: List[Dict[str, Any]] = []
    for index, left in enumerate(live):
        for right in live[index + 1 :]:
            if blocks_overlap(left.span, right.span):
                overlaps.append(
                    {
                        "left": left.session_id,
                        "right": right.session_id,
                        "span": list(left.span),
                    }
                )
    mine = block_for_session(session_id, home=home) if session_id else None
    return {
        "guard_home": str(guard_home(home)),
        "reads_environment": False,
        "window": [PORT_BASE_MIN, PORT_BASE_MAX],
        "block_size": PORT_BLOCK_SIZE,
        "host": PORT_HOST,
        "blocks": [block.to_dict() for block in blocks],
        "live_block_count": len(live),
        "released_block_count": sum(1 for block in blocks if block.released),
        "unreadable_records": sum(
            1 for block in blocks if block.base == 0 and block.released
        ),
        "overlaps": overlaps,
        "collisions": len(overlaps),
        "session_block": mine.to_dict() if mine is not None else None,
        "note": (
            "an overlap between two LIVE session blocks is a cross-session "
            "collision: two end-to-end checks would be told to bind the same "
            "host port and the second would fail in a way that reads exactly "
            "like a flaky feature"
        ),
    }


def render_port_report(report: Dict[str, Any]) -> List[str]:
    """Render :func:`port_report` as plain lines a surface can print."""
    lines = [
        f"port allocation guard home : {report.get('guard_home')}",
        f"reads the environment     : {report.get('reads_environment')}",
        f"window                    : {report.get('window')}",
        f"block size                : {report.get('block_size')}",
        f"live blocks               : {report.get('live_block_count')}",
        f"released blocks           : {report.get('released_block_count')}",
        f"unreadable records        : {report.get('unreadable_records')}",
        f"cross-session collisions  : {report.get('collisions')}",
    ]
    for row in report.get("overlaps") or ():
        lines.append(
            f"  COLLISION {row.get('left')} / {row.get('right')} "
            f"over {row.get('span')}"
        )
    for block in report.get("blocks") or ():
        if not block.get("base"):
            lines.append(f"  unreadable record: {block.get('session_id')}")
            continue
        state = "released" if block.get("released") else "live"
        span = block.get("span") or []
        lines.append(
            f"  {state:<8} {block.get('session_id')}: "
            f"{span[0] if span else '?'}-{span[1] if len(span) > 1 else '?'}"
            f"  ({block.get('reason') or 'no reason recorded'})"
        )
    session_block = report.get("session_block")
    if session_block:
        lines.append(
            f"this session's block      : {session_block.get('base')}"
            f"+{session_block.get('size')}"
        )
    return lines


def _reserved_ports() -> Sequence[int]:  # pragma: no cover - documentation
    """Ports this module deliberately never hands out.

    Published as prose rather than a table because there is nothing to enforce:
    the window itself excludes them, which is the enforcement.
    """
    return (22, 25, 53, 110, 143, 443, 445, 3306, 5432, 6379, 9200, 11211)
