"""Windows-safe atomic file primitives for the runtime.

Assumptions: single writer per file. The scheduler gives every task its own
process and its own log directory, so checkpoint/ledger files have exactly
one writer; tmp-file + os.replace is then sufficient for crash consistency.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def now_epoch() -> float:
    """Current wall-clock time in fractional seconds since the epoch."""
    return time.time()


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (ms precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_dir(path: str | os.PathLike) -> Path:
    """Create ``path`` (and parents) if missing; return it as a Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_json(path: str | os.PathLike, obj: Any) -> None:
    """Atomically write ``obj`` as JSON to ``path``.

    Writes a unique temp file in the same directory, fsyncs, then
    os.replace()s it onto the target. os.replace is atomic on the same
    volume (including Windows), so a reader sees either the old or the
    new content — never a torn file. Assumes a single writer per path.

    The final replace is retried (up to 3 attempts, 50ms apart) on
    PermissionError: on Windows a supervisor concurrently READING the
    target (no FILE_SHARE_DELETE in the CRT's open) makes the replace
    transiently fail with access denied. Found live by the Round-7 soak
    (5 workers in 3600 died on it before this retry); a sharing
    violation clears when the short-lived reader closes, so a bounded
    retry is safe and keeps single-writer semantics. Anything else
    (or a replace still denied after 150ms) raises as before.
    """
    p = Path(path)
    ensure_dir(p.parent)
    last_err: Optional[BaseException] = None
    for _ in range(3):
        fd, tmp_name = tempfile.mkstemp(
            dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, p)
            return
        except PermissionError as exc:  # Windows sharing violation, retry
            last_err = exc
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    raise last_err  # type: ignore[misc]


def read_json(path: str | os.PathLike) -> Any:
    """Read JSON from ``path``. Raises on missing/corrupt input."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_json_or_none(path: str | os.PathLike) -> Optional[Any]:
    """Read JSON from ``path``; None on missing or corrupt input."""
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def append_jsonl(path: str | os.PathLike, obj: Any) -> None:
    """Append one record to a JSONL journal (single writer per file assumed)."""
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()


def read_jsonl(path: str | os.PathLike) -> list:
    """Read all records from a JSONL file; missing file -> [].

    Skips blank or corrupt lines instead of failing the whole read — a
    crash mid-append can leave one partial final line.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def is_pid_alive(pid: Optional[int]) -> bool:
    """Best-effort liveness probe for a pid.

    On Windows uses OpenProcess — os.kill(pid, 0) on Windows TERMINATES the
    process (sends a signal that kills), so it must never be used to probe.
    PID reuse can false-positive; only use for stale-checkpoint heuristics,
    never for correctness decisions.
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
