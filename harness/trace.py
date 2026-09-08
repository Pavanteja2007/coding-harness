"""Full-trace logging: every prompt, model response, tool call, and result
for a task goes to logs/{task_id}/trace.jsonl (one JSON object per line).
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class TraceLogger:
    """Append-only JSONL trace writer for one task run.

    Assumes the log directory (logs/{task_id}/) is created once at task
    start and that only this task's controller writes to it. Writes are
    flushed immediately so a crashed run still leaves a readable trace.
    """

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / "trace.jsonl"
        self._lock = threading.Lock()

    def log(self, kind: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Append one trace event. `kind` is a short event discriminator,
        e.g. "task_start", "plan", "model_request", "model_response",
        "tool_call", "tool_result", "verify", "attempt_end", "task_end".
        Assumes `data` is JSON-serializable (or None).
        """
        event: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "kind": kind,
        }
        if data is not None:
            event["data"] = _jsonable(data)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list:
        """Return all logged events (for tests and post-run inspection).
        Assumes the file exists and every line is valid JSON written by `log`.
        """
        events = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion of non-JSON-serializable objects so tracing
    never crashes a task run. Paths -> str, sets -> sorted lists,
    dataclasses -> dict; everything else falls back to repr().
    """
    import dataclasses
    from pathlib import Path as _Path

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, _Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_jsonable(v) for v in obj)  # type: ignore[arg-type]
    return repr(obj)
