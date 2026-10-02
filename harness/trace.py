"""Full-trace logging: every prompt, model response, tool call, and result
for a task goes to logs/{task_id}/trace.jsonl (one JSON object per line).

trace.jsonl is the permanent record; state.json is the COMPACTED view. The
reversible-compaction retrieval hook (TraceLogger.find_events) reads older
detail back out of the trace on demand (spec item 13).

Redaction is NOT implemented here. This module used to carry a private
sensitive-key set and a narrower set of secret patterns, which meant the
authoritative trace, the shared overlay, diffs, memory rows, and error text
each redacted differently — a credential that the trace missed but memory
caught (or the reverse) was a real divergence, not a theoretical one. The
single implementation now lives in ``shared.security`` and is re-exported
under the same names so existing importers keep working.

What this module DOES own is the boundary: every row goes through
``harness.redaction.redact_for_journal`` before it is written, which is the
one fail-closed, input-capped call site in the harness (see that module's
docstring and the redaction boundary table in ``harness/AGENTS.md``). The raw
re-exported names stay in ``__all__`` for existing importers, but nothing in
this module calls them on a write path any more.
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from harness.redaction import (
    JOURNAL_TEXT_CAP,
    redact_for_journal,
    redact_text_for_journal,
)
from shared.security import (
    REDACTED_SECRET,
    contains_secret,
    redact_secrets,
    redact_text,
)

# Historical placeholder emitted by the old private implementation. It is
# still exported so a consumer that string-matched on it does not break, but
# new code must use the shared placeholder.
LEGACY_REDACTED = "[REDACTED]"

__all__ = [
    "JOURNAL_TEXT_CAP",
    "LEGACY_REDACTED",
    "REDACTED_SECRET",
    "TraceLogger",
    "contains_secret",
    "redact_for_journal",
    "redact_secrets",
    "redact_text",
    "redact_text_for_journal",
]


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

        Every string in `data` is capped at ``JOURNAL_TEXT_CAP`` (200 000
        chars) and redacted through ``harness.redaction.redact_for_journal``
        BEFORE it reaches the file, so a minified asset or a base64 blob in a
        tool result cannot wedge this write and a credential in that result
        cannot outlive the process. If the redactor cannot answer, the row
        still lands - carrying ``(detail withheld: ...)`` - because losing the
        journal row would lose the evidence a reader needs to see the
        withholding.
        """
        event: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "kind": kind,
        }
        if data is not None:
            event["data"] = _jsonable(
                redact_for_journal(data, where=f"TraceLogger.log({kind})")
            )
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_receipt(self, receipt: Any) -> Path:
        """Write this run's provenance receipt next to the trace and log it.

        The receipt is the machine-readable answer to "which model, which
        tools, which image digest, which source state produced this run". It
        is a SEPARATE file (`receipt.json`) rather than a trace row, so a
        consumer can read provenance without replaying the whole transcript,
        and so a malformed receipt can never corrupt the authoritative trace.

        Redaction happens inside ``shared.security.build_run_receipt``; this
        method re-redacts defensively at the same fail-closed boundary as
        ``log`` so a hand-built receipt passed by a caller cannot leak a
        credential into the artifact. Returns the path.
        """
        payload = receipt.as_dict() if hasattr(receipt, "as_dict") else receipt
        clean = redact_for_journal(payload, where="TraceLogger.write_receipt")
        path = self.log_dir / "receipt.json"
        with self._lock:
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        self.log("run_receipt", {"receipt": clean})
        return path

    def read_receipt(self) -> Optional[Dict[str, Any]]:
        """Read this run's receipt, or None when absent/unreadable.

        Never raises: a missing or corrupt receipt is a missing receipt, and
        the caller decides whether that blocks anything.
        """
        path = self.log_dir / "receipt.json"
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

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
                    hay = (
                        kind.lower()
                        + " "
                        + json.dumps(event.get("data", {}), ensure_ascii=False).lower()
                    )
                    if q not in hay:
                        continue
                    dump = json.dumps(event.get("data", {}), ensure_ascii=False)
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
