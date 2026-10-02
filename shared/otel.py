"""OpenTelemetry-compatible JSON export for derived shared views.

This exporter emits the OTLP/HTTP JSON shape using only the Python standard
library.  It never edits the authoritative trace; callers pass a list or a
JSONL source and receive a new payload or destination file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from .privacy import apply_privacy_mode, privacy_mode
from .security import SecurityViolation, redact_secrets, redact_text, require_contained

__all__ = [
    "OTLPExporter",
    "export_otel",
    "export_otlp",
    "main",
    "otel_json",
    "to_otlp",
    "to_otlp_json",
]

PathLike = Union[str, os.PathLike[str]]
_OTEL_SAFE_KEYS = {
    "name",
    "module",
    "event",
    "ts",
    "task_id",
    "run_id",
    "tokens",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "context_tokens",
    "context_limit",
    "event_count",
    "provider",
    "model",
    "outcome",
    "error_class",
    "latency_ms",
    "attributes",
}


def _hex_id(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _timestamp_nanos(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = time.time()
    if seconds <= 0:
        seconds = time.time()
    return str(max(1, int(seconds * 1_000_000_000)))


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float, str)):
        return value
    if value is None:
        return ""
    return redact_text(value)


def _span_attributes(event: Mapping[str, Any], mode: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, value in event.items():
        name = str(key)
        if name not in _OTEL_SAFE_KEYS and not name.startswith("neo."):
            continue
        if name in {"ts", "task_id", "run_id", "attributes"}:
            if name == "attributes" and isinstance(value, Mapping):
                for nested_key, nested_value in value.items():
                    result.append(
                        {
                            "key": f"neo.{str(nested_key)[:80]}",
                            "value": {
                                "stringValue": str(_attribute_value(nested_value))[:500]
                            },
                        }
                    )
            continue
        if name in {"task_id", "run_id"}:
            value = "sha256:" + _hex_id(str(value), 16)
        result.append(
            {
                "key": name,
                "value": {"stringValue": str(_attribute_value(value))[:500]},
            }
        )
    result.append({"key": "neo.privacy_mode", "value": {"stringValue": mode}})
    return result


def to_otlp(
    events: Iterable[Mapping[str, Any]],
    *,
    service_name: str = "neo",
    privacy: str = "shareable",
) -> dict[str, Any]:
    """Return an OTLP JSON-compatible resourceSpans document."""
    mode = privacy_mode(privacy)
    rows = [
        apply_privacy_mode(dict(event), mode)
        for event in events
        if isinstance(event, Mapping)
    ]
    spans: list[dict[str, Any]] = []
    for index, event in enumerate(rows):
        task_id = str(event.get("task_id") or "unknown-task")
        trace_id = _hex_id(task_id, 32)
        span_seed = f"{task_id}:{index}:{event.get('ts', '')}:{event.get('name', '')}"
        start = _timestamp_nanos(event.get("ts"))
        end = str(int(start) + 1_000_000)
        spans.append(
            {
                "traceId": trace_id,
                "spanId": _hex_id(span_seed, 16),
                "name": redact_text(event.get("name") or event.get("event") or "event"),
                "kind": 1,
                "startTimeUnixNano": start,
                "endTimeUnixNano": end,
                "attributes": _span_attributes(event, mode),
                "status": {"code": 1},
            }
        )
    resource_attributes = [
        {
            "key": "service.name",
            "value": {"stringValue": redact_text(service_name) or "neo"},
        },
        {"key": "telemetry.sdk.name", "value": {"stringValue": "neo-shared"}},
        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
    ]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attributes},
                "scopeSpans": [
                    {
                        "scope": {"name": "shared.otel", "version": "1"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


to_otlp_json = to_otlp
otel_json = to_otlp


def _read_source(source: PathLike) -> list[dict[str, Any]]:
    raw_path = Path(source)
    path = require_contained(raw_path.parent, raw_path.name, must_exist=True)
    if path.is_symlink():
        raise SecurityViolation("OTel source must be a regular non-symlink file")
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise SecurityViolation("OTel source is unreadable") from exc
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _write_destination(destination: PathLike, payload: Mapping[str, Any]) -> None:
    raw_path = Path(destination)
    path = require_contained(raw_path.parent, raw_path.name)
    if path.is_symlink():
        raise SecurityViolation("OTel destination must not be a symbolic link")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    redact_secrets(payload),
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except OSError as exc:
        raise SecurityViolation("OTel destination could not be written") from exc


def export_otel(
    source: Union[PathLike, Iterable[Mapping[str, Any]]],
    destination: Optional[PathLike] = None,
    *,
    service_name: str = "neo",
    privacy: str = "shareable",
) -> dict[str, Any]:
    """Build an OTLP document and optionally write it to a separate file."""
    rows = (
        _read_source(source)
        if isinstance(source, (str, os.PathLike))
        else [dict(item) for item in source]
    )
    payload = to_otlp(rows, service_name=service_name, privacy=privacy)
    if destination is not None:
        _write_destination(destination, payload)
    return payload


export_otlp = export_otel


class OTLPExporter:
    """Stateful wrapper for repeated derived OTLP exports."""

    def __init__(self, service_name: str = "neo", privacy: str = "shareable") -> None:
        self.service_name = service_name
        self.privacy = privacy_mode(privacy)

    def export(
        self,
        source: Union[PathLike, Iterable[Mapping[str, Any]]],
        destination: Optional[PathLike] = None,
    ) -> dict[str, Any]:
        """Export one source without modifying it."""
        return export_otel(
            source, destination, service_name=self.service_name, privacy=self.privacy
        )


def main(argv: Optional[list[str]] = None) -> int:
    """Export a JSONL trace as OTLP JSON without modifying the source."""
    parser = argparse.ArgumentParser(prog="python -m shared.otel")
    parser.add_argument("source", help="JSONL trace source")
    parser.add_argument("--out", default=None, help="optional derived JSON destination")
    parser.add_argument("--service-name", default="neo")
    parser.add_argument(
        "--privacy",
        choices=("local_only", "redacted", "shareable"),
        default="shareable",
    )
    args = parser.parse_args(argv)
    try:
        payload = export_otel(
            args.source,
            args.out,
            service_name=args.service_name,
            privacy=args.privacy,
        )
    except SecurityViolation as exc:
        parser.error(str(exc))
    if args.out is None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
