"""Persistent conversation sessions for the `neo` interactive shell.

The TUI and the rich REPL used to treat every submitted line as an
isolated fix run (one Task per line, history kept only as task dirs).
This module gives `neo` an opencode/claude-style persistent session:

- ONE state file per conversation under ``<log_root>/_conversations/``,
  holding the multi-turn transcript (user + assistant turns), the raw
  input history, and a compacted summary — not one Task row per line.
- ``@path`` mention expansion against the repo's file list (the same
  ``scan_repo_files`` surface the palette searches).
- ``compact_session`` — deterministic compaction that reuses the
  harness's EXISTING recall primitive (``TraceLogger.find_events``)
  to enrich the summary, then drops old turns.
- Memory-first hooks: ``session_memory_brief`` (structural + decision
  memory queried automatically on session start) and
  ``ingest_session_facts`` (facts ingested automatically on finish),
  so no manual ``neo memory`` calls are needed.

Conversation helpers are total for ordinary input, while persisted snapshots
are strict: malformed or cross-repository files raise ``SessionCorruptError``
unless a diagnostic caller explicitly passes ``strict=False``. File IO uses
atomic tmp+replace writes and bounded redaction so a session cannot silently
lose a conversation or persist credentials.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

__all__ = [
    "PULSE_ANTI_CLUTTER_EXEMPT",
    "PULSE_SECTIONS",
    "SESSION_GUARD_MODES",
    "SURVIVAL_VERDICTS",
    "SessionCorruptError",
    "SessionImportError",
    "SessionRecoveryError",
    "SessionRepositoryMismatch",
    "append_history",
    "append_session_event",
    "append_turn",
    "build_session_context",
    "checkpoint_before_mutation",
    "compact_session",
    "conversations_dir",
    "copy_text_to_clipboard",
    "create_checkpoint",
    "create_session_checkpoint",
    "cross_repo_sessions",
    "diff_checkpoint",
    "event_log_path",
    "expand_at_mentions",
    "expand_symbol_mentions",
    "export_conversation",
    "export_session",
    "export_session_document",
    "export_session_file",
    "fork_session",
    "format_session_context_status",
    "import_conversation",
    "import_session",
    "index_compact",
    "index_conversation",
    "index_dir",
    "index_newest_resumable",
    "index_records",
    "index_remove",
    "index_roots",
    "index_run",
    "index_session",
    "index_stats",
    "ingest_session_facts",
    "inspect_session",
    "link_turn_to_run",
    "list_checkpoints",
    "list_conversations",
    "load_checkpoint",
    "load_latest_session",
    "load_or_create",
    "new_session_id",
    "open_session",
    "promote_session_decision",
    "rebuild_session",
    "reconstruct_session",
    "recover_conversation",
    "recover_corrupt_session",
    "recover_session",
    "resolve_artifact_root",
    "resolve_index_session",
    "resolve_session_token",
    "restore_checkpoint",
    "restore_session_checkpoint",
    "resume_session",
    "retrieve_session_turns",
    "review_checkpoint",
    "save_session",
    "session_fork",
    "session_instance_guard",
    "session_memory_brief",
    "session_pulse",
    "session_pulse_lines",
    "session_survival_lines",
    "session_survival_report",
    "set_active_run",
    "set_model_profile",
    "set_unresolved_questions",
    "set_workspace_mode",
    "startup_recovery_candidate",
    "transcript_segment_lines",
    "transcript_segments",
    "workspace_identity",
]

_CONVERSATIONS_DIR = "_conversations"
_SCHEMA_VERSION = 2
_EVENT_SCHEMA_VERSION = 1
_EXPORT_SCHEMA_VERSION = 1
_MAX_TURNS = 200
_MAX_RAW_TURNS = 400
_MAX_COMPACTED_TURNS = 400
_MAX_HISTORY = 200
_MAX_SNIPPET_CHARS = 6000
_MAX_SNIPPET_LINES = 60
_MAX_SESSION_BYTES = 32 * 1024 * 1024
_MAX_LOCK_WAIT_S = 5.0
_MAX_LOCK_STALE_S = 30.0

_AT_PAT = re.compile(r"(?<!\w)@([A-Za-z0-9_./\\-]{1,120})")
_TRAILING_PUNCT = ".,;:!?)}]['\""
_REDACTED_SECRET = "[REDACTED_SECRET]"
_SESSION_SECRET_PARTS = (
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "auth_token",
    "authorization",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)


def _redact_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    try:
        from memory.decision_store import redact_secrets

        return redact_secrets(value)
    except Exception:
        text = str(value)
        text = re.sub(r"(?i)\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b", _REDACTED_SECRET, text)
        text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer " + _REDACTED_SECRET, text)
        return text


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return any(part in normalized for part in _SESSION_SECRET_PARTS)


def _redact_value(value: Any, key: Any = None, depth: int = 0) -> Any:
    if key is not None and _sensitive_key(key):
        return _REDACTED_SECRET
    if depth > 8:
        return ""
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item_value, item_key, depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return ""


def _strip_sensitive(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return {}
    if isinstance(value, Mapping):
        return {
            str(key): _strip_sensitive(item, depth + 1)
            for key, item in value.items()
            if not _sensitive_key(key)
        }
    if isinstance(value, (list, tuple, set)):
        return [_strip_sensitive(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return ""


class SessionCorruptError(RuntimeError):
    """Raised when a persisted conversation cannot be decoded safely."""


class SessionRepositoryMismatch(SessionCorruptError):
    """Raised when a session is reopened for a different repository."""


class SessionRecoveryError(SessionCorruptError):
    """Raised when an explicit recovery operation cannot be completed."""


class SessionImportError(ValueError):
    """Raised when an exported session is malformed or unsafe to import."""


def _valid_session_id(value: Any) -> bool:
    text = str(value or "")
    if not text or text != text.strip() or text in (".", ".."):
        return False
    if any(char in text for char in '/\\:*?"<>|\x00'):
        return False
    if any(ord(char) < 32 for char in text):
        return False
    normalized = text.rstrip(". ")
    if normalized in ("", ".", ".."):
        return False
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    return normalized.casefold() not in {item.casefold() for item in reserved}


def _repo_key(value: Any) -> str:
    try:
        return os.path.normcase(str(Path(str(value or "")).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(str(value or ""))


def _git_value(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def workspace_identity(repo: Any) -> Dict[str, str]:
    """Return a stable, non-secret identity for a project workspace."""
    raw = str(repo or "").strip()
    if not raw:
        return {
            "path": "",
            "key": "",
            "name": "",
            "workspace_id": "",
            "git_root": "",
            "git_revision": "",
            "git_branch": "",
        }
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(raw)
    stat_value = ""
    device = ""
    inode = ""
    try:
        stat = resolved.stat()
        stat_value = str(int(stat.st_mtime_ns))
        device = str(getattr(stat, "st_dev", ""))
        inode = str(getattr(stat, "st_ino", ""))
    except (OSError, RuntimeError, ValueError):
        pass
    git_root = _git_value(resolved, "rev-parse", "--show-toplevel")
    git_revision = _git_value(resolved, "rev-parse", "HEAD")
    git_branch = _git_value(resolved, "symbolic-ref", "--short", "-q", "HEAD")
    key = _repo_key(resolved)
    seed = "|".join((key, device, inode, git_revision))
    workspace_id = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[
        :24
    ]
    return {
        "path": str(resolved),
        "key": key,
        "name": resolved.name,
        "workspace_id": workspace_id,
        "git_root": git_root,
        "git_revision": git_revision,
        "git_branch": git_branch,
        "observed_mtime_ns": stat_value,
    }


def _repo_identity(repo: Any) -> Dict[str, str]:
    identity = workspace_identity(repo)
    return {
        "path": identity.get("path", ""),
        "key": identity.get("key", ""),
        "name": identity.get("name", ""),
    }


def _conversations_root(log_root: Any) -> Path:
    try:
        root = conversations_dir(log_root)
    except Exception:
        root = Path(".") / _CONVERSATIONS_DIR
    try:
        if root.is_symlink():
            raise SessionCorruptError("conversation root must not be a symbolic link")
    except OSError as exc:
        raise SessionCorruptError("conversation root cannot be inspected") from exc
    return root


def _session_path(log_root: Any, sid: str) -> Path:
    if not _valid_session_id(sid):
        raise ValueError("invalid conversation session id")
    return _conversations_root(log_root) / f"{sid}.json"


def _event_log_path(log_root: Any, sid: str) -> Path:
    if not _valid_session_id(sid):
        raise ValueError("invalid conversation session id")
    return _conversations_root(log_root) / f"{sid}.events.jsonl"


def event_log_path(log_root: Any, session_id: str) -> Path:
    """Return the append-only event journal path for one conversation."""
    return _event_log_path(log_root, session_id)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes, *, backup: bool = False) -> None:
    if path.is_symlink():
        raise OSError("refusing to replace a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists() and path.is_file():
        old = path.read_bytes()
        backup_path = path.with_name(path.name + ".bak")
        fd, backup_temp_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        backup_temp = Path(backup_temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(old)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(backup_temp, backup_path)
        finally:
            if backup_temp.exists():
                try:
                    backup_temp.unlink()
                except OSError:
                    pass
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        last_error: Optional[OSError] = None
        for attempt in range(4):
            try:
                os.replace(temp_path, path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.03 * (attempt + 1))
        if last_error is not None:
            raise last_error
        _fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@contextmanager
def _session_write_lock(path: Path):
    lock_path = _lock_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise SessionCorruptError("conversation root must not be a symbolic link")
    started = time.monotonic()
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > _MAX_LOCK_STALE_S and not lock_path.is_symlink():
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
            except OSError:
                pass
            if time.monotonic() - started > _MAX_LOCK_WAIT_S:
                raise SessionCorruptError("conversation writer lock is busy") from None
            time.sleep(0.01)
        except OSError as exc:
            raise SessionCorruptError(
                "conversation writer lock cannot be created"
            ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            descriptor = None
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            if lock_path.exists() and not lock_path.is_symlink():
                lock_path.unlink()
        except OSError:
            pass


def _session_digest(data: Mapping[str, Any]) -> str:
    value = {str(key): item for key, item in data.items() if key != "integrity"}
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json_file(path: Path, *, strict: bool = True) -> Any:
    if path.is_symlink():
        if strict:
            raise SessionCorruptError("conversation file must not be a symbolic link")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        if strict:
            raise SessionCorruptError(
                f"cannot decode conversation file {path.name}: {type(exc).__name__}"
            ) from exc
        return None


def _read_event_records(path: Path, *, strict: bool = True) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink():
        if strict:
            raise SessionCorruptError(
                "conversation event journal must not be a symbolic link"
            )
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        if strict:
            raise SessionCorruptError(
                "conversation event journal is unreadable"
            ) from exc
        return []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            if strict:
                raise SessionCorruptError(
                    f"conversation event journal line {number} is corrupt"
                ) from exc
            continue
        if not isinstance(value, dict):
            if strict:
                raise SessionCorruptError(
                    f"conversation event journal line {number} is not an object"
                )
            continue
        try:
            version = int(value.get("schema_version", _EVENT_SCHEMA_VERSION))
            sequence = int(value.get("sequence", 0))
        except (TypeError, ValueError) as exc:
            if strict:
                raise SessionCorruptError(
                    "conversation event metadata is invalid"
                ) from exc
            continue
        if version != _EVENT_SCHEMA_VERSION or sequence <= 0:
            if strict:
                raise SessionCorruptError("conversation event schema is unsupported")
            continue
        records.append(dict(value))
    expected = 1
    for record in records:
        if int(record.get("sequence", 0)) != expected:
            if strict:
                raise SessionCorruptError(
                    "conversation event sequence is not contiguous"
                )
            break
        expected += 1
    return records


def _append_event_record(
    log_root: Any,
    session: Dict[str, Any],
    event_type: str,
    payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    sid = str(session.get("session_id") or "")
    if not _valid_session_id(sid):
        return None
    try:
        path = _event_log_path(log_root, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _session_write_lock(path):
            records = _read_event_records(path, strict=False)
            next_sequence = len(records) + 1
            if records:
                next_sequence = max(
                    next_sequence, int(records[-1].get("sequence", 0)) + 1
                )
            record = {
                "schema_version": _EVENT_SCHEMA_VERSION,
                "sequence": next_sequence,
                "session_id": sid,
                "event": _redact_text(event_type)[:128],
                "timestamp": time.time(),
                "payload": _redact_value(dict(payload)),
            }
            encoded = (
                json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            ).encode("utf-8")
            with path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            session["event_sequence"] = next_sequence
            session["event_log_path"] = str(path)
            return record
    except Exception:
        session["_event_error"] = "conversation event journal could not be updated"
        return None


def _new_session(sid: str, repo: Any) -> Dict[str, Any]:
    now = time.time()
    identity = workspace_identity(repo)
    return {
        "schema_version": _SCHEMA_VERSION,
        "session_id": sid,
        "repo": str(identity.get("path") or repo or ""),
        "repo_identity": _repo_identity(repo),
        "workspace_identity": identity,
        "started_ts": now,
        "updated_ts": now,
        "turns": [],
        "raw_turns": [],
        "compacted_turns": [],
        "summaries": [],
        "history": [],
        "summary": "",
        "active_run_id": None,
        "model_profile": {},
        "unresolved_questions": [],
        "workspace_mode": "live",
        "event_sequence": 0,
        "event_log_path": "",
        "lineage": [],
        "_revision": 0,
    }


def _validate_turn(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    safe_value = _redact_value(value)
    if not isinstance(safe_value, dict):
        return None
    role = str(safe_value.get("role") or "user")
    text = safe_value.get("text")
    if not isinstance(text, str):
        return None
    result = dict(safe_value)
    result["role"] = role
    result["text"] = text
    turn_id = str(result.get("turn_id") or "").strip()
    if not _valid_session_id(turn_id):
        result["turn_id"] = f"turn-{uuid.uuid4().hex[:12]}"
    if result.get("run_id") is not None:
        result["run_id"] = str(result.get("run_id") or "") or None
    if result.get("task_id") is not None:
        result["task_id"] = str(result.get("task_id") or "") or None
    return result


def _normalise_session(data: Dict[str, Any], sid: str, repo: Any) -> Dict[str, Any]:
    safe_data = _redact_value(data)
    if not isinstance(safe_data, dict):
        raise SessionCorruptError(f"conversation {sid!r} is not a JSON object")
    try:
        version = int(safe_data.get("schema_version", _SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise SessionCorruptError("conversation schema version is invalid") from exc
    if version != _SCHEMA_VERSION:
        raise SessionCorruptError(
            f"unsupported conversation schema version {version}; expected {_SCHEMA_VERSION}"
        )
    stored_sid = str(safe_data.get("session_id") or "").strip()
    if stored_sid and stored_sid != sid:
        raise SessionCorruptError(
            f"conversation identity mismatch: expected {sid!r}, found {stored_sid!r}"
        )
    stored_path = str(safe_data.get("repo") or safe_data.get("repo_path") or "").strip()
    stored_identity = safe_data.get("workspace_identity")
    if not isinstance(stored_identity, dict):
        stored_identity = safe_data.get("repo_identity")
    if isinstance(stored_identity, dict):
        stored_path = str(
            stored_identity.get("path") or stored_identity.get("key") or stored_path
        )
    if stored_path.startswith("sha256:"):
        stored_path = ""
    selected_repo = repo or stored_path
    identity = workspace_identity(selected_repo)
    if (
        identity.get("key")
        and stored_path
        and identity["key"] != _repo_key(stored_path)
    ):
        raise SessionRepositoryMismatch(
            f"session {sid!r} belongs to {stored_path!r}, not {identity['path']!r}"
        )
    result = _new_session(sid, selected_repo)
    result.update(safe_data)
    result["schema_version"] = _SCHEMA_VERSION
    result["session_id"] = sid
    result["repo"] = str(identity.get("path") or stored_path or "")
    result["repo_identity"] = _repo_identity(selected_repo)
    stored_workspace = safe_data.get("workspace_identity")
    if isinstance(stored_workspace, dict):
        merged_workspace = dict(stored_workspace)
        merged_workspace.update(identity)
        result["workspace_identity"] = _redact_value(merged_workspace)
    turns: List[Dict[str, Any]] = []
    for index, value in enumerate(safe_data.get("turns", []) or []):
        validated = _validate_turn(value)
        if validated is not None:
            if not validated.get("turn_id"):
                validated["turn_id"] = f"turn-legacy-{index + 1}"
            turns.append(validated)
    raw_value = safe_data.get("raw_turns")
    raw_source = raw_value if isinstance(raw_value, list) else list(turns)
    raw_turns: List[Dict[str, Any]] = []
    seen_turn_ids = set()
    for index, value in enumerate(raw_source):
        validated = _validate_turn(value)
        if validated is None:
            continue
        turn_id = str(validated.get("turn_id") or f"turn-legacy-{index + 1}")
        validated["turn_id"] = turn_id
        if turn_id in seen_turn_ids:
            continue
        seen_turn_ids.add(turn_id)
        raw_turns.append(validated)
    for turn in turns:
        turn_id = str(turn.get("turn_id") or "")
        if turn_id and turn_id not in seen_turn_ids:
            seen_turn_ids.add(turn_id)
            raw_turns.append(turn)
    result["turns"] = turns[-_MAX_TURNS:]
    result["raw_turns"] = raw_turns[-_MAX_RAW_TURNS:]
    compacted: List[Dict[str, Any]] = []
    compacted_ids = set()
    for index, value in enumerate(safe_data.get("compacted_turns", []) or []):
        validated = _validate_turn(value)
        if validated is None:
            continue
        turn_id = str(validated.get("turn_id") or f"turn-compacted-{index + 1}")
        validated["turn_id"] = turn_id
        if turn_id in compacted_ids:
            continue
        compacted_ids.add(turn_id)
        compacted.append(validated)
    result["compacted_turns"] = compacted[-_MAX_COMPACTED_TURNS:]
    result["summaries"] = [
        _redact_value(item)
        for item in safe_data.get("summaries", []) or []
        if isinstance(item, dict)
    ][-_MAX_COMPACTED_TURNS:]
    result["history"] = [
        _redact_text(item)
        for item in safe_data.get("history", []) or []
        if isinstance(item, str)
    ][-_MAX_HISTORY:]
    result["summary"] = _redact_text(safe_data.get("summary") or "")
    result["unresolved_questions"] = [
        _redact_text(item)
        for item in safe_data.get("unresolved_questions", []) or []
        if isinstance(item, str)
    ][:_MAX_TURNS]
    result["active_run_id"] = (
        _redact_text(safe_data.get("active_run_id")) or None
        if safe_data.get("active_run_id")
        else None
    )
    result["model_profile"] = _strip_sensitive(safe_data.get("model_profile") or {})
    result["workspace_mode"] = _redact_text(safe_data.get("workspace_mode") or "live")
    result["started_ts"] = safe_data.get("started_ts") or result["started_ts"]
    result["updated_ts"] = safe_data.get("updated_ts") or result["updated_ts"]
    try:
        result["event_sequence"] = max(0, int(safe_data.get("event_sequence", 0) or 0))
    except (TypeError, ValueError):
        result["event_sequence"] = 0
    result["event_log_path"] = _redact_text(safe_data.get("event_log_path") or "")
    lineage = safe_data.get("lineage")
    result["lineage"] = (
        [_redact_value(item) for item in lineage if isinstance(item, (dict, str))][
            -_MAX_COMPACTED_TURNS:
        ]
        if isinstance(lineage, list)
        else []
    )
    try:
        result["_revision"] = max(0, int(safe_data.get("_revision", 0) or 0))
    except (TypeError, ValueError):
        result["_revision"] = 0
    return result


def _apply_event_record(session: Dict[str, Any], record: Mapping[str, Any]) -> None:
    event_type = str(record.get("event") or "")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if event_type == "turn_appended":
        turn = _validate_turn(payload.get("turn"))
        if turn is None:
            return
        event_id = str(payload.get("event_id") or turn.get("event_id") or "")
        turns = session.setdefault("turns", [])
        raw_turns = session.setdefault("raw_turns", [])
        if event_id and any(
            str(item.get("event_id") or "") == event_id for item in raw_turns
        ):
            return
        turns.append(dict(turn))
        raw_turns.append(dict(turn))
        del turns[: max(0, len(turns) - _MAX_TURNS)]
        del raw_turns[: max(0, len(raw_turns) - _MAX_RAW_TURNS)]
    elif event_type == "compaction":
        summary = _redact_text(payload.get("summary") or "")
        recent = [
            validated
            for value in payload.get("turns", []) or []
            if (validated := _validate_turn(value)) is not None
        ][-_MAX_TURNS:]
        compacted = [
            validated
            for value in payload.get("compacted_turns", []) or []
            if (validated := _validate_turn(value)) is not None
        ][-_MAX_COMPACTED_TURNS:]
        session["summary"] = summary
        session["turns"] = recent
        compacted_current = session.get("compacted_turns")
        if not isinstance(compacted_current, list):
            compacted_current = []
        compacted_current.extend(compacted)
        session["compacted_turns"] = compacted_current[-_MAX_COMPACTED_TURNS:]
        summaries = session.get("summaries")
        if not isinstance(summaries, list):
            summaries = []
        summaries.append(
            {
                "ts": float(record.get("timestamp") or time.time()),
                "event_sequence": int(record.get("sequence") or 0),
                "turn_count": int(payload.get("turn_count") or len(compacted)),
                "source_turn_ids": [
                    str(item.get("turn_id") or "")
                    for item in compacted
                    if isinstance(item, dict)
                ],
                "text": summary,
            }
        )
        session["summaries"] = summaries[-_MAX_COMPACTED_TURNS:]
    elif event_type == "run_linked":
        turn_id = _redact_text(payload.get("turn_id") or "")
        run_id = _redact_text(payload.get("run_id") or "")
        task_id = _redact_text(payload.get("task_id") or "")
        for turn in list(session.get("turns") or []) + list(
            session.get("raw_turns") or []
        ):
            if str(turn.get("turn_id") or "") != turn_id:
                continue
            if run_id:
                turn["run_id"] = run_id
            if task_id:
                turn["task_id"] = task_id
            for key in ("trace_path", "checkpoint_path"):
                value = _redact_text(payload.get(key) or "")
                if value:
                    turn[key] = value
    elif event_type == "session_forked":
        lineage = session.get("lineage")
        if not isinstance(lineage, list):
            lineage = []
        lineage.append(_redact_value(dict(payload)))
        session["lineage"] = lineage[-_MAX_COMPACTED_TURNS:]
    elif event_type == "decision_promoted":
        promoted = session.get("decision_promotions")
        if not isinstance(promoted, list):
            promoted = []
        promoted.append(
            {
                "decision_id": int(payload.get("decision_id") or 0),
                "turn_id": _redact_text(payload.get("turn_id") or ""),
                "run_id": _redact_text(payload.get("run_id") or ""),
                "event_sequence": int(record.get("sequence") or 0),
            }
        )
        session["decision_promotions"] = promoted[-_MAX_TURNS:]
    session["event_sequence"] = max(
        int(session.get("event_sequence") or 0),
        int(record.get("sequence") or 0),
    )
    session["event_count"] = max(
        int(session.get("event_count") or 0),
        int(record.get("sequence") or 0),
    )
    session["last_event_type"] = event_type


def _replay_session_events(
    session: Dict[str, Any],
    log_root: Any,
    *,
    strict: bool = True,
) -> List[Dict[str, Any]]:
    sid = str(session.get("session_id") or "")
    if not _valid_session_id(sid):
        return []
    try:
        records = _read_event_records(_event_log_path(log_root, sid), strict=strict)
    except SessionCorruptError:
        if strict:
            raise
        return []
    for record in records:
        if str(record.get("session_id") or "") != sid:
            if strict:
                raise SessionCorruptError("conversation event identity mismatch")
            continue
        if int(record.get("sequence") or 0) > int(session.get("event_sequence") or 0):
            _apply_event_record(session, record)
    session["event_count"] = len(records)
    if records:
        session["last_event_type"] = str(records[-1].get("event") or "")
    return records


def new_session_id() -> str:
    """A fresh conversation id (``sess-<8 hex>``)."""
    return f"sess-{uuid.uuid4().hex[:8]}"


def conversations_dir(log_root: Any) -> Path:
    """``<log_root>/_conversations`` (created on save, never on read)."""
    return Path(log_root) / _CONVERSATIONS_DIR


def load_or_create(
    log_root: Any,
    repo: Any,
    session_id: Optional[str] = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """Load a conversation, creating a new one only when no file exists."""
    sid = str(session_id or "").strip() or new_session_id()
    if not _valid_session_id(sid):
        raise ValueError("invalid conversation session id")
    try:
        path = _session_path(log_root, sid)
    except Exception:
        if strict:
            raise
        result = _new_session(sid, repo)
        result["_recovery_state"] = "unavailable"
        return result
    if not path.exists():
        result = _new_session(sid, repo)
        result["_log_root"] = str(log_root or "")
        try:
            index_conversation(result, log_root, status="new", resumable=False)
        except Exception:
            pass
        return result
    try:
        data = _read_json_file(path, strict=True)
        if not isinstance(data, dict):
            raise SessionCorruptError(f"conversation {sid!r} is not a JSON object")
        stored_integrity = str(data.get("integrity") or "")
        if stored_integrity and stored_integrity != _session_digest(data):
            raise SessionCorruptError(
                f"conversation {sid!r} failed its integrity check"
            )
        result = _normalise_session(data, sid, repo)
        _replay_session_events(result, log_root, strict=strict)
    except SessionCorruptError:
        if strict:
            raise
        result = _new_session(sid, repo)
        result["_recovery_state"] = "corrupt_fresh_compatibility"
        result["_log_root"] = str(log_root or "")
        return result
    result["_log_root"] = str(log_root or "")
    return result


def _conversation_records(
    log_root: Any,
    repo: Any = None,
    *,
    include_corrupt: bool = False,
) -> List[Dict[str, Any]]:
    try:
        root = _conversations_root(log_root)
    except Exception:
        return []
    wanted = _repo_identity(repo)["key"] if repo else ""
    records: List[Dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        sid = path.stem
        if not _valid_session_id(sid):
            continue
        try:
            data = _read_json_file(path, strict=False)
            if not isinstance(data, dict):
                if include_corrupt:
                    records.append(
                        {
                            "session_id": sid,
                            "repo": "",
                            "updated_ts": path.stat().st_mtime,
                            "turn_count": 0,
                            "summary": "",
                            "path": str(path),
                            "status": "corrupt",
                        }
                    )
                continue
            identity = _repo_identity(data.get("repo") or data.get("repo_path"))
            if wanted and identity["key"] != wanted:
                continue
            records.append(
                {
                    "session_id": sid,
                    "repo": identity["path"],
                    "updated_ts": float(
                        data.get("updated_ts") or data.get("started_ts") or 0
                    ),
                    "turn_count": len(data.get("turns") or []),
                    "summary": _redact_text(data.get("summary") or "")[:240],
                    "path": str(path),
                    "status": "ok",
                }
            )
        except (OSError, UnicodeError, ValueError, TypeError):
            if include_corrupt:
                try:
                    records.append(
                        {
                            "session_id": sid,
                            "repo": "",
                            "updated_ts": path.stat().st_mtime,
                            "turn_count": 0,
                            "summary": "",
                            "path": str(path),
                            "status": "corrupt",
                        }
                    )
                except OSError:
                    continue
    return sorted(
        records,
        key=lambda item: (item["updated_ts"], item["session_id"]),
        reverse=True,
    )


def list_conversations(log_root: Any, repo: Any = None) -> List[Dict[str, Any]]:
    """List readable conversation snapshots without exposing file contents."""
    return _conversation_records(log_root, repo, include_corrupt=False)


def inspect_session(log_root: Any, session_id: str, repo: Any = None) -> Dict[str, Any]:
    """Describe a session's explicit health without changing any files."""
    sid = str(session_id or "").strip()
    result: Dict[str, Any] = {
        "session_id": sid,
        "status": "missing",
        "path": "",
        "backup_available": False,
        "event_path": "",
        "event_count": 0,
        "error": None,
    }
    if not _valid_session_id(sid):
        result.update({"status": "invalid", "error": "invalid conversation session id"})
        return result
    try:
        path = _session_path(log_root, sid)
    except Exception as exc:
        result.update({"status": "unavailable", "error": type(exc).__name__})
        return result
    result["path"] = str(path)
    result["backup_available"] = path.with_name(path.name + ".bak").is_file()
    try:
        event_path = _event_log_path(log_root, sid)
        result["event_path"] = str(event_path)
    except Exception as exc:
        result.update({"status": "unavailable", "error": type(exc).__name__})
        return result
    if not path.exists():
        return result
    try:
        session = load_or_create(log_root, repo, sid, strict=True)
        result.update(
            {
                "status": "ok",
                "revision": int(session.get("_revision") or 0),
                "event_count": int(session.get("event_count") or 0),
                "turn_count": len(session.get("turns") or []),
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "corrupt",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
        )
    return result


def recover_session(
    log_root: Any,
    session_id: str,
    repo: Any = None,
    *,
    strategy: str = "report",
) -> Dict[str, Any]:
    """Recover a corrupt session only through an explicit strategy."""
    selected = str(strategy or "report").strip().casefold()
    if selected not in {"report", "backup", "fresh"}:
        raise ValueError("session recovery strategy must be report, backup, or fresh")
    sid = str(session_id or "").strip()
    path = _session_path(log_root, sid)
    event_path = _event_log_path(log_root, sid)
    report = inspect_session(log_root, sid, repo)
    if report["status"] == "ok":
        report["action"] = "none"
        return report
    if selected == "report":
        raise SessionRecoveryError(
            report.get("error") or f"conversation {sid!r} is corrupt"
        )
    if selected == "backup":
        backup = path.with_name(path.name + ".bak")
        if not backup.is_file() or backup.is_symlink():
            raise SessionRecoveryError(
                "no last-known-good conversation backup is available"
            )
        _atomic_write_bytes(path, backup.read_bytes())
        report = inspect_session(log_root, sid, repo)
        if report["status"] != "ok":
            raise SessionRecoveryError(
                report.get("error")
                or "conversation backup did not restore a valid session"
            )
        report["action"] = "restored_backup"
        return report
    if path.exists() and path.is_symlink():
        raise SessionRecoveryError("conversation file must not be a symbolic link")
    if event_path.exists() and event_path.is_symlink():
        raise SessionRecoveryError(
            "conversation event journal must not be a symbolic link"
        )
    stamp = int(time.time() * 1000)
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
    if path.exists():
        os.replace(path, quarantine)
    event_quarantine = event_path.with_name(f"{event_path.name}.corrupt-{stamp}")
    if event_path.exists():
        os.replace(event_path, event_quarantine)
    fresh = _new_session(sid, repo)
    if not save_session(log_root, fresh):
        raise SessionRecoveryError("fresh conversation could not be persisted")
    report.update(
        {
            "status": "recovered_fresh",
            "action": "quarantined_and_recreated",
            "quarantine_path": str(quarantine) if quarantine.exists() else "",
            "event_quarantine_path": str(event_quarantine)
            if event_quarantine.exists()
            else "",
            "error": None,
        }
    )
    try:
        index_conversation(
            load_or_create(log_root, repo, sid, strict=True),
            log_root,
            status="recovered",
            resumable=False,
            source="recovery",
        )
    except Exception:
        pass
    return report


def load_latest_session(log_root: Any, repo: Any) -> Dict[str, Any]:
    """Resume the newest readable conversation for ``repo``."""
    records = _conversation_records(log_root, repo, include_corrupt=True)
    if not records:
        result = _new_session(new_session_id(), repo)
        result["_log_root"] = str(log_root or "")
        return result
    newest = records[0]
    if newest.get("status") == "corrupt":
        return load_or_create(log_root, repo, newest["session_id"], strict=True)
    return load_or_create(log_root, repo, newest["session_id"], strict=True)


def resume_session(
    log_root: Any,
    repo: Any = None,
    session_id: Optional[str] = None,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Resume a named session or the newest session for a repository."""
    if session_id:
        return load_or_create(log_root, repo, session_id, strict=strict)
    return load_latest_session(log_root, repo)


def _serialisable_session(session: Dict[str, Any]) -> Dict[str, Any]:
    value = _redact_value(copy.deepcopy(session))
    if not isinstance(value, dict):
        return {}
    for key in (
        "_read_only",
        "_load_error",
        "_last_save_error",
        "_event_error",
        "_recovery_state",
        "_log_root",
    ):
        value.pop(key, None)
    return value


def save_session(log_root: Any, session: Dict[str, Any]) -> bool:
    """Atomically compare, write, and publish one complete session snapshot."""
    if not isinstance(session, dict) or session.get("_read_only"):
        return False
    try:
        sid = str(session.get("session_id") or new_session_id())
        if not _valid_session_id(sid):
            session["_last_save_error"] = "invalid conversation session id"
            return False
        path = _session_path(log_root, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _session_write_lock(path):
            if path.is_symlink():
                session["_last_save_error"] = (
                    "conversation file must not be a symbolic link"
                )
                return False
            try:
                base_revision = max(0, int(session.get("_revision", 0) or 0))
            except (TypeError, ValueError):
                base_revision = 0
            existing: Optional[Dict[str, Any]] = None
            if path.exists():
                if not path.is_file():
                    session["_last_save_error"] = (
                        "conversation path is not a regular file"
                    )
                    return False
                try:
                    existing = _read_json_file(path, strict=True)
                except SessionCorruptError as exc:
                    session["_last_save_error"] = str(exc)
                    return False
                if not isinstance(existing, dict):
                    session["_last_save_error"] = "conversation root is not an object"
                    return False
                stored_integrity = str(existing.get("integrity") or "")
                if stored_integrity and stored_integrity != _session_digest(existing):
                    session["_last_save_error"] = (
                        "conversation failed its integrity check"
                    )
                    return False
                try:
                    existing_revision = max(0, int(existing.get("_revision", 0) or 0))
                except (TypeError, ValueError):
                    existing_revision = -1
                if existing_revision != base_revision:
                    session["_last_save_error"] = (
                        "concurrent conversation update refused"
                    )
                    return False
            elif base_revision:
                session["_last_save_error"] = "conversation disappeared before save"
                return False
            candidate = copy.deepcopy(session)
            candidate["session_id"] = sid
            candidate["schema_version"] = _SCHEMA_VERSION
            candidate["updated_ts"] = time.time()
            candidate["_revision"] = base_revision + 1
            candidate["event_log_path"] = str(_event_log_path(log_root, sid))
            payload = _serialisable_session(candidate)
            payload["integrity"] = _session_digest(payload)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                indent=1,
                default=str,
            ).encode("utf-8")
            if len(encoded) > _MAX_SESSION_BYTES:
                session["_last_save_error"] = (
                    "conversation snapshot exceeds the size limit"
                )
                return False
            _atomic_write_bytes(path, encoded, backup=existing is not None)
            session.clear()
            session.update(copy.deepcopy(payload))
            session["_log_root"] = str(log_root or "")
            session.pop("_last_save_error", None)
            try:
                index_conversation(
                    session,
                    log_root,
                    status="active" if session.get("active_run_id") else "idle",
                    resumable=bool(session.get("active_run_id")),
                )
            except Exception:
                pass
            return True
    except Exception as exc:
        try:
            session["_last_save_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        except Exception:
            pass
        return False


def append_turn(
    session: Dict[str, Any],
    role: str,
    text: str,
    task_id: Optional[str] = None,
    *,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    trace_path: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append a linked, redacted turn and its durable event record."""
    if not isinstance(session, dict) or session.get("_read_only"):
        return
    try:
        turns = session.get("turns")
        if not isinstance(turns, list):
            turns = session["turns"] = []
        raw_turns = session.get("raw_turns")
        if not isinstance(raw_turns, list):
            raw_turns = session["raw_turns"] = list(turns)
        entry: Dict[str, Any] = {
            "event_id": f"turn-event-{uuid.uuid4().hex[:16]}",
            "turn_id": str(turn_id or f"turn-{uuid.uuid4().hex[:12]}"),
            "role": str(role or "user"),
            "text": str(text or ""),
            "ts": time.time(),
        }
        if task_id:
            entry["task_id"] = str(task_id)
        if run_id:
            entry["run_id"] = str(run_id)
        if trace_path:
            entry["trace_path"] = str(trace_path)
        if checkpoint_path:
            entry["checkpoint_path"] = str(checkpoint_path)
        if metadata:
            entry["metadata"] = _redact_value(dict(metadata))
        validated = _validate_turn(entry)
        if validated is None:
            return
        entry = validated
        turns.append(dict(entry))
        raw_turns.append(dict(entry))
        del turns[: max(0, len(turns) - _MAX_TURNS)]
        del raw_turns[: max(0, len(raw_turns) - _MAX_RAW_TURNS)]
        session["updated_ts"] = entry["ts"]
        if run_id:
            session["active_run_id"] = str(run_id)
        log_root = session.get("_log_root")
        if log_root:
            _append_event_record(
                log_root,
                session,
                "turn_appended",
                {"turn": entry, "event_id": entry["event_id"]},
            )
    except Exception:
        return


def append_session_event(
    session: Dict[str, Any],
    event_type: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Append one redacted structured event to a conversation journal."""
    if not isinstance(session, dict):
        return None
    log_root = session.get("_log_root")
    if not log_root:
        return None
    return _append_event_record(log_root, session, event_type, payload or {})


def link_turn_to_run(
    session: Dict[str, Any],
    turn_id: str,
    run_id: str,
    *,
    task_id: Optional[str] = None,
    trace_path: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
) -> bool:
    """Link a durable conversation turn to one agent run and trace."""
    if not isinstance(session, dict) or not str(turn_id or "") or not str(run_id or ""):
        return False
    changed = False
    for collection_name in ("turns", "raw_turns", "compacted_turns"):
        collection = session.get(collection_name)
        if not isinstance(collection, list):
            continue
        for turn in collection:
            if not isinstance(turn, dict) or str(turn.get("turn_id") or "") != str(
                turn_id
            ):
                continue
            turn["run_id"] = str(run_id)
            if task_id:
                turn["task_id"] = str(task_id)
            if trace_path:
                turn["trace_path"] = str(trace_path)
            if checkpoint_path:
                turn["checkpoint_path"] = str(checkpoint_path)
            changed = True
    if changed:
        session["active_run_id"] = str(run_id)
        session["updated_ts"] = time.time()
        append_session_event(
            session,
            "run_linked",
            {
                "turn_id": str(turn_id),
                "run_id": str(run_id),
                "task_id": task_id or "",
                "trace_path": trace_path or "",
                "checkpoint_path": checkpoint_path or "",
            },
        )
    return changed


def append_history(session: Dict[str, Any], line: str) -> None:
    """Append one raw input line to the session history."""
    if not isinstance(session, dict) or session.get("_read_only"):
        return
    try:
        text = _redact_text(line or "").strip()
        if not text:
            return
        hist = session.get("history")
        if not isinstance(hist, list):
            hist = session["history"] = []
        if hist and hist[-1] == text:
            return
        hist.append(text)
        del hist[: max(0, len(hist) - _MAX_HISTORY)]
        session["updated_ts"] = time.time()
    except Exception:
        return


def set_active_run(session: Dict[str, Any], run_id: Any) -> None:
    """Set or clear the active run id used by restart/resume surfaces."""
    if isinstance(session, dict) and not session.get("_read_only"):
        session["active_run_id"] = str(run_id) if run_id else None
        session["updated_ts"] = time.time()


def set_model_profile(session: Dict[str, Any], profile: Any) -> None:
    """Persist the selected model profile without putting credentials in it."""
    if not isinstance(session, dict) or session.get("_read_only"):
        return
    if isinstance(profile, Mapping):
        safe = _strip_sensitive(profile)
        session["model_profile"] = safe if isinstance(safe, dict) else {}
    else:
        session["model_profile"] = _redact_text(profile or "")
    session["updated_ts"] = time.time()


def set_unresolved_questions(session: Dict[str, Any], questions: Any) -> None:
    """Replace the durable unresolved-question list."""
    if not isinstance(session, dict) or session.get("_read_only"):
        return
    if isinstance(questions, str):
        values = [questions]
    elif isinstance(questions, (list, tuple)):
        values = [str(item) for item in questions if str(item).strip()]
    else:
        values = []
    session["unresolved_questions"] = values
    session["updated_ts"] = time.time()


def set_workspace_mode(session: Dict[str, Any], mode: Any) -> None:
    """Persist the workspace mode label without changing execution policy."""
    if isinstance(session, dict) and not session.get("_read_only"):
        session["workspace_mode"] = str(mode or "live")
        session["updated_ts"] = time.time()


def expand_at_mentions(
    text: str,
    repo: Any,
    files: Optional[List[str]] = None,
    max_snippets: int = 3,
    max_chars: int = _MAX_SNIPPET_CHARS,
) -> Tuple[str, List[str]]:
    """Expand ``@path`` mentions into file-context blocks.

    Assumes text is the user's raw line and repo the session repo.
    Each ``@token`` that resolves to a repo file appends a fenced
    context block (first lines, capped); unresolvable tokens are left
    verbatim. Returns (expanded_text, inserted_relpaths). Never raises.
    """
    try:
        raw = str(text or "")
    except Exception:
        return str(text or ""), []
    if "@" not in raw:
        return raw, []
    try:
        root = Path(str(repo or ""))
    except Exception:
        return raw, []
    inserted: List[str] = []
    blocks: List[str] = []
    try:
        tokens = [m.group(1).rstrip(_TRAILING_PUNCT) for m in _AT_PAT.finditer(raw)]
    except Exception:
        return raw, []
    seen = set()
    for token in tokens:
        if not token or token in seen:
            continue
        seen.add(token)
        if len(inserted) >= max(1, max_snippets):
            break
        rel = _resolve_mention(token, root, files or [])
        if rel is None:
            continue
        snippet = _read_snippet(root, rel, max_chars=max_chars)
        if snippet is None:
            continue
        inserted.append(rel)
        blocks.append(f"--- {rel}\n{snippet}")
    if not blocks:
        return raw, []
    return raw + "\n\n@path context:\n" + "\n".join(blocks), inserted


def expand_symbol_mentions(
    text: str,
    repo: Any,
    max_symbols: int = 3,
    max_chars: int = _MAX_SNIPPET_CHARS,
) -> Tuple[str, List[str]]:
    """Attach indexed symbol ranges for ``@symbol`` mentions without executing code."""
    raw = str(text or "")
    if "@" not in raw:
        return raw, []
    try:
        root = Path(str(repo or "")).resolve()
        from harness.retrieval import retrieve_symbol

        tokens = [
            match.group(1).rstrip(_TRAILING_PUNCT) for match in _AT_PAT.finditer(raw)
        ]
    except Exception:
        return raw, []
    inserted: List[str] = []
    blocks: List[str] = []
    for token in tokens:
        if not token or len(inserted) >= max(1, int(max_symbols)):
            break
        try:
            if (root / token).is_file():
                continue
            records = retrieve_symbol(
                str(root),
                token,
                context_lines=2,
                include_source=True,
                limit=1,
            )
        except Exception:
            records = []
        if not records:
            continue
        record = records[0] if isinstance(records[0], Mapping) else {}
        path = str(record.get("file") or record.get("path") or "")
        if not path:
            continue
        line = max(1, int(record.get("line") or 1))
        end_line = max(line, int(record.get("end_line") or line))
        body = str(record.get("text") or record.get("source") or "")[
            : max(1, int(max_chars))
        ]
        label = f"{token} ({path}:{line}-{end_line})"
        inserted.append(label)
        blocks.append(f"--- {label}\n{body}")
    if not blocks:
        return raw, []
    return raw + "\n\n@symbol context:\n" + "\n".join(blocks), inserted


def _contained_repo_relative(root: Path, value: Any) -> Optional[str]:
    try:
        raw = Path(str(value))
        root_resolved = root.resolve()
        candidate = (root / raw).resolve()
        if not candidate.is_file():
            return None
        root_key = os.path.normcase(os.path.abspath(str(root_resolved)))
        candidate_key = os.path.normcase(os.path.abspath(str(candidate)))
        if os.path.commonpath((root_key, candidate_key)) != root_key:
            return None
        rel = os.path.relpath(str(candidate), str(root_resolved))
        rel_path = Path(rel)
        if (
            rel_path.is_absolute()
            or rel == os.pardir
            or rel.startswith(os.pardir + os.sep)
        ):
            return None
        return rel_path.as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _mention_norm(value: Any) -> str:
    return os.path.normcase(str(value or "").replace("\\", "/")).replace("\\", "/")


def _resolve_mention(token: str, root: Path, files: List[str]) -> Optional[str]:
    try:
        direct = _contained_repo_relative(root, token)
        if direct is not None:
            return direct
        safe_files: List[str] = []
        for raw in files or []:
            candidate = _contained_repo_relative(root, raw)
            if candidate is not None and candidate not in safe_files:
                safe_files.append(candidate)
        normalized = _mention_norm(token)
        base = normalized.rsplit("/", 1)[-1]
        exact = [
            f
            for f in safe_files
            if _mention_norm(f) == normalized
            or _mention_norm(f).endswith("/" + normalized)
        ]
        if len(exact) == 1:
            return exact[0]
        bases = [f for f in safe_files if _mention_norm(f).rsplit("/", 1)[-1] == base]
        if len(bases) == 1:
            return bases[0]
        if bases:
            try:
                from cli import fuzzy as _fz

                ranked = _fz.rank(bases, token)
                for candidate, _score in ranked or []:
                    if _contained_repo_relative(root, candidate) is not None:
                        return candidate
            except Exception:
                return sorted(bases)[0]
    except Exception:
        return None
    return None


def _read_snippet(
    root: Path, rel: str, max_chars: int = _MAX_SNIPPET_CHARS
) -> Optional[str]:
    """First lines of a repo file (None when unreadable/binary)."""
    try:
        safe_rel = _contained_repo_relative(root, rel)
        if safe_rel is None:
            return None
        data = (root / safe_rel).read_bytes()[: max(256, max_chars)]
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()[:_MAX_SNIPPET_LINES]
    snippet = "\n".join(lines).strip()
    if not snippet:
        return None
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n... [truncated]"
    return snippet


def compact_session(
    session: Dict[str, Any],
    log_root: Any,
    keep_last: int = 12,
) -> str:
    """Compact active turns while retaining every raw turn and summary.

    Active context is reduced to the most recent ``keep_last`` turns. The
    removed turns are copied to ``compacted_turns`` and remain in
    ``raw_turns`` for retrieval. Each compaction is recorded in
    ``summaries`` so repeated compactions do not erase prior structure.
    """
    if not isinstance(session, dict) or session.get("_read_only"):
        return ""
    try:
        turns = session.get("turns")
        if not isinstance(turns, list) or len(turns) <= max(1, int(keep_last)):
            return str(session.get("summary") or "")
        before = copy.deepcopy(session)
        split = len(turns) - max(1, int(keep_last))
        old = turns[:split]
        recent = turns[split:]
        bits: List[str] = []
        previous = _redact_text(session.get("summary") or "").strip()
        if previous:
            bits.append(f"prior: {previous[:1500]}")
        for turn in old:
            if not isinstance(turn, dict):
                continue
            role = _redact_text(turn.get("role") or "?")
            text = _redact_text(turn.get("text") or "").replace("\n", " ").strip()
            task_id = _redact_text(turn.get("task_id") or "")
            suffix = f" {task_id}" if task_id else ""
            bits.append(f"{role}{suffix}: {text[:240]}")
        recall_notes = _recall_task_outcomes(old, log_root)
        if recall_notes:
            bits.append("recalled outcomes: " + "; ".join(recall_notes)[:1500])
        summary = "\n".join(bits)[:4000]
        compacted = session.get("compacted_turns")
        if not isinstance(compacted, list):
            compacted = session["compacted_turns"] = []
        compacted.extend(copy.deepcopy(turn) for turn in old)
        session["compacted_turns"] = compacted[-_MAX_COMPACTED_TURNS:]
        summaries = session.get("summaries")
        if not isinstance(summaries, list):
            summaries = session["summaries"] = []
        summary_record = {
            "ts": time.time(),
            "turn_count": len(old),
            "source_turn_ids": [
                str(item.get("turn_id") or "") for item in old if isinstance(item, dict)
            ],
            "text": summary,
        }
        summaries.append(summary_record)
        session["summaries"] = summaries[-_MAX_COMPACTED_TURNS:]
        session["summary"] = summary
        session["turns"] = recent[-_MAX_TURNS:]
        session["compaction_count"] = int(session.get("compaction_count") or 0) + 1
        if log_root:
            _append_event_record(
                log_root,
                session,
                "compaction",
                {
                    "summary": summary,
                    "turns": copy.deepcopy(session["turns"]),
                    "compacted_turns": copy.deepcopy(old),
                    "turn_count": len(old),
                    "source_turn_ids": summary_record["source_turn_ids"],
                },
            )
        if not save_session(log_root, session):
            session.clear()
            session.update(before)
            return ""
        return summary
    except Exception:
        return ""


def _recall_task_outcomes(turns: List[Any], log_root: Any) -> List[str]:
    """Read result and verify events for task ids referenced by old turns."""
    out: List[str] = []
    try:
        task_ids: List[str] = []
        for turn in turns:
            if isinstance(turn, dict):
                task_id = str(turn.get("task_id") or "")
                if task_id and task_id not in task_ids:
                    task_ids.append(task_id)
        from harness.trace import TraceLogger
        from memory.paths import safe_task_dir

        for task_id in task_ids[:5]:
            task_dir = safe_task_dir(task_id, Path(log_root))
            if task_dir is None:
                continue
            logger = TraceLogger(task_dir)
            entries: List[Dict[str, Any]] = []
            for kind in ("result", "verify"):
                entries.extend(logger.find_events(kind, kinds=[kind], limit=3))
            for entry in entries[-3:]:
                kind = str(entry.get("kind") or "?")
                data = _redact_text(entry.get("data") or "")[:160]
                out.append(f"{task_id} {kind}: {data}")
    except Exception:
        return out
    return out


def retrieve_session_turns(
    session: Dict[str, Any],
    query: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Retrieve historical turns from the event journal without activating them."""
    if not isinstance(session, dict):
        return []
    source: List[Dict[str, Any]] = []
    log_root = session.get("_log_root")
    if log_root:
        try:
            for record in _read_event_records(
                _event_log_path(log_root, str(session.get("session_id") or "")),
                strict=False,
            ):
                if str(record.get("event") or "") != "turn_appended":
                    continue
                payload = record.get("payload")
                turn = payload.get("turn") if isinstance(payload, dict) else None
                validated = _validate_turn(turn)
                if validated is not None:
                    source.append(validated)
        except Exception:
            source = []
    if not source:
        raw = session.get("raw_turns")
        if not isinstance(raw, list):
            raw = session.get("compacted_turns") or session.get("turns") or []
        source = [dict(item) for item in raw if isinstance(item, dict)]
    needle = str(query or "").strip().casefold()
    matches: List[Dict[str, Any]] = []
    for turn in source:
        if not isinstance(turn, dict):
            continue
        haystack = " ".join(
            str(turn.get(key) or "")
            for key in ("role", "text", "task_id", "run_id", "turn_id")
        ).casefold()
        if not needle or needle in haystack:
            matches.append(dict(turn))
    try:
        bounded_limit = max(1, int(limit))
    except (TypeError, ValueError):
        bounded_limit = 20
    return matches[-bounded_limit:]


def reconstruct_session(
    log_root: Any,
    repo: Any = None,
    session_id: Optional[str] = None,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Reconstruct a bounded session view from its durable event journal."""
    session = resume_session(log_root, repo, session_id, strict=strict)
    sid = str(session.get("session_id") or "")
    events = (
        _read_event_records(
            _event_log_path(log_root, sid),
            strict=strict,
        )
        if sid
        else []
    )
    turns = retrieve_session_turns(session, limit=100000)
    return {
        "status": "ok",
        "session_id": sid,
        "session": session,
        "turns": turns,
        "recent_turns": list(session.get("turns") or []),
        "summary": str(session.get("summary") or ""),
        "events": events,
        "event_count": len(events),
        "last_event_sequence": int(session.get("event_sequence") or 0),
        "compaction_count": int(session.get("compaction_count") or 0),
        "lineage": list(session.get("lineage") or []),
    }


def _session_from_source(
    log_root: Any,
    source: Any,
    repo: Any = None,
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    if isinstance(source, dict):
        result = _normalise_session(source, str(source.get("session_id") or ""), repo)
        result["_log_root"] = str(log_root or "")
        return result
    return load_or_create(log_root, repo, str(source), strict=strict)


def fork_session(
    log_root: Any,
    source: Any,
    repo: Any = None,
    *,
    session_id: Optional[str] = None,
    at_turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fork a conversation at a durable turn boundary."""
    base = _session_from_source(log_root, source, repo)
    selected_turns = [
        dict(item) for item in base.get("turns") or [] if isinstance(item, dict)
    ]
    if at_turn_id:
        indexes = [
            index
            for index, item in enumerate(selected_turns)
            if str(item.get("turn_id") or "") == str(at_turn_id)
        ]
        if not indexes:
            raise SessionImportError(
                "fork turn is not present in the source conversation"
            )
        selected_turns = selected_turns[: indexes[-1] + 1]
    fork_id = str(session_id or new_session_id()).strip()
    if not _valid_session_id(fork_id):
        raise ValueError("invalid forked conversation session id")
    fork = _new_session(fork_id, repo or base.get("repo"))
    fork["_log_root"] = str(log_root or "")
    fork["parent_session_id"] = str(base.get("session_id") or "")
    fork["fork_point_turn_id"] = str(
        at_turn_id or (selected_turns[-1].get("turn_id") if selected_turns else "")
    )
    fork["source_event_sequence"] = int(base.get("event_sequence") or 0)
    fork["turns"] = copy.deepcopy(selected_turns[-_MAX_TURNS:])
    fork["raw_turns"] = copy.deepcopy(selected_turns[-_MAX_RAW_TURNS:])
    fork["summary"] = _redact_text(base.get("summary") or "")
    fork["summaries"] = copy.deepcopy(base.get("summaries") or [])[
        -_MAX_COMPACTED_TURNS:
    ]
    fork["lineage"] = [
        *list(base.get("lineage") or []),
        {
            "parent_session_id": fork["parent_session_id"],
            "fork_point_turn_id": fork["fork_point_turn_id"],
            "created_at": time.time(),
        },
    ]
    _append_event_record(
        log_root,
        fork,
        "session_forked",
        {
            "parent_session_id": fork["parent_session_id"],
            "fork_point_turn_id": fork["fork_point_turn_id"],
            "copied_turn_count": len(selected_turns),
            "source_event_sequence": int(base.get("event_sequence") or 0),
        },
    )
    for turn in selected_turns:
        _append_event_record(
            log_root,
            fork,
            "turn_appended",
            {"turn": turn, "event_id": turn.get("event_id") or ""},
        )
    if not save_session(log_root, fork):
        raise SessionRecoveryError("forked conversation could not be persisted")
    try:
        index_conversation(
            fork,
            log_root,
            status="forked",
            resumable=False,
            source="fork",
        )
    except Exception:
        pass
    return fork


def _session_export_payload(
    log_root: Any,
    session: Dict[str, Any],
    *,
    mode: str = "redacted",
) -> Dict[str, Any]:
    from shared.privacy import apply_privacy_mode

    selected = apply_privacy_mode(_serialisable_session(session), mode)
    events: List[Any] = []
    try:
        raw_events = _read_event_records(
            _event_log_path(log_root, str(session.get("session_id") or "")),
            strict=False,
        )
        events = [apply_privacy_mode(item, mode) for item in raw_events]
    except Exception:
        events = []
    return {
        "kind": "neo-session-export",
        "schema_version": _EXPORT_SCHEMA_VERSION,
        "exported_at": time.time(),
        "source_session_id": str(session.get("session_id") or ""),
        "workspace_identity": apply_privacy_mode(
            session.get("workspace_identity") or {},
            mode,
        ),
        "session": selected,
        "events": events,
    }


def export_session_document(
    log_root: Any,
    source: Any,
    repo: Any = None,
    *,
    mode: str = "redacted",
) -> Dict[str, Any]:
    """Return a privacy-filtered, JSON-compatible session export."""
    session = _session_from_source(log_root, source, repo)
    return _session_export_payload(log_root, session, mode=mode)


def export_session(
    log_root: Any,
    source: Any,
    destination: Any = None,
    repo: Any = None,
    *,
    mode: str = "redacted",
    privacy: Optional[str] = None,
) -> Any:
    """Export a session to a path, or return its document when no path is given."""
    selected_mode = privacy if privacy is not None else mode
    document = export_session_document(log_root, source, repo, mode=selected_mode)
    if destination is None:
        return document
    path = Path(destination)
    if path.is_symlink():
        raise SessionImportError(
            "session export destination must not be a symbolic link"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    if len(payload) > _MAX_SESSION_BYTES:
        raise SessionImportError("session export exceeds the size limit")
    _atomic_write_bytes(path, payload)
    return str(path)


def import_session(
    source: Any,
    log_root: Any,
    repo: Any = None,
    *,
    session_id: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Validate and import a session export as a new durable conversation."""
    if isinstance(source, (str, os.PathLike)) and isinstance(
        log_root, (str, os.PathLike)
    ):
        possible_root = Path(source)
        possible_source = Path(log_root)
        if possible_root.is_dir() and (
            possible_source.is_file() or possible_source.suffix.casefold() == ".json"
        ):
            source, log_root = possible_source, possible_root
    if isinstance(source, (str, os.PathLike)):
        source_path = Path(source)
        if source_path.is_symlink():
            raise SessionImportError(
                "session import source must not be a symbolic link"
            )
        try:
            document = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SessionImportError("session import source is not valid JSON") from exc
    else:
        document = source
    if not isinstance(document, dict):
        raise SessionImportError("session import root must be an object")
    if document.get("kind") not in (None, "neo-session-export"):
        raise SessionImportError("unsupported session export kind")
    try:
        version = int(document.get("schema_version", _EXPORT_SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise SessionImportError("session export schema version is invalid") from exc
    if version != _EXPORT_SCHEMA_VERSION:
        raise SessionImportError("unsupported session export schema version")
    exported = document.get("session")
    if not isinstance(exported, dict):
        raise SessionImportError("session export has no session object")
    if exported.get("active_run_id"):
        raise SessionImportError("cannot import a session with an active run")
    sid = str(session_id or new_session_id()).strip()
    if not _valid_session_id(sid):
        raise ValueError("invalid imported conversation session id")
    target_path = _session_path(log_root, sid)
    base_revision = 0
    if target_path.exists():
        if not overwrite:
            raise SessionImportError("imported session id already exists")
        try:
            base_revision = int(
                load_or_create(log_root, repo, sid, strict=True).get("_revision") or 0
            )
        except SessionCorruptError as exc:
            raise SessionImportError("existing imported session is corrupt") from exc
    imported = copy.deepcopy(exported)
    imported["session_id"] = sid
    imported["schema_version"] = _SCHEMA_VERSION
    imported["active_run_id"] = None
    imported["_revision"] = base_revision
    imported["integrity"] = ""
    imported.pop("integrity", None)
    try:
        session = _normalise_session(imported, sid, repo)
    except SessionCorruptError as exc:
        raise SessionImportError(str(exc)) from exc
    session["_log_root"] = str(log_root or "")
    session["parent_session_id"] = str(
        document.get("source_session_id") or exported.get("session_id") or ""
    )
    event_path = _event_log_path(log_root, sid)
    session["event_log_path"] = str(event_path)
    raw_events = document.get("events")
    if raw_events is not None and not isinstance(raw_events, list):
        raise SessionImportError("session export events must be a list")
    if isinstance(raw_events, list):
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with _session_write_lock(event_path):
            if event_path.exists() and not overwrite:
                raise SessionImportError("imported event journal already exists")
            lines: List[bytes] = []
            for index, raw_event in enumerate(raw_events, start=1):
                if not isinstance(raw_event, dict):
                    raise SessionImportError(
                        "session export contains a malformed event"
                    )
                if not str(raw_event.get("event") or "").strip():
                    raise SessionImportError("session export event has no type")
                if not isinstance(raw_event.get("payload", {}), Mapping):
                    raise SessionImportError(
                        "session export event payload is malformed"
                    )
                try:
                    event_version = int(
                        raw_event.get("schema_version", _EVENT_SCHEMA_VERSION)
                    )
                    sequence = int(raw_event.get("sequence", index))
                except (TypeError, ValueError) as exc:
                    raise SessionImportError(
                        "session export event metadata is invalid"
                    ) from exc
                if event_version != _EVENT_SCHEMA_VERSION or sequence != index:
                    raise SessionImportError("session export event sequence is invalid")
                event = _redact_value(dict(raw_event))
                event["schema_version"] = _EVENT_SCHEMA_VERSION
                event["sequence"] = index
                event["session_id"] = sid
                lines.append(
                    (
                        json.dumps(
                            event, ensure_ascii=False, sort_keys=True, default=str
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            payload = b"".join(lines)
            _atomic_write_bytes(event_path, payload)
        session["event_sequence"] = len(raw_events)
        session["event_count"] = len(raw_events)
    if not save_session(log_root, session):
        raise SessionImportError("imported session could not be persisted")
    try:
        index_conversation(
            session,
            log_root,
            status="imported",
            resumable=False,
            source="import",
        )
    except Exception:
        pass
    return session


def promote_session_decision(
    session: Dict[str, Any],
    text: str,
    *,
    turn_id: str,
    run_id: str = "",
    repo: Any = None,
    category: str = "general",
    confirmed: bool = True,
    actor: str = "user",
) -> Optional[int]:
    """Promote only an explicitly confirmed, provenance-linked decision."""
    if not isinstance(session, dict) or not confirmed or not str(turn_id or "").strip():
        return None
    available_turns = [
        item
        for collection_name in ("turns", "raw_turns", "compacted_turns")
        for item in (session.get(collection_name) or [])
        if isinstance(item, dict)
    ]
    if not any(
        str(item.get("turn_id") or "") == str(turn_id) for item in available_turns
    ):
        return None
    try:
        from memory.decision_store import open_default_store

        store = open_default_store()
        try:
            decision_id = store.promote_decision(
                text=text,
                category=category,
                source="session",
                repo_path=str(repo or session.get("repo") or "") or None,
                provenance={
                    "kind": "session-decision",
                    "explicit": True,
                    "actor": str(actor or "user")[:64],
                    "turn_id": str(turn_id),
                    "run_id": str(run_id or "")[:128],
                },
                dedupe=True,
            )
        finally:
            store.close()
    except Exception:
        return None
    if decision_id is not None:
        append_session_event(
            session,
            "decision_promoted",
            {"decision_id": decision_id, "turn_id": turn_id, "run_id": run_id},
        )
    return decision_id


def create_checkpoint(*args: Any, **kwargs: Any) -> Any:
    """Create a durable workspace checkpoint through the memory adapter."""
    from memory.checkpoints import create_checkpoint as _create_checkpoint

    return _create_checkpoint(*args, **kwargs)


def checkpoint_before_mutation(*args: Any, **kwargs: Any) -> Any:
    """Create a checkpoint before a caller-authorized mutation."""
    from memory.checkpoints import checkpoint_before_mutation as _checkpoint

    return _checkpoint(*args, **kwargs)


def list_checkpoints(*args: Any, **kwargs: Any) -> Any:
    """List durable checkpoints for a workspace."""
    from memory.checkpoints import list_checkpoints as _list

    return _list(*args, **kwargs)


def load_checkpoint(*args: Any, **kwargs: Any) -> Any:
    """Load one durable checkpoint record."""
    from memory.checkpoints import load_checkpoint as _load

    return _load(*args, **kwargs)


def review_checkpoint(*args: Any, **kwargs: Any) -> Any:
    """Review drift between a workspace and a checkpoint."""
    from memory.checkpoints import review_checkpoint as _review

    return _review(*args, **kwargs)


def diff_checkpoint(*args: Any, **kwargs: Any) -> Any:
    """Return a bounded checkpoint diff."""
    from memory.checkpoints import diff_checkpoint as _diff

    return _diff(*args, **kwargs)


def restore_checkpoint(*args: Any, **kwargs: Any) -> Any:
    """Restore files and/or a conversation through the checkpoint adapter."""
    from memory.checkpoints import restore_checkpoint as _restore

    return _restore(*args, **kwargs)


session_fork = fork_session
export_conversation = export_session
export_session_file = export_session
import_conversation = import_session
recover_conversation = recover_session
rebuild_session = reconstruct_session
create_session_checkpoint = create_checkpoint
restore_session_checkpoint = restore_checkpoint


def build_session_context(
    session: Optional[Dict[str, Any]] = None,
    repo: Any = None,
    task: Any = None,
    prior_diff: str = "",
    selected_files: Optional[List[str]] = None,
    project_instructions: Any = None,
    skills: Any = None,
    decision_memory: Any = None,
    decision_store: Any = None,
    token_budget: int = 12000,
) -> Dict[str, Any]:
    """Build a bounded context bundle through the owned project-context API."""
    from memory.project_context import build_context

    return build_context(
        session=session,
        repo_path=repo,
        task=task,
        prior_diff=prior_diff,
        selected_files=selected_files,
        project_instructions=project_instructions,
        skills=skills,
        decision_memory=decision_memory,
        decision_store=decision_store,
        token_budget=token_budget,
    )


def format_session_context_status(bundle: Dict[str, Any]) -> str:
    """Render the context source and size receipt for a status surface."""
    from memory.project_context import format_context_status

    return format_context_status(bundle)


def session_memory_brief(repo: Any, log_root: Any, limit: int = 5) -> List[str]:
    """Automatic memory context for a fresh session (never raises).

    Queries only the selected repository's scoped decisions and its
    persisted code-graph index. Returns short human lines; [] when
    nothing applies.
    """
    repo_value = str(repo or "").strip()
    if not repo_value:
        return []
    lines: List[str] = []
    try:
        from memory.decision_store import open_default_store

        try:
            store = open_default_store()
        except Exception:
            store = None
        if store is not None:
            try:
                rows = store.search(
                    "", limit=max(1, min(int(limit), 10)), repo_path=repo_value
                )
                for row in rows[: max(1, min(int(limit), 10))]:
                    text = str(getattr(row, "text", "") or "").strip()
                    if text:
                        lines.append(f"memory: {text[:160]}")
            except Exception:
                pass
            try:
                store.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        from memory.code_graph import CodeGraph

        try:
            graph = CodeGraph(repo_value).load()
        except Exception:
            graph = None
        if graph is not None:
            try:
                files = int(getattr(graph, "file_count", 0) or 0)
                nodes = len(getattr(graph, "nodes", {}) or {})
                if files:
                    lines.append(f"structure: {files} files, {nodes} symbols indexed")
            except Exception:
                pass
    except Exception:
        pass
    return lines[:10]


def ingest_session_facts(
    log_root: Any,
    task_id: str,
    issue: str,
    repo: Any,
    status: str,
    *,
    promote: bool = False,
    decision_text: str = "",
    turn_id: str = "",
    run_id: str = "",
    actor: str = "user",
) -> None:
    """Ingest explicit state decisions without promoting ordinary session text."""
    try:
        tid = str(task_id or "").strip()
        if not tid:
            return
        try:
            from memory.decision_store import open_default_store

            store = open_default_store()
        except Exception:
            store = None
        if store is not None:
            try:
                store.poll(str(log_root))
            except Exception:
                pass
            if promote and decision_text and turn_id:
                try:
                    store.promote_decision(
                        text=decision_text,
                        category="session",
                        source="session",
                        task_id=tid,
                        repo_path=str(repo or "") or None,
                        provenance={
                            "kind": "session-decision",
                            "explicit": True,
                            "actor": str(actor or "user")[:64],
                            "turn_id": str(turn_id),
                            "run_id": str(run_id or "")[:128],
                        },
                        dedupe=True,
                    )
                except Exception:
                    pass
            try:
                store.close()
            except Exception:
                pass
    except Exception:
        return


def copy_text_to_clipboard(text: str) -> bool:
    """Copy text to the OS clipboard (True on success; never raises).

    Uses platform clipboard utilities (no new dependency): Windows
    ``clip``, macOS ``pbcopy``, Linux ``xclip``/``xsel``. Falls back
    to False so callers print the text instead.
    """
    try:
        import subprocess

        data = (text or "").encode("utf-8", errors="replace")
        if not data:
            return False
        import os

        if os.name == "nt":
            proc = subprocess.run(["clip"], input=data, capture_output=True, timeout=10)
            return proc.returncode == 0
        import shutil

        if shutil.which("pbcopy"):
            proc = subprocess.run(
                ["pbcopy"], input=data, capture_output=True, timeout=10
            )
            return proc.returncode == 0
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard"]):
            if shutil.which(cmd[0]):
                proc = subprocess.run(cmd, input=data, capture_output=True, timeout=10)
                if proc.returncode == 0:
                    return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Safe default artifact location (VEX-CEILING-03 requirement 1)
# ---------------------------------------------------------------------------


def resolve_artifact_root(
    explicit: Any = None,
    repo: Any = None,
    file_config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the ONE log/artifact root every session surface shares.

    Precedence: explicit flag > settings ``log_root`` > the harness-owned
    home's per-repository default (``memory.paths.default_logs_dir``,
    outside the user's repository). A root that lands inside a git
    repository — which happens whenever a user FORCES one — is added to
    ``.gitignore`` and reported through ``warnings``; nothing is silent.

    Returns a dict with ``log_root`` (Path), ``source``
    (``flag`` | ``config`` | ``default``), ``placement`` (the gitignore
    receipt) and ``warnings``. Never raises.
    """
    from memory import paths as _paths

    source = "default"
    raw: Optional[str] = None
    if explicit is not None and str(explicit).strip():
        source = "flag"
        raw = str(explicit)
    else:
        config = file_config
        if config is None:
            try:
                from cli.neoconfig import merged_settings

                config = merged_settings()
            except Exception:
                config = {}
        candidate = (config or {}).get("log_root")
        if isinstance(candidate, str) and candidate.strip():
            source = "config"
            raw = candidate
    if source == "default":
        root = _paths.default_logs_dir(repo)
    else:
        root = Path(str(raw)).expanduser()
        try:
            root = root.resolve()
        except (OSError, RuntimeError, ValueError):
            root = root.absolute()
    placement: Dict[str, Any] = {}
    warnings: Tuple[str, ...] = ()
    try:
        placement = _paths.ensure_log_root_ignored(root, repo)
        warnings = _paths.artifact_root_warnings([placement])
    except Exception:
        placement = {}
    return {
        "log_root": root,
        "source": source,
        "placement": placement,
        "warnings": warnings,
        "repo_key": _paths.repo_key(repo),
    }


# ---------------------------------------------------------------------------
# Global per-repo session index (VEX-CEILING-03 requirement 2)
#
# A single harness-owned, COMPACTED index so `/sessions` and `--continue`
# work from any CWD and can search across repositories without re-reading
# every task trace.
#
# Layout under ``memory.paths.neo_home()``:
#
#     session-index/index.jsonl   append-only journal of index rows
#     session-index/index.json    compacted snapshot (the fast read path)
#
# The snapshot stores rows as arrays keyed by ``_INDEX_FIELDS`` (an
# array-of-arrays parses several times faster than 5,000 objects, which is
# what keeps listing well under the 100ms p95 budget). The journal is
# read as a TAIL from the byte offset the snapshot recorded, so a listing
# after N appends still parses N deltas, not N rows. Compaction folds the
# journal into the snapshot and truncates it; the merge is idempotent, so
# a crash between the two steps loses nothing.
#
# IMPORTANT: the index is a LOOKUP structure, not a status authority. It
# never opens a trace.jsonl. The surface that acts on a row (resume,
# continue) re-verifies that one run against its own journal via the
# caller-supplied `verify` callable.
# ---------------------------------------------------------------------------

_INDEX_SCHEMA_VERSION = 1
_INDEX_FIELDS: Tuple[str, ...] = (
    "session_id",
    "repo_key",
    "repo_path",
    "repo_name",
    "branch",
    "worktree",
    "task_id",
    "status",
    "resumable",
    "issue",
    "updated_at",
    "turn_count",
    "log_root",
    "parent_session_id",
    "fork_point_turn_id",
    # R2-17: the HONEST verdict label for the run, resolved from the run's
    # own journal at record time. Appended LAST so every existing field's
    # positional index is unchanged; `_row_to_record` pads a short legacy
    # row with None, which `interactive.normalize_index_row` then resolves
    # fail-closed (`unverified`) rather than inheriting the lifecycle word.
    "display_status",
)
_INDEX_ROW_LIMIT = 20_000
# Compaction threshold for the journal. Deliberately small: the snapshot is
# the fast read path, and a 128 KiB tail merges in ~2ms where a multi-MB one
# costs tens of milliseconds on every listing.
_INDEX_JOURNAL_LIMIT = 128 * 1024
_INDEX_MAX_ISSUE = 200
_INDEX_LOCK_WAIT_S = 2.0
_INDEX_LOCK_STALE_S = 30.0
#: Above this requested row count, projection is not worth deferring.
_INDEX_LAZY_LIMIT = 500
_UPDATED_AT = _INDEX_FIELDS.index("updated_at")


def _index_dir() -> Path:
    from memory import paths as _paths

    return _paths.neo_home() / "session-index"


def index_dir() -> Path:
    """Directory holding the global per-repo session index."""
    return _index_dir()


def _index_journal_path() -> Path:
    return _index_dir() / "index.jsonl"


def _index_snapshot_path() -> Path:
    return _index_dir() / "index.json"


@contextmanager
def _index_write_lock():
    """Bound-wait advisory lock around one index journal append.

    The index is an enhancement: a busy or unwritable index directory
    degrades to "not indexed" (the per-log-root index and the directory
    scan still work), never to a failed session.
    """
    directory = _index_dir()
    lock = directory / ".index.lock"
    acquired = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + _INDEX_LOCK_WAIT_S
        while True:
            try:
                handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(handle)
                acquired = True
                break
            except FileExistsError:
                try:
                    age = time.time() - lock.stat().st_mtime
                    if age > _INDEX_LOCK_STALE_S:
                        os.unlink(str(lock))
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    break
                time.sleep(0.01)
            except OSError:
                break
        yield acquired
    finally:
        if acquired:
            try:
                os.unlink(str(lock))
            except OSError:
                pass


def _index_row(record: Mapping[str, Any]) -> List[Any]:
    return [record.get(field) for field in _INDEX_FIELDS]


def _row_to_record(row: Any) -> Optional[Dict[str, Any]]:
    if isinstance(row, Mapping):
        return {field: row.get(field) for field in _INDEX_FIELDS}
    if isinstance(row, (list, tuple)):
        if len(row) > len(_INDEX_FIELDS):
            return None
        padded = list(row) + [None] * (len(_INDEX_FIELDS) - len(row))
        return dict(zip(_INDEX_FIELDS, padded, strict=False))
    return None


def _normalise_index_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Project any caller record onto the fixed index schema (total)."""
    from memory import paths as _paths

    repo = record.get("repo_path") or record.get("repo") or ""
    repo_path = str(repo) if repo else ""
    try:
        key = str(record.get("repo_key") or _paths.repo_key(repo_path or None))
    except Exception:
        key = str(record.get("repo_key") or "")
    try:
        updated = float(record.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated = 0.0
    try:
        turns = int(record.get("turn_count") or 0)
    except (TypeError, ValueError):
        turns = 0
    return {
        "session_id": str(record.get("session_id") or ""),
        "repo_key": key,
        "repo_path": repo_path,
        "repo_name": str(record.get("repo_name") or "")
        or (Path(repo_path).name if repo_path else ""),
        "branch": str(record.get("branch") or ""),
        "worktree": str(record.get("worktree") or ""),
        "task_id": str(record.get("task_id") or ""),
        "status": str(record.get("status") or "unknown"),
        "display_status": str(record.get("display_status") or ""),
        "resumable": bool(record.get("resumable")),
        "issue": str(record.get("issue") or "")[:_INDEX_MAX_ISSUE],
        "updated_at": updated,
        "turn_count": turns,
        "log_root": str(record.get("log_root") or ""),
        "parent_session_id": str(record.get("parent_session_id") or ""),
        "fork_point_turn_id": str(record.get("fork_point_turn_id") or ""),
    }


def index_session(record: Mapping[str, Any]) -> bool:
    """Append one row to the global session index. Never raises.

    Returns True when the row was durably appended. A session id is the
    merge key, so repeated upserts of the same session collapse to its
    newest state at read time.
    """
    if not isinstance(record, Mapping):
        return False
    row = _normalise_index_record(record)
    if not row["session_id"] and not row["task_id"]:
        return False
    if not row["updated_at"]:
        row["updated_at"] = time.time()
    payload = json.dumps(
        _index_row(row), ensure_ascii=False, separators=(",", ":"), default=str
    )
    try:
        with _index_write_lock() as acquired:
            if not acquired:
                return False
            path = _index_journal_path()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
            # Deliberately NO fsync: the index is a lookup accelerator, not a
            # durability boundary. A torn tail is already tolerated by
            # ``_read_index_journal_tail`` (a partial final line is ignored),
            # and the conversation's own event journal remains the authority.
            try:
                oversized = path.stat().st_size > _INDEX_JOURNAL_LIMIT
            except OSError:
                oversized = False
        if oversized:
            index_compact()
        return True
    except (OSError, TypeError, ValueError):
        return False


def index_conversation(
    session: Mapping[str, Any],
    log_root: Any,
    *,
    status: str = "active",
    resumable: Optional[bool] = None,
    source: str = "conversation",
) -> bool:
    """Index one durable conversation from its own snapshot.

    Repo/branch/worktree come from the session's recorded workspace
    identity, so the index never shells out to git on the read path.
    """
    if not isinstance(session, Mapping):
        return False
    identity = session.get("workspace_identity")
    if not isinstance(identity, Mapping):
        identity = {}
    repo_path = str(
        identity.get("repo_path") or identity.get("path") or session.get("repo") or ""
    )
    # A one-line preview is what makes `/sessions <free text>` work across
    # repositories. Prefer the compacted summary; otherwise fall back to the
    # FIRST user turn, which is the question the conversation started from.
    issue = str(session.get("summary") or "")
    if not issue.strip():
        for turn in session.get("turns") or []:
            if not isinstance(turn, Mapping):
                continue
            if str(turn.get("role") or "user") in ("user", "human"):
                issue = str(turn.get("text") or "")
                if issue.strip():
                    break
    record = {
        "session_id": str(session.get("session_id") or ""),
        "repo_path": repo_path,
        "repo_name": str(identity.get("repo_name") or identity.get("name") or ""),
        "branch": str(identity.get("git_branch") or session.get("branch") or ""),
        "worktree": str(identity.get("git_worktree") or session.get("worktree") or ""),
        "task_id": str(session.get("active_run_id") or session.get("task_id") or ""),
        "status": status,
        "resumable": bool(session.get("active_run_id"))
        if resumable is None
        else bool(resumable),
        "issue": issue,
        "updated_at": float(
            session.get("updated_ts") or session.get("started_ts") or time.time()
        ),
        "turn_count": len(session.get("turns") or []),
        "event_count": int(session.get("event_count") or 0),
        "log_root": str(log_root or session.get("_log_root") or ""),
        "source": source,
        "parent_session_id": str(session.get("parent_session_id") or ""),
        "fork_point_turn_id": str(session.get("fork_point_turn_id") or ""),
    }
    return index_session(record)


def index_run(
    log_root: Any,
    task_id: str,
    issue: str = "",
    repo: Any = None,
    status: str = "completed",
    *,
    resumable: bool = False,
    session_id: str = "",
    display_status: str = "",
) -> bool:
    """Index one completed/attempted run so `--continue` can find it.

    `display_status` is the HONEST verdict label (`verified` /
    `unverified` / `pending` / …) resolved by the caller from the run's
    own journal. It is stored alongside the lifecycle `status` because
    `status` answers "can this run be continued", NOT "was it verified" —
    and every run-metadata renderer used to print the lifecycle word, so a
    `completed_unverified` run was listed with the same word as a verified
    one. An empty `display_status` is normalised here to the fail-closed
    `unverified` rather than being left absent, so a caller that forgets it
    produces an honest row instead of a silently blank one.
    """
    from memory import paths as _paths

    repo_path = str(repo or "")
    if not str(display_status or "").strip():
        display_status = "unverified"
    record = {
        "session_id": str(session_id or ""),
        "repo_path": repo_path,
        "repo_name": Path(repo_path).name if repo_path else "",
        "task_id": str(task_id or ""),
        "status": status,
        "display_status": str(display_status),
        "resumable": bool(resumable),
        "issue": issue,
        "updated_at": time.time(),
        "log_root": str(log_root or ""),
        "source": "run",
    }
    try:
        record["repo_key"] = _paths.repo_key(repo_path or None)
    except Exception:
        record["repo_key"] = ""
    return index_session(record)


def _read_index_snapshot() -> Tuple[List[Any], int]:
    """Raw compact rows from the snapshot plus the journal offset it covers.

    Rows are returned UNPROJECTED: the steady-state read projects each one
    exactly once, in `_read_index_projection`, instead of once here and
    again in the merge.
    """
    path = _index_snapshot_path()
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError):
        return [], 0
    if not isinstance(payload, Mapping):
        return [], 0
    if int(payload.get("schema_version") or 0) != _INDEX_SCHEMA_VERSION:
        return [], 0
    try:
        offset = int(payload.get("journal_bytes") or 0)
    except (TypeError, ValueError):
        offset = 0
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return [], offset
    return rows, offset


def _is_singleton_row(value: Any) -> bool:
    """True for the legacy one-element wrapper a previous format wrote."""
    return (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], (list, tuple))
    )


def _read_index_journal_tail(offset: int = 0) -> List[Any]:
    """Rows appended after `offset`; a partial trailing line is ignored.

    Each journal line is one complete JSON array, so the whole tail parses
    with a SINGLE ``json.loads`` (join the lines with commas) rather than
    one call per line. That is the difference between a 5,000-row journal
    costing ~100ms and ~15ms. Rows written by the earlier nested
    ``[[...]]`` format still parse through the per-line fallback.
    """
    path = _index_journal_path()
    try:
        size = path.stat().st_size
        if size <= offset:
            return []
        with path.open("rb") as handle:
            handle.seek(max(0, offset))
            blob = handle.read()
    except OSError:
        return []
    if not blob:
        return []
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        blob = blob[: cut + 1] if cut >= 0 else b""
    text = blob.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        decoded = json.loads("[" + text.replace("\n", ",") + "]")
    except ValueError:
        decoded = None
    if isinstance(decoded, list):
        return [_unwrap_singleton_row(item) for item in decoded]
    rows: List[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        for candidate in item if isinstance(item, list) else [item]:
            rows.append(_unwrap_singleton_row(candidate))
    return rows


def _unwrap_singleton_row(value: Any) -> Any:
    """Strip the legacy one-element wrapper a previous format wrote."""
    return value[0] if _is_singleton_row(value) else value


def _merge_index_rows(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Newest-wins merge keyed by session/task id, newest first.

    Two deliberate choices:

    - Decorate-sort-undecorate, so the sort key is computed once per row
      instead of twice per comparison.
    - The final tie-break is APPEND ORDER, not the session id. Windows'
      ``time.time()`` has a coarse resolution, so two runs recorded in the
      same tick share a timestamp; without this, "newest" would be decided
      by an id and `--continue` could pick the older run.
    """
    merged: Dict[str, Tuple[float, int, Dict[str, Any]]] = {}
    anonymous: List[Tuple[float, int, Dict[str, Any]]] = []
    width = len(_INDEX_FIELDS)
    for position, row in enumerate(rows):
        if isinstance(row, dict):
            record: Optional[Dict[str, Any]] = row
        elif type(row) is list and len(row) == width:
            record = dict(zip(_INDEX_FIELDS, row, strict=False))
        else:
            record = _row_to_record(row)
        if record is None:
            continue
        try:
            updated = float(record.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        key = str(record.get("session_id") or "")
        if not key:
            key = f"task:{record.get('task_id') or ''}"
        if not key.strip(":"):
            anonymous.append((updated, position, record))
            continue
        current = merged.get(key)
        if current is None or (updated, position) >= (current[0], current[1]):
            merged[key] = (updated, position, record)
    out = list(merged.values()) + anonymous
    out.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _updated, _position, record in out]


def index_records(
    *,
    repo: Any = None,
    query: str = "",
    limit: Optional[int] = None,
    verify: Optional[Any] = None,
    log_root: Any = None,
) -> List[Dict[str, Any]]:
    """Global session records, newest first, WITHOUT reading any trace.

    `repo` is an explicit cross-repo filter: a path, a repo key, or a repo
    directory name. `log_root` is the ISOLATION filter: when a caller
    explicitly chose one artifact root (``--log-root`` or a configured
    ``log_root``), only rows under that root are returned, so an isolated
    root never reports the machine's other repositories' sessions. `query`
    reuses the shared ``session_matches`` filter grammar when
    ``cli.interactive`` is importable and otherwise applies a plain
    substring match, so this function stays total on its own. `verify` is
    an optional predicate applied to the rows AFTER filtering (used by
    ``--continue`` to re-check one candidate against its journal).

    Never raises and never touches a task trace — that is the property
    that keeps listing inside its latency budget at 5,000 sessions.
    """
    rows, offset = _read_index_snapshot()
    tail = _read_index_journal_tail(offset)
    wanted_repo: Optional[Set[str]] = None
    if repo is not None and str(repo).strip():
        wanted_repo = _repo_match_forms(repo)
    wanted_root: Optional[str] = None
    if log_root is not None and str(log_root).strip():
        wanted_root = str(Path(str(log_root)).expanduser())
    query_text = str(query or "").strip()
    if (
        limit is not None
        and 0 <= limit <= _INDEX_LAZY_LIMIT
        and wanted_repo is None
        and wanted_root is None
        and not query_text
        and verify is None
    ):
        # Bounded listing (what every surface actually renders): order and
        # cut the RAW arrays first, then project only the rows returned.
        # At 5,000 sessions that is ~15ms instead of ~40ms.
        ordered = sorted(
            (*rows, *tail),
            key=lambda item: (
                item[_UPDATED_AT]
                if type(item) is list and len(item) > _UPDATED_AT
                else 0,
                item[0] if type(item) is list and item else "",
            ),
            reverse=True,
        )
        width = len(_INDEX_FIELDS)
        out: List[Dict[str, Any]] = []
        for raw in ordered[:limit]:
            record = (
                dict(zip(_INDEX_FIELDS, raw, strict=False))
                if type(raw) is list and len(raw) == width
                else _row_to_record(raw)
            )
            if record is not None:
                out.append(record)
        return out
    if tail:
        merged = _merge_index_rows([*rows, *tail])
    else:
        # Steady state: the snapshot is already deduped and newest-first,
        # so project it directly instead of re-merging every row.
        width = len(_INDEX_FIELDS)
        merged = []
        append = merged.append
        for raw in rows:
            record = (
                dict(zip(_INDEX_FIELDS, raw, strict=False))
                if type(raw) is list and len(raw) == width
                else _row_to_record(raw)
            )
            if record is not None:
                append(record)
    if wanted_repo is not None:
        merged = [row for row in merged if _row_repo_matches(row, wanted_repo)]
    if wanted_root is not None:
        merged = [row for row in merged if _row_root_matches(row, wanted_root)]
    if query_text:
        merged = [row for row in merged if _index_query_matches(row, query_text)]
    if verify is not None:
        kept: List[Dict[str, Any]] = []
        for row in merged:
            try:
                if verify(row):
                    kept.append(row)
            except Exception:
                continue
        merged = kept
    if limit is not None and limit >= 0:
        return merged[:limit]
    return merged


def _repo_match_forms(repo: Any) -> Set[str]:
    """Every spelling a caller may use for one repository.

    Resolved ONCE per query, never per row: the naive version called
    ``Path.resolve()`` for every candidate of every indexed session, which
    dominated the listing cost at 5,000 sessions.
    """
    from memory import paths as _paths

    wanted = str(repo).strip()
    forms = {wanted, wanted.casefold()}
    try:
        forms.add(_paths.repo_key(wanted))
    except Exception:
        pass
    try:
        forms.add(_paths.repo_key(Path(wanted)))
    except Exception:
        pass
    return {form for form in forms if form}


def _row_root_matches(record: Mapping[str, Any], wanted: str) -> bool:
    """True when the row's artifact root IS, or lives under, `wanted`."""
    value = str(record.get("log_root") or "")
    if not value:
        return False
    if os.path.normcase(value) == os.path.normcase(wanted):
        return True
    try:
        from memory.paths import is_within

        return bool(is_within(value, wanted))
    except Exception:
        return False


def _row_repo_matches(record: Mapping[str, Any], wanted: Set[str]) -> bool:
    for value in (
        str(record.get("repo_path") or ""),
        str(record.get("repo_name") or ""),
        str(record.get("repo_key") or ""),
    ):
        if value and (value in wanted or value.casefold() in wanted):
            return True
    return False


def _index_query_matches(record: Mapping[str, Any], query: str) -> bool:
    try:
        from cli.interactive import session_matches

        return bool(session_matches(dict(record), query))
    except Exception:
        haystack = " ".join(
            str(record.get(key) or "")
            for key in (
                "session_id",
                "task_id",
                "issue",
                "repo_path",
                "repo_name",
                "status",
                "branch",
            )
        ).casefold()
        return all(token.casefold() in haystack for token in query.split())


def index_compact(max_records: int = _INDEX_ROW_LIMIT) -> Dict[str, Any]:
    """Fold the journal into the compacted snapshot and truncate it.

    Order is snapshot-then-truncate, and the read merge is idempotent by
    id, so an interrupted compaction re-reads the same rows rather than
    losing them. Returns a receipt; never raises.
    """
    rows = index_records()
    kept = rows[: max(0, int(max_records))]
    try:
        directory = _index_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "fields": list(_INDEX_FIELDS),
            "rows": [_index_row(row) for row in kept],
            "compacted_at": time.time(),
            "dropped": max(0, len(rows) - len(kept)),
            "journal_bytes": 0,
        }
        _atomic_write_bytes(
            _index_snapshot_path(),
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8"),
        )
        with _index_write_lock() as acquired:
            if acquired:
                path = _index_journal_path()
                if path.exists():
                    with path.open("w", encoding="utf-8"):
                        pass
        return {
            "ok": True,
            "rows": len(kept),
            "dropped": payload["dropped"],
            "path": str(_index_snapshot_path()),
        }
    except (OSError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rows": len(kept)}


def index_stats() -> Dict[str, Any]:
    """Counts for the global index (repos, roots, rows). Never raises."""
    rows = index_records()
    repos = {str(row.get("repo_key") or "") for row in rows if row.get("repo_key")}
    roots = {str(row.get("log_root") or "") for row in rows if row.get("log_root")}
    return {
        "sessions": len(rows),
        "repos": len(repos),
        "log_roots": len(roots),
        "path": str(_index_dir()),
        "journal": str(_index_journal_path()),
        "snapshot": str(_index_snapshot_path()),
    }


def index_newest_resumable(
    *,
    repo: Any = None,
    verify: Optional[Any] = None,
    scan: int = 200,
    log_root: Any = None,
) -> Optional[Dict[str, Any]]:
    """Newest index row whose status says it is resumable.

    The index is only a hint: when `verify` is supplied it is applied to
    each candidate until one passes, so a stale "resumable" row can never
    send `--continue` into a finished run. Falls back to the recorded flag
    when no verifier is available. `log_root` restricts the search to one
    artifact root (the isolation contract for an explicitly chosen root);
    without it the search spans every root this machine has indexed.
    """
    candidates = index_records(repo=repo, limit=max(1, int(scan)), log_root=log_root)
    preferred = [row for row in candidates if row.get("resumable")]
    for row in preferred or candidates:
        if verify is None:
            return row
        try:
            if verify(row):
                return row
        except Exception:
            continue
    return None


def index_roots() -> List[str]:
    """Distinct artifact roots the index knows about."""
    roots = {str(row.get("log_root") or "") for row in index_records()}
    return sorted(root for root in roots if root)


def cross_repo_sessions(
    query: str = "",
    repo: Any = None,
    limit: int = 50,
    *,
    verify: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Sessions across every indexed repository, newest first.

    This is the read model behind `/sessions` (and `neo --list-sessions`)
    when the caller wants results from any CWD: the global index carries
    repo path, repo key, repo name, branch, worktree, task and status for
    every conversation and run, so no task trace is re-read to list them.
    `repo` is the EXPLICIT cross-repo filter (path, key, or directory name);
    `query` reuses the shared session filter grammar.
    """
    return index_records(repo=repo, query=query, limit=limit, verify=verify)


# ---------------------------------------------------------------------------
# Short-id resolution + startup recovery (VEX-CEILING-03 requirement 3)
# ---------------------------------------------------------------------------

_MIN_SHORT_ID = 4


def resolve_session_token(
    log_root: Any, token: Any, repo: Any = None
) -> Dict[str, Any]:
    """Resolve an exact-or-unique-prefix session id.

    Returns ``{"status": "ok"|"missing"|"ambiguous"|"invalid",
    "session_id": ..., "candidates": [...]}``. An exact id always wins; a
    prefix shorter than ``_MIN_SHORT_ID`` characters is refused so a
    three-letter guess can never sweep a whole directory. Never raises.
    """
    text = str(token or "").strip()
    if not text:
        return {"status": "invalid", "session_id": "", "candidates": []}
    try:
        # include_corrupt=True on purpose: a CORRUPT snapshot is exactly the
        # session `/recover` must be able to name, so a corrupt id (or a
        # prefix of one) still resolves instead of reporting "missing".
        known = [
            record["session_id"]
            for record in _conversation_records(log_root, repo, include_corrupt=True)
        ]
    except Exception:
        known = []
    if text in known:
        return {"status": "ok", "session_id": text, "candidates": [text]}
    if len(text) < _MIN_SHORT_ID:
        return {
            "status": "invalid",
            "session_id": "",
            "candidates": [],
            "error": f"session id prefix must be at least {_MIN_SHORT_ID} characters",
        }
    matches = sorted({sid for sid in known if sid.startswith(text)})
    if len(matches) == 1:
        return {"status": "ok", "session_id": matches[0], "candidates": matches}
    if not matches:
        return {"status": "missing", "session_id": "", "candidates": []}
    return {"status": "ambiguous", "session_id": "", "candidates": matches}


def resolve_index_session(token: Any) -> Dict[str, Any]:
    """Resolve a short session id across every indexed root."""
    text = str(token or "").strip()
    result: Dict[str, Any] = {
        "status": "missing",
        "session_id": "",
        "log_root": "",
        "candidates": [],
    }
    if not text:
        result["status"] = "invalid"
        return result
    rows = index_records()
    exact = [row for row in rows if str(row.get("session_id") or "") == text]
    matches = exact
    if not matches:
        if len(text) < _MIN_SHORT_ID:
            result["status"] = "invalid"
            result["error"] = (
                f"session id prefix must be at least {_MIN_SHORT_ID} characters"
            )
            return result
        matches = [
            row for row in rows if str(row.get("session_id") or "").startswith(text)
        ]
    if not matches:
        return result
    if len(matches) > 1:
        result["status"] = "ambiguous"
        result["candidates"] = sorted(
            {str(row.get("session_id") or "") for row in matches}
        )
        return result
    row = matches[0]
    result["status"] = "ok"
    result["session_id"] = str(row.get("session_id") or "")
    result["log_root"] = str(row.get("log_root") or "")
    result["record"] = row
    return result


def startup_recovery_candidate(
    log_root: Any, repo: Any = None
) -> Optional[Dict[str, Any]]:
    """Describe the newest UNREADABLE conversation, or None when healthy.

    Startup calls this before loading a conversation: a corrupt snapshot
    must produce an explicit offer to recover, not a silently dropped
    session (the previous behaviour caught the load error and continued
    with no conversation at all).
    """
    try:
        records = _conversation_records(log_root, repo, include_corrupt=True)
    except Exception:
        return None
    for record in records:
        if str(record.get("status")) != "corrupt":
            continue
        session_id = str(record.get("session_id") or "")
        if not session_id:
            continue
        report = inspect_session(log_root, session_id, repo)
        if str(report.get("status")) == "ok":
            continue
        return {
            "session_id": session_id,
            "path": str(report.get("path") or record.get("path") or ""),
            "event_path": str(report.get("event_path") or ""),
            "backup_available": bool(report.get("backup_available")),
            "error": str(report.get("error") or "conversation snapshot is unreadable"),
            "repo": str(record.get("repo") or ""),
        }
    return None


def recover_corrupt_session(
    log_root: Any, session_id: str, repo: Any = None, *, strategy: str = "fresh"
) -> Dict[str, Any]:
    """Recover a corrupt conversation and re-index the result.

    ``strategy="fresh"`` QUARANTINES the corrupt snapshot and its event
    journal next to the original (``<name>.corrupt-<ms>``) and writes a new
    empty conversation; the corrupt bytes are never deleted. ``"backup"``
    restores the last-known-good backup instead. The caller re-indexes so
    the global listing reflects the recovered state.
    """
    report = recover_session(log_root, session_id, repo, strategy=strategy)
    try:
        session = load_or_create(log_root, repo, session_id, strict=True)
        index_conversation(
            session,
            log_root,
            status="recovered",
            resumable=False,
            source="recovery",
        )
    except Exception:
        pass
    return report


def index_remove(session_id: str) -> bool:
    """Drop rows for one session (or one run-only row) from the index.

    The index journal is append-only by design, so removal is expressed as
    a compaction that filters the id out; returns True when the row is
    absent afterwards. A run-only row (no conversation id) is keyed by its
    task id, so both spellings are filtered. Never raises.
    """
    target = str(session_id or "").strip()
    if not target:
        return False
    rows = [
        row
        for row in index_records()
        if str(row.get("session_id") or "") != target
        and str(row.get("task_id") or "") != target
    ]
    try:
        directory = _index_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "fields": list(_INDEX_FIELDS),
            "rows": [_index_row(row) for row in rows],
            "compacted_at": time.time(),
            "dropped": 0,
            "journal_bytes": 0,
        }
        _atomic_write_bytes(
            _index_snapshot_path(),
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8"),
        )
        with _index_write_lock() as acquired:
            if acquired:
                path = _index_journal_path()
                if path.exists():
                    with path.open("w", encoding="utf-8"):
                        pass
        return True
    except (OSError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# VEX-PF-08 platform round: long-session coherence, cost/context visibility, and
# an honest survival report after a kill.
#
# Everything below is APPENDED and additive. No existing signature, snapshot
# key, event kind, or ``_SCHEMA_VERSION`` value changed, and nothing here
# writes a session: the pulse is a projection, the survival report is a
# reader, and the segmenter is pure. That is deliberate -- the three
# behaviours this round is about (keep a 60-turn session coherent, keep cost
# and context visible, report exactly what survived a kill) are all read-only
# questions, and a reader that could also write would eventually be the reason
# a reader's answer is wrong.
# ---------------------------------------------------------------------------

#: Characters per token for the context estimate. Read from
#: ``harness.config.DEFAULTS``-adjacent convention (``context_chars_per_token``
#: is 4 there); this is the FALLBACK for when no config was supplied, and a
#: pulse that used a different number from the rest of the product would make
#: two surfaces disagree about how full the context is.
_DEFAULT_CHARS_PER_TOKEN = 4

#: A session at or above this fraction of its context window is "pressured";
#: at or above 0.9 it is "exhausted". The two bands are the difference between
#: "a compaction will probably fire soon" and "the next turn will be refused",
#: and they are reported separately because they need different answers.
_PRESSURE_WARN_FRACTION = 0.75
_PRESSURE_EXHAUSTED_FRACTION = 0.9

#: The pulse's sections, with the anti-clutter rule applied. ``attention`` and
#: ``gaps`` are genuine panels and obey the rule: one or two entries render as
#: a single inline clause, three or more render as a titled block. ``session``
#: and ``spend`` are the receipt's own subject -- the reason a person opened a
#: long session -- and are exempt for the same reason a per-file roster is: a
#: receipt that rendered nothing because its subject was small would be a
#: broken surface, not a tidy one. The exemption is declared here rather than
#: inferred so a test can pin it.
PULSE_SECTIONS = ("session", "spend", "attention", "gaps")
PULSE_ANTI_CLUTTER_EXEMPT = ("session", "spend")

#: The minimum entries before a section earns a heading. Read from
#: ``cli.toggles`` (which reads ``cli.design``) at call time, with a literal
#: fallback so a broken or mid-edit import can never take this module offline.
#: The whole ``cli`` package has been taken down by an import-time table
#: failure twice in this tree's history, and a session reader is the last
#: thing that should be collateral damage.
_LITERAL_MIN_SECTION_ENTRIES = 3


def _min_section_entries() -> int:
    try:
        from cli.toggles import MIN_SECTION_ENTRIES

        return max(2, int(MIN_SECTION_ENTRIES))
    except Exception:
        return _LITERAL_MIN_SECTION_ENTRIES


def _bounded_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _session_text_chars(session: Mapping[str, Any]) -> int:
    """Characters a resume would have to re-read: summary plus active turns."""
    total = len(str(session.get("summary") or ""))
    for key in ("turns", "compacted_turns"):
        collection = session.get(key)
        if not isinstance(collection, list):
            continue
        for turn in collection:
            if isinstance(turn, dict):
                total += len(str(turn.get("text") or ""))
    return total


def _read_turn_ledger(task_dir: Any) -> List[Dict[str, Any]]:
    """Read ``<task_dir>/turns.jsonl`` defensively; return turn records.

    A torn final line is skipped rather than raised: a process killed
    mid-write leaves a half line, and a reader that refuses to read past it
    cannot answer the question it exists to answer.
    """
    rows: List[Dict[str, Any]] = []
    try:
        path = Path(task_dir) / "turns.jsonl"
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return rows
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_trace_kinds(task_dir: Any) -> Tuple[List[str], int]:
    """Return ``(kinds, last_sequence)`` from ``<task_dir>/trace.jsonl``."""
    kinds: List[str] = []
    sequence = 0
    try:
        path = Path(task_dir) / "trace.jsonl"
        handle = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return kinds, sequence
    for line in handle.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = str(row.get("event") or row.get("kind") or "")
        if kind:
            kinds.append(kind)
        payload = row.get("payload")
        candidate = row.get("sequence")
        if candidate is None and isinstance(payload, dict):
            candidate = payload.get("sequence")
        if candidate is not None:
            value = _bounded_int(candidate, 0)
            sequence = max(sequence, value)
    return kinds, sequence


def _read_checkpoint(task_dir: Any) -> Dict[str, Any]:
    """Return the run's checkpoint record, or ``{}`` when there is none."""
    for candidate in (Path(task_dir) / "checkpoint.json",):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _ledger_turn_numbers(ledger: List[Mapping[str, Any]]) -> List[int]:
    numbers: List[int] = []
    for row in ledger:
        value = row.get("turn")
        if value is None:
            value = row.get("turn_number")
        number = _bounded_int(value, 0)
        if number > 0:
            numbers.append(number)
    return numbers


def _missing_turns(numbers: List[int]) -> List[int]:
    """Return the turn numbers absent from a 1..max sequence."""
    if not numbers:
        return []
    expected = set(range(1, max(numbers) + 1))
    return sorted(expected - set(numbers))


def _file_survival(
    repo: Any, paths: List[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Report which named files are still on disk, and which are gone."""
    survived: List[Dict[str, Any]] = []
    lost: List[str] = []
    if not repo:
        return survived, lost
    base = Path(str(repo))
    for raw in paths[:200]:
        name = str(raw or "").strip()
        if not name:
            continue
        try:
            candidate = Path(name)
            if not candidate.is_absolute():
                candidate = base / candidate
            present = candidate.is_file()
            survived.append(
                {
                    "path": name,
                    "present": present,
                    "bytes": candidate.stat().st_size if present else 0,
                }
            )
            if not present:
                lost.append(name)
        except (OSError, ValueError, TypeError):
            survived.append({"path": name, "present": False, "bytes": 0})
            lost.append(name)
    return survived, lost


def session_pulse(
    session: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
    log_root: Any = None,
    task_id: Any = None,
    chars_per_token: int = 0,
) -> Dict[str, Any]:
    """Return the long-session receipt: is this still coherent, and at what cost.

    Answers the three questions a person has after an hour in one session --
    *how much of my conversation is still in context, how much have I spent,
    and is anything wrong* -- from the session's own records. It is a
    projection: it writes nothing, and a session it cannot read returns a
    receipt that says so instead of a zero.

    Honesty rules this receipt is built to keep:

    * **An unpriced run reports ``cost.usd is None`` and ``priced: False``**
      with a note naming why. ``0.0`` reads as a measured fact that the work
      was free, which is the one thing a cost receipt must never imply.
    * **A context window nobody configured reports ``window: None``** and a
      gap, not a made-up default. The estimate is labelled ``heuristic`` and
      carries the characters-per-token divisor it used, so two surfaces using
      this and the kernel's own meter can be compared instead of argued about.
    * **Every gap is named.** A fact this receipt could not measure is in
      ``gaps`` with a reason, never silently omitted and never rendered as a
      zero.
    """
    gaps: List[str] = []
    attention: List[str] = []
    notes: List[str] = []

    if not isinstance(session, Mapping):
        return {
            "schema_version": 1,
            "session_id": "",
            "readable": False,
            "conversation": {},
            "context": {"window": None, "used": None, "fraction": None},
            "cost": {"usd": None, "priced": False, "calls": None, "tokens": None},
            "pressure": "unknown",
            "attention": [],
            "gaps": ["the session record could not be read as a mapping"],
            "lines": [],
        }

    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    raw_turns = (
        session.get("raw_turns") if isinstance(session.get("raw_turns"), list) else []
    )
    compacted = (
        session.get("compacted_turns")
        if isinstance(session.get("compacted_turns"), list)
        else []
    )
    summaries = (
        session.get("summaries") if isinstance(session.get("summaries"), list) else []
    )
    history = session.get("history") if isinstance(session.get("history"), list) else []
    summary_text = str(session.get("summary") or "")

    total_turns = len(raw_turns) or (len(turns) + len(compacted))
    compactions = len(summaries)
    if not raw_turns:
        gaps.append(
            "raw_turns is empty, so the total turn count is an estimate from active + compacted"
        )

    conversation = {
        "session_id": str(session.get("session_id") or ""),
        "turns_total": total_turns,
        "turns_active": len(turns),
        "turns_compacted": len(compacted),
        "compactions": compactions,
        "summary_chars": len(summary_text),
        "history_lines": len(history),
        "updated_ts": _bounded_float(session.get("updated_ts"), 0.0),
        "active_run_id": str(session.get("active_run_id") or ""),
        "unresolved_questions": len(
            session.get("unresolved_questions")
            if isinstance(session.get("unresolved_questions"), list)
            else []
        ),
    }

    # -- context window, by KEY PRESENCE, never by truthiness ---------------
    values = dict(config or {})
    window = None
    if "context_window_tokens" in values:
        window = _bounded_int(values.get("context_window_tokens"), 0) or None
        if window is None:
            gaps.append("context_window_tokens is present but not a usable integer")
    else:
        gaps.append(
            "no context_window_tokens in the config, so context pressure is not measurable"
        )
    divisor = _bounded_int(
        chars_per_token or values.get("context_chars_per_token"),
        _DEFAULT_CHARS_PER_TOKEN,
    )
    if divisor <= 0:
        divisor = _DEFAULT_CHARS_PER_TOKEN
    chars = _session_text_chars(session)
    used_tokens = max(0, chars // divisor)
    fraction = None
    if window:
        fraction = round(used_tokens / float(window), 4)
    context = {
        "window": window,
        "used": used_tokens,
        "chars": chars,
        "chars_per_token": divisor,
        "fraction": fraction,
        "estimator": "heuristic",
        "source": "config:context_window_tokens" if window else "absent",
    }

    # -- cost, delegated to the one authority that already reads the ledger --
    cost: Dict[str, Any] = {
        "usd": None,
        "priced": False,
        "calls": None,
        "tokens": None,
        "source": "none",
    }
    root = log_root if log_root is not None else session.get("_log_root")
    if not root:
        gaps.append("no log root, so cost and call counts were not read")
    else:
        try:
            from cli import runview as _runview

            facts = _runview.cost_reconciliation(
                root, str(task_id or conversation["active_run_id"])
            )
        except Exception as exc:
            facts = None
            gaps.append(f"cost could not be read ({type(exc).__name__})")
        if isinstance(facts, Mapping):
            usd = _bounded_float(facts.get("cost_usd"))
            calls = _bounded_int(facts.get("model_calls"), 0) or None
            tokens = _bounded_int(facts.get("total_tokens"), 0) or None
            cost = {
                "usd": usd,
                "priced": usd is not None,
                "calls": calls,
                "tokens": tokens,
                "source": str(facts.get("source") or facts.get("status") or "ledger"),
                "reconciled": bool(facts.get("reconciled")),
                "ledger_available": bool(facts.get("ledger_available", True)),
            }
            if usd is None:
                gaps.append(
                    "no priced model call is recorded for this run, so the cost is unknown "
                    "rather than zero"
                )
            if calls is None:
                gaps.append("the model call count is unknown, not zero")
        elif not gaps:
            gaps.append("cost_reconciliation returned nothing for this run")

    # -- pressure -----------------------------------------------------------
    pressure = "fresh"
    if fraction is not None:
        if fraction >= _PRESSURE_EXHAUSTED_FRACTION:
            pressure = "exhausted"
        elif fraction >= _PRESSURE_WARN_FRACTION:
            pressure = "pressuring"
        elif compactions:
            pressure = "compacted"
        elif total_turns:
            pressure = "active"
    else:
        pressure = "unmeasured" if total_turns else "fresh"

    if pressure == "exhausted":
        attention.append(
            f"context is at {fraction:.0%} of the {window}-token window; the next turn may be refused"
        )
    elif pressure == "pressuring":
        attention.append(
            f"context is at {fraction:.0%} of the {window}-token window; a compaction is due soon"
        )
    if compactions >= 3:
        notes.append(
            f"compacted {compactions} times: {conversation['turns_compacted']} turns are summarised "
            f"and {conversation['turns_active']} are still active"
        )
    unresolved = conversation["unresolved_questions"]
    if unresolved:
        attention.append(
            f"{unresolved} question(s) from earlier turns are still unanswered"
        )
    if conversation["active_run_id"]:
        notes.append(
            f"a run is linked to this session: {conversation['active_run_id']}"
        )

    receipt = {
        "schema_version": 1,
        "readable": True,
        "session_id": conversation["session_id"],
        "conversation": conversation,
        "context": context,
        "cost": cost,
        "pressure": pressure,
        "attention": attention,
        "notes": notes,
        "gaps": gaps,
    }
    receipt["lines"] = session_pulse_lines(receipt)
    return receipt


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "unknown (no priced call recorded)"
    return f"${value:.4f}"


def _fmt_count(value: Optional[int], unknown: str) -> str:
    if value is None:
        return unknown
    return str(int(value))


def session_pulse_lines(pulse: Mapping[str, Any], *, width: int = 0) -> List[str]:
    """Render a :func:`session_pulse` receipt as PLAIN, markup-free lines.

    Two shapes, both earned:

    * The **headline plus its two subject rows** (conversation, spend) are
      always rendered. They are the receipt's subject -- the reason a person
      opened a long session -- and are declared exempt in
      :data:`PULSE_ANTI_CLUTTER_EXEMPT` for the same reason a per-file roster
      is: a receipt that rendered nothing because its subject was small would
      be a broken surface, not a tidy one.
    * The **attention and gaps sections** obey the anti-clutter rule. One or
      two entries render as a single inline clause, because a titled block
      with two rows in it costs a heading and a margin to say less than a
      sentence. Three or more earn a heading. Zero renders nothing at all --
      an empty section is not rendered.

    The omission of a section is never silent in the other direction: gaps are
    facts about what could NOT be measured, so ``gaps`` uses the rule for
    structure but every entry is still shown; a section is dropped only when
    it has fewer than three, and a dropped section of gaps is folded into the
    headline rather than discarded.
    """
    if not isinstance(pulse, Mapping):
        return ["session pulse: no receipt"]
    threshold = _min_section_entries()
    conversation = (
        pulse.get("conversation")
        if isinstance(pulse.get("conversation"), Mapping)
        else {}
    )
    context = pulse.get("context") if isinstance(pulse.get("context"), Mapping) else {}
    cost = pulse.get("cost") if isinstance(pulse.get("cost"), Mapping) else {}
    attention = [
        str(item) for item in (pulse.get("attention") or []) if str(item).strip()
    ]
    gaps = [str(item) for item in (pulse.get("gaps") or []) if str(item).strip()]

    pressure = str(pulse.get("pressure") or "unknown")
    total = _bounded_int(conversation.get("turns_total"), 0)
    lines = [
        f"session {conversation.get('session_id') or '(new)'} - {total} turn(s), {pressure}"
    ]
    lines.append(
        "  conversation: "
        f"{_fmt_count(conversation.get('turns_active'), '?')} active, "
        f"{_fmt_count(conversation.get('turns_compacted'), '?')} compacted, "
        f"{_fmt_count(conversation.get('compactions'), '?')} compaction(s)"
    )
    window = context.get("window")
    used = context.get("used")
    context_text = (
        f"context {used}/{window} tokens ({context.get('fraction')})"
        if window
        else f"context about {used} tokens (window not configured)"
    )
    lines.append(f"  spend: {_fmt_money(cost.get('usd'))} - {context_text}")

    for title, entries in (("attention", attention), ("gaps", gaps)):
        if not entries:
            continue
        if len(entries) < threshold:
            joined = "; ".join(entries)
            marker = "!" if title == "attention" else "~"
            lines.append(f"  {marker} {joined}")
            continue
        lines.append(f"  {title} ({len(entries)}):")
        lines.extend(f"    - {entry}" for entry in entries)
    return lines


def transcript_segments(
    session: Any,
    *,
    max_segments: int = 12,
    normalize_chars: int = 48,
) -> List[Dict[str, Any]]:
    """Group a long transcript into readable runs so it stops being a wall.

    A 60-turn session rendered as 60 equal rows is not 60 facts, it is one
    fact and 59 lines of texture. This folds *consecutive* turns that share a
    role and a normalized text prefix into one counted segment. Only REPEATED
    runs are collapsed: a run of one is left alone, because folding a single
    turn into "1x ..." makes the transcript longer and less readable.

    The cap is reported, never silent: the result carries ``omitted`` with the
    number of segments dropped, so a truncated transcript cannot read as a
    complete one. Pure -- it reads the session and returns a new list.
    """
    if not isinstance(session, Mapping):
        return []
    source = session.get("raw_turns")
    if not isinstance(source, list) or not source:
        source = session.get("turns") if isinstance(session.get("turns"), list) else []
    limit = max(1, _bounded_int(max_segments, 12))
    keep = max(4, _bounded_int(normalize_chars, 48))

    def signature(turn: Mapping[str, Any]) -> str:
        role = str(turn.get("role") or "user")
        text = " ".join(str(turn.get("text") or "").split()).casefold()
        return f"{role}|{text[:keep]}"

    segments: List[Dict[str, Any]] = []
    for turn in source:
        if not isinstance(turn, Mapping):
            continue
        key = signature(turn)
        if segments and segments[-1]["signature"] == key:
            current = segments[-1]
            current["count"] += 1
            current["last_turn_id"] = str(turn.get("turn_id") or "")
            continue
        text = " ".join(str(turn.get("text") or "").split())
        segments.append(
            {
                "signature": key,
                "role": str(turn.get("role") or "user"),
                "count": 1,
                "first_turn_id": str(turn.get("turn_id") or ""),
                "last_turn_id": str(turn.get("turn_id") or ""),
                "sample": text[:160],
                "task_ids": [str(turn.get("task_id"))] if turn.get("task_id") else [],
                "repeated": False,
            }
        )
    for segment in segments:
        segment["repeated"] = segment["count"] > 1
        segment.pop("signature", None)
    if len(segments) > limit:
        return [*segments[-limit:], {"omitted": len(segments) - limit}]
    return segments


def transcript_segment_lines(segments: Any, *, width: int = 0) -> List[str]:
    """Render :func:`transcript_segments` as PLAIN, markup-free lines.

    A repeated run renders as one line with a count; a single turn renders as
    itself. The trailing ``omitted`` marker is the omission notice -- a
    truncated transcript must never read as a complete one.
    """
    if not isinstance(segments, (list, tuple)):
        return []
    lines: List[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        if "omitted" in segment:
            lines.append(
                f"  (+{_bounded_int(segment.get('omitted'), 0)} earlier segments not shown)"
            )
            continue
        count = _bounded_int(segment.get("count"), 1)
        role = str(segment.get("role") or "user")
        sample = str(segment.get("sample") or "")
        if count > 1:
            lines.append(f"{role} x{count}: {sample}")
        else:
            lines.append(f"{role}: {sample}")
    return lines


SURVIVAL_VERDICTS = (
    "nothing_found",
    "not_resumable",
    "resumed_with_gaps",
    "clean_resume",
)

#: A run that wrote a terminal event is NOT resumable even when a turn ledger
#: survived, and the report says which of the two facts it found. A killed
#: process that happened to flush its last line must not be reported as an
#: interrupted run that can simply continue.
_TERMINAL_TRACE_KINDS = ("task_end", "run_finished", "result")


def session_survival_report(
    log_root: Any,
    task_id: Any,
    *,
    repo: Any = None,
    session_id: Any = None,
) -> Dict[str, Any]:
    """Report EXACTLY what survived an interrupted run, and what did not.

    This is the "kill the process mid-run and restart" receipt. It reads four
    independent records -- the kernel's turn ledger, the run's own trace, the
    checkpoint, and the files on disk -- and CROSS-CHECKS them, because each
    one can be ahead of the others after a hard kill:

    * a turn is **durable** only when the ledger has it;
    * a file is **survived** only when it is on disk now;
    * a run **looked finished** when a terminal event is in the trace, and that
      is reported separately from "resumable" rather than being smoothed into
      one word;
    * any disagreement between the records becomes an entry in
      ``unexplained_gaps``, and a non-empty gap list downgrades the verdict --
      a report that could not explain a gap must not call the resume clean.

    It reads only. It never repairs, quarantines, or resumes, so it is safe to
    call on a live run directory.
    """
    gaps: List[str] = []
    target = str(task_id or "").strip()
    if not target:
        return {
            "schema_version": 1,
            "task_id": "",
            "verdict": "nothing_found",
            "resumable": False,
            "gaps": ["no task id was supplied, so nothing could be checked"],
            "lines": ["survival: no task id was supplied, so nothing could be checked"],
        }

    task_dir = Path(str(log_root)) / target if log_root else Path(target)
    ledger = _read_turn_ledger(task_dir)
    kinds, last_sequence = _read_trace_kinds(task_dir)
    checkpoint = _read_checkpoint(task_dir)
    numbers = _ledger_turn_numbers(ledger)
    if not numbers and not kinds and not checkpoint:
        if not task_dir.exists():
            return {
                "schema_version": 1,
                "task_id": target,
                "verdict": "nothing_found",
                "resumable": False,
                "turns_durable": 0,
                "turn_numbers": [],
                "gaps": [f"no run directory exists at {task_dir}"],
                "lines": [f"survival: nothing found for {target} at {task_dir}"],
            }
        gaps.append(
            "the run directory exists but carries no turn ledger, no trace and no checkpoint, "
            "so there is no evidence any of its work was written"
        )

    missing_turns = _missing_turns(numbers)
    if missing_turns:
        gaps.append(
            "the turn ledger skips turn(s) "
            + ", ".join(str(item) for item in missing_turns[:8])
            + ": a hard kill can land between two fsynced records"
        )

    terminal_kinds = [kind for kind in kinds if kind in _TERMINAL_TRACE_KINDS]
    looked_finished = bool(terminal_kinds)
    if looked_finished and not checkpoint:
        gaps.append(
            "the trace contains a terminal event ("
            + ", ".join(terminal_kinds[:3])
            + ") but no checkpoint was written, so this run cannot be resumed"
        )
    if not ledger and looked_finished:
        gaps.append(
            "the trace says the run finished but the turn ledger is empty; one of them lied"
        )

    changed: List[str] = []
    for row in ledger:
        for name in row.get("changed_files") or []:
            text = str(name or "").strip()
            if text and text not in changed:
                changed.append(text)
    files, files_lost = _file_survival(repo, changed)
    if files_lost:
        gaps.append(
            f"{len(files_lost)} file(s) the run recorded as changed are no longer on disk: "
            + ", ".join(files_lost[:6])
        )

    resume_token = str(checkpoint.get("resume_token") or "")
    resumable = bool(resume_token) and not looked_finished
    if resume_token and looked_finished:
        gaps.append(
            "a resume token exists but the trace already recorded a terminal event"
        )
    if not resume_token and not looked_finished:
        gaps.append(
            "no checkpoint resume token was written, so the run cannot be resumed as-is"
        )

    if not ledger and not kinds and not checkpoint:
        verdict = "nothing_found"
    elif not resumable:
        verdict = "not_resumable"
    elif gaps:
        verdict = "resumed_with_gaps"
    else:
        verdict = "clean_resume"

    return {
        "schema_version": 1,
        "task_id": target,
        "session_id": str(session_id or ""),
        "run_dir": str(task_dir),
        "verdict": verdict,
        "resumable": resumable,
        "resume_token_present": bool(resume_token),
        "looked_finished": looked_finished,
        "terminal_events": terminal_kinds[:5],
        "turns_durable": len(numbers),
        "turn_numbers": numbers,
        "turns_missing": missing_turns,
        "last_event_sequence": last_sequence,
        "changed_files": files,
        "files_lost": files_lost,
        "gaps": gaps,
        "lines": [],
    }


def session_survival_lines(report: Mapping[str, Any]) -> List[str]:
    """Render a :func:`session_survival_report` as PLAIN, markup-free lines.

    This is a receipt about a possibly-catastrophic event, so it is EXEMPT
    from the anti-clutter rule for the same reason a refusal is: a user whose
    process was killed needs the complete list, and a survivor report that
    dropped two of its three gaps to satisfy a tidiness rule would be the
    worst receipt this module could emit. Every gap renders.
    """
    if not isinstance(report, Mapping):
        return ["survival: no receipt"]
    lines = [
        f"survival for {report.get('task_id') or '(unknown task)'}: {report.get('verdict')}"
    ]
    lines.append(
        "  survived: "
        f"{_fmt_count(report.get('turns_durable'), '0')} durable turn(s) "
        f"{report.get('turn_numbers') or '[]'}, "
        f"last event sequence {_fmt_count(report.get('last_event_sequence'), 'unknown')}"
    )
    files = (
        report.get("changed_files")
        if isinstance(report.get("changed_files"), list)
        else []
    )
    if files:
        present = [
            item for item in files if isinstance(item, Mapping) and item.get("present")
        ]
        lines.append(
            f"  files: {len(present)}/{len(files)} the run recorded as changed are still on disk"
        )
    lines.append(
        "  looks finished: "
        + ("yes" if report.get("looked_finished") else "no")
        + " | resumable: "
        + ("yes" if report.get("resumable") else "no")
    )
    gaps = [str(item) for item in (report.get("gaps") or []) if str(item).strip()]
    if gaps:
        lines.append(f"  gaps ({len(gaps)}):")
        lines.extend(f"    - {gap}" for gap in gaps)
    return lines


# ---------------------------------------------------------------------------
# The multi-instance mount, for the two shells (Prompt 08, DO 1).
#
# ``load_or_create`` is deliberately NOT gated: it is also the reader behind
# ``/sessions``, ``resume_session``, ``load_latest_session`` and
# ``save_session``'s revision check, and a listing must keep working while
# another instance legitimately holds the repository. Gating the WRITER is
# the correct scope, and the writer is ``open_session``.
# ---------------------------------------------------------------------------

#: ``session_instance_guard`` is read by KEY PRESENCE and takes one of two
#: values. ``"refuse"`` is the default and the reason this module exists;
#: ``"warn"`` records the conflict and continues, which is only ever correct
#: for an operator who has decided their two sessions will not collide. There
#: is no value in ``harness/config.py::DEFAULTS`` and there must not be: a
#: default there is merged into every task and every eval arm, and this is a
#: per-machine decision about whether two humans share a working tree.
SESSION_GUARD_MODES = ("refuse", "warn")


def _guard_mode(config: Optional[Mapping[str, Any]] = None) -> str:
    """Return the guard mode from a config mapping, by key presence."""
    values = dict(config or {})
    if "session_instance_guard" not in values:
        return "refuse"
    raw = str(values.get("session_instance_guard") or "").strip().casefold()
    return raw if raw in SESSION_GUARD_MODES else "refuse"


def session_instance_guard(
    repo: Any,
    *,
    session_id: Any = "",
    config: Optional[Mapping[str, Any]] = None,
    home: Any = None,
) -> Dict[str, Any]:
    """Read-only: is another live ``neo`` writing this repository right now?

    A surface calls this to RENDER the conflict (a banner, a sidebar section,
    a ``--json`` field) without taking anything. The returned dict is
    :func:`shared.instance_guard.instance_guard_report` plus two fields this
    module owns: ``mode`` (the resolved ``refuse``/``warn`` policy) and
    ``enforced`` (whether a writer would actually stop).
    """
    try:
        from shared import instance_guard
    except Exception as exc:  # pragma: no cover - the guard is bottom-layer
        return {
            "schema_version": 1,
            "state": "unavailable",
            "free": False,
            "refuse": False,
            "takeable": False,
            "mode": _guard_mode(config),
            "enforced": False,
            "error": f"the instance guard could not be imported ({type(exc).__name__})",
            "lines": ["the multi-instance guard is unavailable; this run is UNGUARDED"],
        }
    report = instance_guard.instance_guard_report(repo, home=home)
    mode = _guard_mode(config)
    report["mode"] = mode
    report["enforced"] = bool(report.get("refuse")) and mode == "refuse"
    report["session_id"] = str(session_id or "")
    if report.get("free"):
        report["lines"] = ["free: no other instance holds this repository"]
    elif mode == "warn":
        report["lines"] = [
            *report.get("lines", []),
            "  the guard is set to 'warn', so this session will run alongside the other one",
        ]
    return report


@contextmanager
def open_session(
    log_root: Any,
    repo: Any,
    session_id: Optional[str] = None,
    *,
    strict: bool = True,
    config: Optional[Mapping[str, Any]] = None,
    command: str = "",
    home: Any = None,
):
    """Hold the single-writer guard, then load or create the conversation.

    This is the ONE call a shell needs instead of ``load_or_create``. Inside
    the block the repository is exclusively yours; outside it, nothing is held.

    A live peer is a :class:`shared.instance_guard.ConcurrentInstanceError`
    when ``session_instance_guard`` is ``"refuse"`` (the default) and a
    recorded conflict when it is ``"warn"``. A dead peer's lock is taken over
    without asking, because a crashed ``neo`` must not need a human to clear
    it. An UNREADABLE lock refuses in both modes: a lock this process cannot
    parse is not evidence that nobody is there.

    Yields the conversation dict with ``instance_guard`` attached, so a
    surface can render the fact it was protected by without re-probing.
    """
    from shared import instance_guard

    mode = _guard_mode(config)
    sid = str(session_id or "").strip()
    lease = None
    conflict: Optional[Dict[str, Any]] = None
    conflict_lines: List[str] = []
    try:
        lease = instance_guard.acquire_repository_lock(
            repo,
            owner="cli.session.open_session",
            command=command,
            session_id=sid,
            home=home,
        )
    except instance_guard.ConcurrentInstanceError as exc:
        conflict = exc.as_dict()
        conflict_lines = exc.lines()
        if mode == "refuse":
            raise
        conflict_lines.append(
            "  the guard is set to 'warn', so this session runs alongside the other one"
        )
    try:
        session = load_or_create(log_root, repo, session_id, strict=strict)
        session["instance_guard"] = {
            "held": lease is not None,
            "mode": mode,
            "reentrant": bool(lease.reentrant) if lease is not None else False,
            "lock_path": str(lease.path) if lease is not None else "",
            "conflict": conflict,
            "lines": list(lease.info.describe())
            if lease is not None
            else conflict_lines,
        }
        yield session
    finally:
        if lease is not None:
            lease.release()
