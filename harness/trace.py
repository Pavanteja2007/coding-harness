"""Full-trace logging: every prompt, model response, tool call, and result
for a task goes to logs/{task_id}/trace.jsonl (one JSON object per line).

trace.jsonl is the permanent record; state.json is the COMPACTED view. The
reversible-compaction retrieval hook (TraceLogger.find_events) reads older
detail back out of the trace on demand (spec item 13).
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

    def find_events(
        self,
        query: str,
        kinds: Optional[list] = None,
        limit: int = 5,
        max_chars: int = 4000,
    ) -> list:
        """Retrieve hook for REVERSIBLE COMPACTION (spec item 13).

        state.json is the compacted view (step descriptions, no outputs);
        trace.jsonl keeps everything. This method is the mechanism by which
        a later step session pulls older detail BACK on demand: given a
        query, it returns the most recent matching trace entries so the
        harness can re-inject them into the live session's context.

        Matching: case-insensitive substring against the event kind AND a
        compact JSON rendering of the event data (so a query can hit a
        command's text, a tool output, a verify tail, a plan entry, ...).
        Returns the LAST `limit` matches in chronological order, each as
        {"line": <1-based line number>, "kind": str, "data": Any}; each
        entry's JSON dump is capped at max_chars/limit chars so the whole
        returned set fits inside max_chars. Assumes the file was written
        by `log` (one JSON object per line); a malformed line is skipped,
        never raised — retrieval must not crash a step session.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        per_event_cap = max(200, max_chars // max(1, limit))
        matches: list = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for n, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = str(event.get("kind", ""))
                    if kinds is not None and kind not in kinds:
                        continue
                    hay = kind.lower() + " " + json.dumps(
                        event.get("data", {}), ensure_ascii=False).lower()
                    if q not in hay:
                        continue
                    dump = json.dumps(
                        event.get("data", {}), ensure_ascii=False)
                    if len(dump) > per_event_cap:
                        dump = dump[:per_event_cap] + "…[truncated]"
                    matches.append({"line": n, "kind": kind, "data": dump})
        except OSError:
            return []
        if len(matches) > limit:
            matches = matches[-limit:]
        return matches


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
