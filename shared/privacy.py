"""Privacy-preserving views over shared traces and telemetry.

The authoritative trace is never rewritten.  A caller can create a derived
view in one of three explicit modes:

``local_only``
    Keep useful local content while removing credential material.
``redacted``
    Keep lifecycle content but remove common payload fields that commonly
    contain prompts, command bodies, or tool output.
``shareable``
    Keep bounded metadata and hashes only, with paths and identities removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

from .security import (
    REDACTED_SECRET,
    SecurityViolation,
    is_sensitive_key,
    redact_secrets,
    redact_text,
    require_contained,
)

__all__ = [
    "PRIVACY_MODES",
    "PrivacyReport",
    "apply_privacy_mode",
    "export_privacy_trace",
    "export_trace",
    "privacy_mode",
    "privacy_report",
    "privacy_view",
    "redact_trace",
    "shareable_trace",
]

PathLike = Union[str, os.PathLike[str]]
PRIVACY_MODES = ("local_only", "redacted", "shareable")
_CONTENT_KEYS = frozenset(
    {
        "arguments",
        "args",
        "command",
        "content",
        "diff",
        "error",
        "issue_text",
        "message",
        "messages",
        "output",
        "prompt",
        "raw",
        "request",
        "response",
        "stderr",
        "stdout",
        "text",
        "tool_result",
    }
)
_ID_KEYS = frozenset({"run_id", "session_id", "task_id", "trace_id", "user", "actor"})
_PATH_KEYS = frozenset({"cwd", "file", "path", "repo", "repo_path", "root", "workdir"})


def privacy_mode(value: Any, default: str = "redacted") -> str:
    """Validate a privacy mode name."""
    mode = str(value or default).strip().casefold().replace("-", "_")
    aliases = {
        "local": "local_only",
        "localonly": "local_only",
        "share": "shareable",
        "shared": "shareable",
    }
    mode = aliases.get(mode, mode)
    if mode not in PRIVACY_MODES:
        raise ValueError("privacy mode must be local_only, redacted, or shareable")
    return mode


def _hash_identity(value: Any) -> str:
    text = redact_text(value)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _privacy_value(value: Any, key: Any, mode: str, depth: int = 0) -> Any:
    if depth > 12:
        return "[OMITTED_DEPTH]"
    if key is not None and is_sensitive_key(key):
        return REDACTED_SECRET
    if (
        mode in {"redacted", "shareable"}
        and key is not None
        and str(key).casefold() in _CONTENT_KEYS
    ):
        if mode == "shareable":
            return "[OMITTED_CONTENT]"
        if isinstance(value, str):
            return redact_text(value)
        return "[OMITTED_CONTENT]"
    if mode == "shareable" and key is not None:
        normalized = str(key).casefold()
        if normalized in _ID_KEYS:
            return _hash_identity(value)
        if normalized in _PATH_KEYS:
            return _hash_identity(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _privacy_value(item, item_key, mode, depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_privacy_value(item, key, mode, depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def apply_privacy_mode(value: Any, mode: str = "redacted") -> Any:
    """Return a new value filtered for the selected privacy mode."""
    selected = privacy_mode(mode)
    return _privacy_value(value, None, selected)


def privacy_view(
    records: Iterable[Mapping[str, Any]], mode: str = "redacted"
) -> list[dict[str, Any]]:
    """Return redacted/derived records without mutating the input."""
    selected = privacy_mode(mode)
    return [dict(_privacy_value(record, None, selected)) for record in records]


def redact_trace(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return a redacted trace view while retaining local lifecycle content."""
    return privacy_view(records, "redacted")


def shareable_trace(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return a metadata-only trace view suitable for external sharing."""
    return privacy_view(records, "shareable")


@dataclass(frozen=True)
class PrivacyReport:
    """Summary of a derived privacy export."""

    mode: str
    record_count: int
    redacted_fields: int
    omitted_fields: int
    destination: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report."""
        return {
            "mode": self.mode,
            "record_count": self.record_count,
            "redacted_fields": self.redacted_fields,
            "omitted_fields": self.omitted_fields,
            "destination": redact_text(self.destination),
        }


def _count_privacy_fields(
    value: Any, key: Any = None, mode: str = "redacted"
) -> tuple[int, int]:
    if (
        key is not None
        and str(key).casefold() in _CONTENT_KEYS
        and mode != "local_only"
    ):
        return (0, 1)
    if isinstance(value, Mapping):
        redacted = omitted = 0
        for item_key, item in value.items():
            r, o = _count_privacy_fields(item, item_key, mode)
            redacted += r
            omitted += o
        return redacted, omitted
    if isinstance(value, (list, tuple, set)):
        redacted = omitted = 0
        for item in value:
            r, o = _count_privacy_fields(item, key, mode)
            redacted += r
            omitted += o
        return redacted, omitted
    if isinstance(value, str) and value != redact_text(value):
        return 1, 0
    return 0, 0


def privacy_report(
    records: Iterable[Mapping[str, Any]], mode: str = "redacted"
) -> PrivacyReport:
    """Describe what a privacy transformation removes."""
    selected = privacy_mode(mode)
    values = [dict(record) for record in records]
    redacted = omitted = 0
    for record in values:
        r, o = _count_privacy_fields(record, mode=selected)
        redacted += r
        omitted += o
    return PrivacyReport(selected, len(values), redacted, omitted)


def _read_jsonl(path: PathLike) -> list[dict[str, Any]]:
    raw_path = Path(path)
    candidate = require_contained(raw_path.parent, raw_path.name, must_exist=True)
    if candidate.is_symlink():
        raise SecurityViolation("privacy source must be a regular non-symlink file")
    records: list[dict[str, Any]] = []
    try:
        handle = candidate.open("r", encoding="utf-8")
    except OSError as exc:
        raise SecurityViolation("privacy source is unreadable") from exc
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def _write_jsonl(path: PathLike, records: Iterable[Mapping[str, Any]]) -> None:
    raw_destination = Path(path)
    destination = require_contained(raw_destination.parent, raw_destination.name)
    if destination.is_symlink():
        raise SecurityViolation("privacy destination must not be a symbolic link")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(
                        json.dumps(
                            redact_secrets(record), ensure_ascii=False, sort_keys=True
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except OSError as exc:
        raise SecurityViolation("privacy destination could not be written") from exc


def export_privacy_trace(
    source: Union[PathLike, Iterable[Mapping[str, Any]]],
    destination: PathLike,
    *,
    mode: str = "shareable",
) -> PrivacyReport:
    """Write a derived privacy view without changing the authoritative source."""
    selected = privacy_mode(mode)
    records = (
        _read_jsonl(source)
        if isinstance(source, (str, os.PathLike))
        else [dict(item) for item in source]
    )
    transformed = privacy_view(records, selected)
    _write_jsonl(destination, transformed)
    report = privacy_report(records, selected)
    return PrivacyReport(
        selected,
        report.record_count,
        report.redacted_fields,
        report.omitted_fields,
        str(destination),
    )


export_trace = export_privacy_trace
