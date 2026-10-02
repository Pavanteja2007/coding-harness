"""Per-task resume bookkeeping for the scheduler (runtime-owned).

Key design: the runtime does NOT duplicate Terminal 1's per-step state
(state.json, Boundary 4). Instead the worker watches that file's
completed_steps, and persists its own small checkpoint describing where
run_task got to and what it produced. On resume, the worker passes
resume=True in task.config, and Terminal 1's harness is expected to read
its own state.json and skip completed steps (the resume contract; see
runtime/AGENTS.md "What Terminal 1 needs to provide").

Files, under logs/{task_id}/runtime/:
  checkpoint.json â€” the checkpoint itself (atomic, whole-file)
  heartbeat.json  â€” worker liveness, rewritten every ~5s while running
  events.jsonl    â€” append-only worker event journal (start, checkpoint,
                    resume, approval, finish; see worker.py)

This module also owns the two durable primitives a three-way rewind needs, both
append-only and both derived from the same journal authority a resume uses:

  ConversationJournal â€” one row per retained conversation turn plus the
    compaction receipts, so a conversation can be rebuilt exactly (not
    approximated) for a conversation-only rewind or a compaction rollback.
  TurnFileState â€” per-turn file pre-images for the files a run actually
    changed, so a files-only rewind restores the exact pre-turn bytes without
    snapshotting the whole repository.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.security import redact_secrets

from .fsutil import (
    append_jsonl,
    atomic_write_json,
    now_epoch,
    now_iso,
    read_json_or_none,
)

_IDENTITY_VERSION = 1
_IDENTITY_FIELDS = (
    "identity_version",
    "task_id",
    "repository_identity",
    "request_identity",
    "revision_identity",
    "resume_namespace",
    "effort_identity",
)
_REVISION_CONFIG_KEYS = (
    "resume_revision",
    "base_revision",
    "repository_revision",
    "git_revision",
    "revision",
)
_EXCLUDED_REVISION_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}
_EXCLUDED_ROOT_REVISION_PARTS = {"logs"}


def canonical_repository(repo_path: str) -> str:
    """Return a stable absolute repository identity for checkpoint matching."""
    value = str(repo_path or "")
    if not value:
        return ""
    try:
        resolved = Path(value).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(os.path.abspath(os.path.expanduser(value)))
    return os.path.normcase(str(resolved))


def request_identity(request: str) -> str:
    """Return a non-secret SHA-256 identity for the exact task request."""
    payload = str(request or "").encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _git_output(root: Path, args: List[str]) -> Optional[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _tree_revision(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"missing")
        return "tree:" + digest.hexdigest()
    files: List[Path] = []
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if name not in _EXCLUDED_REVISION_PARTS
            and not (
                name in _EXCLUDED_ROOT_REVISION_PARTS
                and Path(directory).resolve() == root.resolve()
            )
        ]
        for name in names:
            candidate = Path(directory) / name
            relative_parts = candidate.relative_to(root).parts
            if not relative_parts or relative_parts[0] in _EXCLUDED_ROOT_REVISION_PARTS:
                continue
            if any(part in _EXCLUDED_REVISION_PARTS for part in relative_parts[1:]):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            files.append(candidate)
    for candidate in sorted(files, key=lambda item: item.as_posix()):
        try:
            relative = candidate.relative_to(root).as_posix()
            size = candidate.stat().st_size
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            with candidate.open("rb") as handle:
                remaining = min(size, 4 * 1024 * 1024)
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    digest.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            continue
    return "tree:" + digest.hexdigest()


def revision_identity(
    repo_path: str, config: Optional[Mapping[str, Any]] = None
) -> str:
    """Return a stable repository revision identity, including dirty state.

    Explicit task-config revisions win. Git repositories use HEAD, branch,
    status, and the working-tree diff; non-Git directories use a bounded file
    manifest so an uncommitted source change cannot silently resume old work.
    """
    values = dict(config or {})
    for key in _REVISION_CONFIG_KEYS:
        explicit = values.get(key)
        if explicit is not None and str(explicit).strip():
            payload = str(explicit).strip().encode("utf-8", errors="surrogatepass")
            return "explicit:" + hashlib.sha256(payload).hexdigest()
    repository = canonical_repository(repo_path)
    if not repository:
        return "empty"
    root = Path(repository)
    head = _git_output(root, ["rev-parse", "--verify", "HEAD"])
    if head:
        branch = _git_output(root, ["branch", "--show-current"]) or ""
        status = _git_output(root, ["status", "--porcelain=v1", "-z"]) or ""
        diff = (
            _git_output(
                root,
                ["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--"],
            )
            or ""
        )
        payload = "\0".join((head, branch, status, diff)).encode(
            "utf-8", errors="surrogatepass"
        )
        return "git:" + hashlib.sha256(payload).hexdigest()
    return _tree_revision(root)


def effort_identity(config: Optional[Mapping[str, Any]] = None) -> str:
    """Return the effort rung this run is identified by (AGT-08).

    Resuming a high-effort run at low effort is a lie about the run: the
    second half of the work would be produced by a model thinking less hard
    than the first half, and every cost/quality claim made about the whole
    would be false. So the rung joins the resume identity, and a mismatch
    takes the historical fail-closed path - a FRESH attempt, journalled -
    rather than quietly continuing at the new level.

    The default rung (``auto``) is a non-empty string on purpose: a checkpoint
    that never recorded an effort still matches an unconfigured run, and a
    checkpoint that recorded a REAL level never matches one. ``resolve_effort``
    is imported lazily so this module keeps no runtime capability import at
    module scope; a broken authority degrades to the raw configured string,
    which still distinguishes one rung from another.
    """
    raw = (config or {}).get("effort")
    try:
        from .model_capabilities import resolve_effort

        return str(resolve_effort(dict(config or {}))[0] or "auto")
    except Exception:
        text = str(raw or "").strip().lower()
        return text or "auto"


def checkpoint_identity(
    task_id: str,
    repo_path: str,
    request: str,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the durable identity tuple used to authorize a resume."""
    return {
        "identity_version": _IDENTITY_VERSION,
        "task_id": str(task_id or ""),
        "repository_identity": canonical_repository(repo_path),
        "request_identity": request_identity(request),
        "revision_identity": revision_identity(repo_path, config),
        "resume_namespace": str(dict(config or {}).get("resume_namespace") or ""),
        "effort_identity": effort_identity(config),
    }


def checkpoint_identity_matches(
    checkpoint: Optional[Mapping[str, Any]],
    identity: Optional[Mapping[str, Any]],
) -> bool:
    """Return whether a checkpoint carries the exact expected resume identity."""
    if not isinstance(checkpoint, Mapping) or not isinstance(identity, Mapping):
        return False
    try:
        version = int(checkpoint.get("identity_version", 0) or 0)
    except (TypeError, ValueError):
        return False
    if version != _IDENTITY_VERSION:
        return False
    return all(
        str(checkpoint.get(field, "")) == str(identity.get(field, ""))
        for field in _IDENTITY_FIELDS
        if field != "identity_version"
    )


class TaskCheckpoint:
    """Read/write the runtime-owned checkpoint for one task.

    Assumes one worker process per task at a time (scheduler guarantees
    this), writing under logs/{task_id}/runtime/.

    Checkpoint content:
      task_id            â€” id of the owning task
      attempt            â€” which attempt number this run is (0-based)
      started_at         â€” ISO ts of first start ever
      last_heartbeat     â€” ISO ts (informational; scheduler uses heartbeat.json)
      completed_steps    â€” steps reported complete at last checkpoint
                          (mirror of state.json's completed_steps)
      result             â€” last TaskResult dict (only if run_task returned)
      status             â€” "running" | "finished" | "killed" | "pending"
      identity_version   â€” checkpoint identity schema version
      repository_identity â€” canonical repository path
      request_identity    â€” SHA-256 of the exact request
      revision_identity   â€” Git/working-tree revision identity
      resume_namespace    â€” scheduler instance namespace
    """

    def __init__(self, runtime_dir: str) -> None:
        self.dir = Path(runtime_dir)
        self.path = self.dir / "checkpoint.json"
        self.heartbeat_path = self.dir / "heartbeat.json"
        self.events_path = self.dir / "events.jsonl"

    # -- lifecycle ------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        """Return the stored checkpoint dict, or None if none/corrupt."""
        data = read_json_or_none(self.path)
        return data if isinstance(data, dict) else None

    def save(self, cp: Dict[str, Any]) -> None:
        """Atomically persist the checkpoint dict for this task."""
        atomic_write_json(self.path, cp)

    def update(self, **fields: Any) -> Optional[Dict[str, Any]]:
        """Load-modify-save the checkpoint; returns the updated dict or
        None if no checkpoint exists yet."""
        cp = self.load()
        if cp is None:
            return None
        cp.update(fields)
        self.save(cp)
        return cp

    # -- heartbeat ------------------------------------------------------

    def beat(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """Write a fresh heartbeat (worker calls this every few seconds).

        The scheduler kills a worker whose heartbeat is older than
        max_wallclock_s (hang detection) â€” heartbeat freshness distinguishes
        a slow-but-alive worker from a hung one.
        """
        hb = {
            "ts": now_iso(),
            "epoch": now_epoch(),
            **(payload or {}),
        }
        atomic_write_json(self.heartbeat_path, hb)

    def heartbeat_age_s(self, attempt_token: Optional[str] = None) -> Optional[float]:
        """Return heartbeat age, optionally requiring the current attempt token."""
        heartbeat = read_json_or_none(self.heartbeat_path)
        if not isinstance(heartbeat, dict) or "epoch" not in heartbeat:
            return None
        if (
            attempt_token is not None
            and heartbeat.get("attempt_token") != attempt_token
        ):
            return None
        try:
            epoch = float(heartbeat["epoch"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(epoch):
            return None
        return now_epoch() - epoch

    # -- events ---------------------------------------------------------

    def log_event(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append an event to the task's worker journal (audit trail).

        Redacted at source, not at display time. This journal is written
        through the raw ``fsutil.append_jsonl`` while the same module already
        had a redacting writer (``_append_jsonl_durable``), wired only to
        ``compactions.jsonl`` and the turn pre-image ledger. That made the
        worker journal the one durable artifact in the runtime that could
        persist a credential verbatim -- and it holds approval
        ``error=str(exc)`` rows and a quota block, so it is the artifact most
        likely to be holding one.

        The value is redacted HERE, at the boundary, so the file is safe by
        construction rather than by a later scrub that a reader may not run.
        ``log_event`` never raises: a redaction failure must not be able to
        take a run's audit trail down, so the row records the failure in
        place of the detail.
        """
        try:
            payload: Any = redact_secrets(dict(data or {}))
        except Exception as exc:  # fail closed into the journal itself
            payload = {"detail_withheld": f"redaction failed: {type(exc).__name__}"}
        append_jsonl(
            self.events_path, {"ts": now_iso(), "event": event, "data": payload}
        )


# -- resume decision helpers (used by scheduler + worker) -----------------


def should_resume(
    config: Dict[str, Any],
    checkpoint: Optional[Dict[str, Any]],
    completed_steps: Optional[List[str]] = None,
    identity: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Apply progress, status, and optional identity authorities to resume.

    ``state.json`` supplies progress; the runtime checkpoint authorizes an
    unfinished relaunch. When ``identity`` is supplied, legacy or mismatched
    checkpoints are rejected fail-closed. Omitting it preserves the original
    helper contract for callers that only exercise the two legacy authorities.
    """
    if not config.get("resume", True) or checkpoint is None:
        return False
    if checkpoint.get("status") == "finished":
        return False
    if identity is not None and not checkpoint_identity_matches(checkpoint, identity):
        return False
    progress = (
        list(completed_steps)
        if completed_steps is not None
        else list(checkpoint.get("completed_steps") or [])
    )
    return bool(progress)


# -- rewind primitives -------------------------------------------------------
#
# A rewind must reproduce the exact pre-turn state, so nothing here is derived
# from a heuristic. The journal is the authority: turns.jsonl, the conversation
# journal, and the checkpoint are all re-derived from the rows that survive the
# requested turn boundary. Nothing is deleted - a rewind rotates the previous
# file aside so the pre-rewind evidence stays inspectable.

CONVERSATION_JOURNAL_NAME = "conversation.jsonl"
COMPACTION_JOURNAL_NAME = "compactions.jsonl"
TURN_FILE_STATE_DIR = "turn-files"


def _append_jsonl_durable(path: Path, record: Mapping[str, Any]) -> bool:
    """Append one redacted JSON row with an explicit flush and fsync.

    Redaction is not optional here: this journal holds full conversation turns
    and tool output, so it is exactly the class of durable artifact that must
    never persist a credential (the same reason ``trace.jsonl`` redacts).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    redact_secrets(dict(record)),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError):
        return False
    return True


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Return every well-formed JSON object; a torn tail is skipped honestly."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _rotate(path: Path) -> Optional[Path]:
    """Move ``path`` aside so a rewrite never destroys the previous evidence."""
    try:
        if not path.is_file():
            return None
        target = path.with_name(f"{path.name}.rewound-{time.time_ns()}")
        shutil.move(str(path), str(target))
        return target
    except OSError:
        return None


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> bool:
    """Rewrite a JSONL file atomically from a list of redacted rows."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(
                    json.dumps(
                        redact_secrets(dict(row)),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        return False
    return True


class ConversationJournal:
    """Append-only durable record of one run's rolling conversation.

    Three row kinds share the file:

    ``base``
        The seeded ``[system, user]`` frame, written once per run.
    ``turn``
        One retained conversation turn, content included, tagged with its
        journal ``seq`` and the run ``turn`` it belongs to.
    ``compaction``
        A compaction receipt: the journal sequences it dropped, the post-
        compaction handoff state, and the method that produced the summary.

    The live conversation is a deterministic fold of those rows: base + every
    ``turn`` row whose ``seq`` is not in the dropped set of an ACTIVE
    compaction. ``ConversationJournal.restore_compaction`` appends a ``restore``
    row, which deactivates one compaction and therefore un-drops exactly its
    sequences - the reversible-compaction contract, with no second authority.
    """

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)

    @property
    def path(self) -> Path:
        """Return the conversation journal path for this run."""
        return self.run_dir / CONVERSATION_JOURNAL_NAME

    def append(self, record: Mapping[str, Any]) -> bool:
        """Append one journal row durably; return whether it persisted."""
        return _append_jsonl_durable(self.path, record)

    def load(self) -> List[Dict[str, Any]]:
        """Return every well-formed journal row."""
        return _read_jsonl(self.path)

    def _base(self, rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
        for row in rows:
            if row.get("record") == "base":
                return [
                    {
                        "role": str(item.get("role") or "user"),
                        "content": str(item.get("content") or ""),
                    }
                    for item in row.get("messages") or ()
                ]
        return []

    def active_compactions(self) -> List[Dict[str, Any]]:
        """Return the compactions currently in force, in journal order.

        A ``restore`` row deactivates the named compaction, so rolling one back
        never rewrites history - it appends the fact that it no longer applies.
        """
        rows = self.load()
        active: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for row in rows:
            kind = str(row.get("record") or "")
            if kind == "compaction":
                identifier = str(row.get("compaction_id") or "")
                if not identifier:
                    continue
                if identifier not in active:
                    order.append(identifier)
                active[identifier] = row
            elif kind == "restore":
                active.pop(str(row.get("compaction_id") or ""), None)
        return [active[identifier] for identifier in order if identifier in active]

    def live_snapshot(self, *, before_turn: Optional[int] = None) -> Dict[str, Any]:
        """Return a ``ConversationMemory.restore``-shaped snapshot.

        ``before_turn`` restricts the result to turns strictly below the given
        index, which is the conversation half of a rewind to that turn.
        """
        rows = self.load()
        base = self._base(rows)
        dropped: set = set()
        handoff: Dict[str, Any] = {}
        for row in self.active_compactions():
            for seq in row.get("dropped_seqs") or ():
                dropped.add(int(seq or 0))
            if isinstance(row.get("handoff"), Mapping):
                handoff = dict(row["handoff"])
        history: List[Dict[str, Any]] = []
        highest = 0
        for row in rows:
            if str(row.get("record") or "") != "turn":
                continue
            try:
                turn_index = int(row.get("turn") or 0)
                seq = int(row.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if before_turn is not None and turn_index >= int(before_turn):
                continue
            if seq in dropped:
                continue
            record = dict(row)
            record["seq"] = seq
            record["turn"] = turn_index
            history.append(record)
            highest = max(highest, seq)
        history.sort(key=lambda item: int(item.get("seq") or 0))
        return {
            "base": base,
            "history": history,
            "handoff": handoff,
            "state_digest": "",
            "total_recorded": len(history),
            "seq": highest,
        }

    def restore_compaction(self, compaction_id: str) -> Optional[Dict[str, Any]]:
        """Deactivate one compaction and return the restored live snapshot."""
        identifier = str(compaction_id or "").strip()
        if not identifier:
            return None
        active = [
            row
            for row in self.active_compactions()
            if str(row.get("compaction_id") or "") == identifier
        ]
        if not active:
            return None
        row = {
            "record": "restore",
            "compaction_id": identifier,
            "turn": int(active[0].get("last_turn") or 0),
            "at": time.time(),
        }
        if not self.append(row):
            return None
        return self.live_snapshot()

    def rewind(self, turn: int) -> Dict[str, Any]:
        """Drop every conversation row belonging to ``turn`` or later.

        The previous file is rotated aside, never deleted, and the returned
        receipt states exactly what was dropped so the caller can put it in the
        journal.
        """
        boundary = max(0, int(turn or 0))
        rows = self.load()
        keep: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for row in rows:
            if str(row.get("record") or "") == "base":
                keep.append(row)
                continue
            try:
                turn_index = int(row.get("turn") or 0)
            except (TypeError, ValueError):
                turn_index = 0
            (dropped if turn_index >= boundary else keep).append(row)
        backup = _rotate(self.path)
        written = _write_jsonl_atomic(self.path, keep)
        return {
            "turn": boundary,
            "kept_rows": len(keep),
            "dropped_rows": len(dropped),
            "dropped_turns": sorted({int(row.get("turn") or 0) for row in dropped}),
            "backup": str(backup) if backup else "",
            "rewritten": written,
            "snapshot": self.live_snapshot(),
        }


class TurnFileState:
    """Per-turn pre-images of the files a run actually changed.

    A whole-repository snapshot per turn is not affordable, and a first-touch
    original is not enough: a file edited on turn 3 and again on turn 5 needs
    its turn-3 bytes to rewind to turn 4. So the caller captures the current
    bytes of the *tracked* (changed) files at the start of each turn, and
    :meth:`rewind` restores, for every file touched at or after the target turn,
    the bytes recorded at the earliest such turn. A file that was never modified
    before the target turn is restored from the caller's pristine source.

    ``read_bytes``/``write_bytes`` are injected by the caller so this class
    never reaches into a repository on its own; ``write_bytes(rel, None)`` must
    delete the path.
    """

    def __init__(
        self,
        run_dir: Path | str,
        *,
        read_bytes: Any,
        write_bytes: Any,
        read_pristine: Any = None,
        max_files_per_turn: int = 200,
        max_file_bytes: int = 2_000_000,
    ) -> None:
        self.root = Path(run_dir) / TURN_FILE_STATE_DIR
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.read_pristine = read_pristine
        self.max_files_per_turn = max(1, int(max_files_per_turn))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.warnings: List[str] = []

    def _turn_dir(self, turn: int) -> Path:
        return self.root / f"{int(turn or 0):06d}"

    def capture(self, turn: int, relatives: Sequence[str]) -> int:
        """Store the current bytes of ``relatives`` as the pre-image of ``turn``.

        Returns how many pre-images were stored. A file that cannot be read is
        recorded as absent, which is exactly what a later restore needs when the
        run created it.
        """
        stored = 0
        for relative in sorted({str(item) for item in relatives or () if item})[
            : self.max_files_per_turn
        ]:
            digest = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()
            try:
                data = self.read_bytes(relative)
            except Exception as exc:
                self._warn(f"pre-image read failed for {relative}: {exc}")
                data = None
            if isinstance(data, (bytes, bytearray)) and len(data) > self.max_file_bytes:
                self._warn(f"pre-image too large, recorded absent: {relative}")
                data = None
            target = self._turn_dir(turn) / f"{digest}.json"
            payload = {
                "turn": int(turn or 0),
                "relative": relative,
                "exists": data is not None,
                "size": len(data) if data is not None else 0,
                "sha256": hashlib.sha256(bytes(data)).hexdigest()
                if data is not None
                else "",
            }
            if not _append_jsonl_durable(target, payload):
                self._warn(f"pre-image index unwritable for {relative}")
                continue
            if data is not None:
                try:
                    (self._turn_dir(turn) / f"{digest}.bin").write_bytes(bytes(data))
                except OSError as exc:
                    self._warn(f"pre-image blob unwritable for {relative}: {exc}")
                    continue
            stored += 1
        return stored

    def captured(self) -> Dict[str, int]:
        """Return ``{relative: earliest captured turn}`` for every stored image."""
        earliest: Dict[str, int] = {}
        try:
            turn_dirs = sorted(self.root.glob("*"))
        except OSError:
            return earliest
        for turn_dir in turn_dirs:
            if not turn_dir.is_dir():
                continue
            for index in sorted(turn_dir.glob("*.json")):
                for row in _read_jsonl(index):
                    relative = str(row.get("relative") or "")
                    turn_index = int(row.get("turn") or 0)
                    if relative and relative not in earliest:
                        earliest[relative] = turn_index
        return earliest

    def pre_image(self, relative: str, turn: int) -> Tuple[bool, Optional[bytes]]:
        """Return ``(found, bytes)`` for the image captured AT ``turn``.

        The lookup is exact, and that is what makes the rewind exact. An image
        exists for a turn only if the file was already tracked at the START of
        that turn, so the image IS the state the turn saw. Any other turn's image
        describes a different moment, and a rewind must not quietly use it: the
        caller falls back to the pristine source, which is the correct answer for
        a file that was still untouched at the start of the target turn.
        """
        relative = str(relative or "")
        if not relative:
            return False, None
        digest = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()
        directory = self._turn_dir(turn)
        index = directory / f"{digest}.json"
        if not index.is_file():
            return False, None
        rows = [
            row
            for row in _read_jsonl(index)
            if row.get("relative") == relative
            and int(row.get("turn") or 0) == int(turn or 0)
        ]
        if not rows:
            return False, None
        if not bool(rows[0].get("exists")):
            return True, None
        blob = directory / f"{digest}.bin"
        try:
            return True, blob.read_bytes()
        except OSError as exc:
            self._warn(f"pre-image blob unreadable for {relative}: {exc}")
            return False, None

    def rewind(
        self,
        turn: int,
        *,
        tracked: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """Restore the exact pre-``turn`` bytes for every file touched since.

        ``tracked`` is the caller's current changed-file set. A file that was
        already changed at the START of ``turn`` has an image for exactly that
        turn, and is restored from it. A file that was still pristine at the
        start of ``turn`` is restored from the caller's pristine source - which
        is the correct answer, because a file mutated during a later turn has no
        image describing the moment the rewind is asking for. The receipt names
        every path so a caller can prove the state it reproduced.
        """
        boundary = max(0, int(turn or 0))
        current = {str(item) for item in tracked or () if item}
        candidates = set(current) | set(self.captured())
        restored: List[str] = []
        deleted: List[str] = []
        from_pristine: List[str] = []
        from_image: List[str] = []
        failed: List[Dict[str, str]] = []
        for relative in sorted(candidates):
            found, data = self.pre_image(relative, boundary)
            if found:
                from_image.append(relative)
            else:
                if self.read_pristine is None:
                    continue
                if relative not in current:
                    # Not currently changed and not imaged for this turn: the
                    # pristine restore would be a no-op, so the receipt should
                    # not claim it restored anything.
                    continue
                try:
                    data = self.read_pristine(relative)
                except Exception as exc:
                    failed.append({"path": relative, "error": str(exc)})
                    continue
                # `None` from the pristine source means "did not exist at the
                # start of the turn", so restoring it means removing the file.
                from_pristine.append(relative)
            try:
                self.write_bytes(relative, data)
            except Exception as exc:
                failed.append({"path": relative, "error": str(exc)})
                continue
            (deleted if data is None else restored).append(relative)
        return {
            "turn": boundary,
            "restored": sorted(restored),
            "deleted": sorted(deleted),
            "from_pre_image": sorted(from_image),
            "from_pristine": sorted(from_pristine),
            "failed": failed,
            "captured": len(self.captured()),
        }

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def rewind_turn_ledger(path: Path | str, turn: int) -> Dict[str, Any]:
    """Truncate a run's turn ledger to the turns before ``turn``.

    Uses the same row shape as ``harness.agent_kernel.turns.TurnLedger`` but is
    deliberately independent of that module (runtime must not import the
    harness). The previous file is rotated aside.
    """
    target = Path(path)
    boundary = max(0, int(turn or 0))
    rows = _read_jsonl(target)
    keep = [row for row in rows if int(row.get("turn") or 0) < boundary]
    backup = _rotate(target)
    written = _write_jsonl_atomic(target, keep)
    surviving = [row for row in keep if int(row.get("turn") or 0) > 0]
    return {
        "path": str(target),
        "turn": boundary,
        "kept": len(keep),
        "kept_rows": surviving,
        "dropped": len(rows) - len(keep),
        "backup": str(backup) if backup else "",
        "rewritten": written,
        "last_turn": max((int(row.get("turn") or 0) for row in surviving), default=0),
        "last_event_sequence": max(
            (int(row.get("event_sequence") or 0) for row in surviving), default=0
        ),
        "changed_files": sorted(
            {str(name) for row in surviving for name in row.get("changed_files") or ()}
        ),
        "spend_usd": max(
            (float(row.get("spend_usd") or 0.0) for row in surviving), default=0.0
        ),
    }


def rewind_checkpoint(path: Path | str, ledger: Any = None) -> Dict[str, Any]:
    """Re-derive a run's checkpoint from the surviving turn-ledger rows.

    ``ledger`` is either the path of an already-truncated ledger or the receipt
    :func:`rewind_turn_ledger` returned; both describe the same surviving rows.

    The checkpoint is a single mutable file, so after a rewind it must describe
    the rewound state rather than the discarded one: the event sequence, the
    agent-owned changes, and the spend all come from the rows that survived. The
    resume token is dropped deliberately - the turn state changed underneath it,
    so a relaunch must re-authorize against the new identity rather than reuse a
    token that described the discarded turns.
    """
    target = Path(path)
    data = read_json_or_none(target)
    if not isinstance(data, dict):
        return {"rewritten": False, "reason": "checkpoint unreadable or missing"}
    if isinstance(ledger, Mapping):
        kept_rows: List[Dict[str, Any]] = list(ledger.get("kept_rows") or ())
        if kept_rows:
            rows = kept_rows
        else:
            rows = _read_jsonl(Path(str(ledger.get("path") or "")))
        highest = int(ledger.get("last_turn") or 0)
        changed = [str(item) for item in ledger.get("changed_files") or ()]
        spend = float(ledger.get("spend_usd") or 0.0)
    elif ledger:
        rows = _read_jsonl(Path(str(ledger)))
        highest = max((int(row.get("turn") or 0) for row in rows), default=0)
        changed = sorted(
            {str(name) for row in rows for name in row.get("changed_files") or ()}
        )
        spend = max((float(row.get("spend_usd") or 0.0) for row in rows), default=0.0)
    else:
        rows = []
        highest = 0
        changed = []
        spend = 0.0
    data["turn_id"] = f"turn-{highest}"
    data["last_event_sequence"] = max(
        (int(row.get("event_sequence") or 0) for row in rows), default=0
    )
    data["agent_owned_changes"] = sorted(set(changed))
    data["spend"] = round(spend, 6)
    data["model_context_references"] = []
    data["active_processes"] = []
    data["resume_token"] = ""
    data["rewound_at"] = now_iso()
    data["rewound_to_turn"] = highest
    return {"rewritten": atomic_write_json(target, data), "checkpoint": data}
