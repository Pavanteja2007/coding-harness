"""Mid-task steering: new user instructions injected while a task runs.

The gap this module closes (steering round, Task A): once a fix/build
task started, the only mid-run control was Ctrl+C (cancel everything)
or waiting. Steering lets the user REDIRECT a live task — "actually,
only touch file X", "stop, that's the wrong approach" — without losing
the task's current progress, the way Claude Code accepts a new message
mid-response.

DESIGN — one mechanism, three surfaces, zero boundary changes:

- The transport is a FILE: logs/{task_id}/steering.jsonl, an
  append-only journal. Any process can inject (the TUI's UI thread, the
  REPL's listener thread, a second terminal, a test driver, an MCP
  client) and the harness polls it at SAFE CHECKPOINTS (see below).
  In-process handoff and cross-process handoff are the same code path.
  CROSS-INSTANCE CORRECTNESS: an injector builds its OWN SteeringBuffer
  on the same directory — the loop's instance and the CLI's instance
  are different objects, so the FILE is the only channel. Every poll
  (pending/take/steering_context) RE-SCANS the journal first (refresh)
  so events appended by any other instance become visible; the journal
  is the single source of truth, memory is a cache rebuilt from it.
- The consumer is the loop controller (harness/core.py): between step
  sessions, at bash-session TURN boundaries (between model calls — a
  message list at a well-defined state, never mid-tool-execution), and
  at the final success gate. This is the "pause cleanly at the next
  safe checkpoint" of the steering round's Task B.
- Intents are EXPLICIT, never guessed from prose:
    "guide"  — incorporate into the CURRENT step session / the next
               step's context; the model continues with the new
               instruction visible (no rollback, no re-plan)
    "replan" — abandon the remaining plan, re-plan with the
               accumulated steering as additional planner context;
               the work already in work/ is KEPT and shown to the
               re-planner (never lose progress)
    "abort"  — clean stop at the checkpoint: work/, state.json and
               plan.json survive (the resume contract's artifacts),
               the task ends "failed" with an honest aborted note —
               explicitly resumable via `vex --resume <id>`

VERIFIER-GATE INTERACTION (steering round, Task C — the guarantee this
module must not break): steering NEVER mints or shortcuts success. A
pending (unconsumed) steering event at the final gate BLOCKS success
minting — the attempt is treated as not reflecting the user's latest
instructions and the loop re-plans/retries so the fix is re-verified
after steering is incorporated. Success remains verifier-gated always.

RESUME INTERACTION (Task C): the journal replays on construction —
an inject without a matching consume is STILL PENDING after a crash
and applies to the resumed run. A steered task that is interrupted
after steering resumes with the steering intact.

Config keys (harness/config.py): steering_enabled (True — the OFF
arm turns the whole mechanism off, exactly one code path),
max_pending_steering (16 — a bounded queue so a runaway injector
can't grow the journal unboundedly; the overflow inject is refused
with an honest None, never silently dropped).

Trace events (written by the LOOP, not this module — same split as
webfetch): steering_injected {seq, intent, where} on consume... no:
the loop logs `steering` {seqs, intents, where} at each consume point
and `steering_replan` / `steering_abort` at the two loop-level
effects. All additive, safe to surface in dashboards.
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# The journal file (inside logs/{task_id}/, alongside trace.jsonl —
# survives relaunches because the resume contract keeps that dir).
STEERING_FILE = "steering.jsonl"

# Explicit intents (single source of truth; the CLI parses prose to
# one of these at INJECT time — the harness never guesses).
INTENT_GUIDE = "guide"
INTENT_REPLAN = "replan"
INTENT_ABORT = "abort"
INTENTS = (INTENT_GUIDE, INTENT_REPLAN, INTENT_ABORT)

# Default cap on pending events (overridable via task.config
# max_pending_steering — config-driven per project convention).
DEFAULT_MAX_PENDING = 16


def normalize_intent(intent: Optional[str]) -> str:
    """Validate/normalize an intent to one of INTENTS.

    Assumes intent is a string (or None). Unknown/empty values degrade
    to INTENT_GUIDE — the least-destructive intent (incorporate, never
    roll back or cancel) — because the caller may be a user typing
    free text; a wrong guess must never abort a run.
    """
    cleaned = str(intent or "").strip().lower()
    return cleaned if cleaned in INTENTS else INTENT_GUIDE


def parse_steering_line(line: str) -> tuple:
    """Parse a typed line into (intent, text) — the CLI-side parser.

    Accepts explicit prefixes so a user can choose the strong forms:
      "replan: <text>" / "replan <text>"  -> (replan, text)
      "abort" / "abort: <reason>"         -> (abort, reason)
    Everything else -> (guide, the line verbatim). Bare "abort" /
    bare "replan" carry a placeholder text (never empty — inject()
    refuses empty text, and a bare strong intent is a legitimate
    instruction: stop, respectively redo the plan). Assumes `line` is
    the raw non-empty user input; never raises.
    """
    text = (line or "").strip()
    low = text.lower()
    if low == "abort" or low.startswith("abort:"):
        body = text.split(":", 1)[1].strip() if ":" in text else ""
        return INTENT_ABORT, body or "stop this task at the next checkpoint"
    if low == "replan" or low.startswith("replan:") or low.startswith("replan "):
        body = text[7:].strip() if len(text) > 6 else ""
        return (
            INTENT_REPLAN,
            body or "re-plan the remaining work at the next step boundary",
        )
    return INTENT_GUIDE, text


class SteeringEvent:
    """One steering instruction (immutable record).

    seq is the per-task monotonic id (journal order); source is a
    short provenance slug ("tui", "repl", "file", "test"); text is the
    user's instruction verbatim; intent is one of INTENTS.
    """

    __slots__ = ("intent", "seq", "source", "text", "ts")

    def __init__(
        self, seq: int, ts: float, text: str, intent: str, source: str
    ) -> None:
        self.seq = int(seq)
        self.ts = float(ts)
        self.text = str(text)
        self.intent = normalize_intent(intent)
        self.source = str(source or "unknown")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "text": self.text,
            "intent": self.intent,
            "source": self.source,
        }

    def __repr__(self) -> str:  # pragma: no cover — debug convenience
        return (
            f"SteeringEvent(seq={self.seq}, intent={self.intent!r}, "
            f"text={self.text[:40]!r})"
        )


class SteeringBuffer:
    """Thread-safe, crash-safe steering inbox for ONE task.

    Owns logs/{task_id}/steering.jsonl: an append-only journal where
    every inject is {"op": "inject", ...} and every consume is
    {"op": "consume", "seq": N, "where": "<checkpoint slug>"}. The
    UNCONSUMED set (injects without consumes) is what the loop polls;
    it is rebuilt from the journal on construction, so a crashed run
    resumes with its pending steering intact (Task C).

    Assumes one consumer process (the harness loop) but any number of
    injector processes/threads — appends are single write() calls of
    one JSON line; the consume path is exclusively the loop's.
    Never raises on I/O: a broken journal degrades to an empty inbox
    (steering is an enhancement; it must not take a run down).
    """

    def __init__(
        self,
        log_dir: Path,
        task_id: str,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.task_id = str(task_id)
        self.max_pending = max(1, int(max_pending))
        self._path = self.log_dir / STEERING_FILE
        self._lock = threading.Lock()
        self._events: Dict[int, SteeringEvent] = {}  # seq -> event
        self._consumed: set = set()
        self._next_seq = 1
        self._scan_pos = 0  # journal bytes already folded into memory
        self._replay()

    # -- replay (crash/resume safety) ------------------------------------

    def _replay(self) -> None:
        """Rebuild the in-memory state from the journal.

        Best-effort: malformed lines are skipped (a truncated final
        line from a crash mid-append is expected); read errors leave
        the buffer empty. Never raises.
        """
        try:
            with open(self._path, "rb") as fh:
                self._scan(fh, from_start=True)
        except OSError:
            return  # no journal yet (fresh task) — nothing to replay

    def _scan(self, fh: "Any", from_start: bool = False) -> None:
        """Fold journal lines into memory (BINARY mode — the byte
        cursor must match stat().st_size exactly, which text-mode
        newline translation would break). Caller holds the lock (or is
        inside __init__/_replay, single-threaded by construction).

        from_start: re-read the whole file (replay after construction or
        a truncation detected by a shrinking size). Incremental: seek to
        the last scanned byte and fold only new appends — the loop polls
        at every checkpoint, so the rescan must stay cheap.

        A partial trailing line (another process mid-append) is folded
        when complete on the NEXT scan: the byte cursor only advances
        past complete lines (ends with newline), so a torn tail is
        re-read once it completes. Duplicate seqs are idempotent
        (same seq -> same event; last inject wins, consumes union).
        """
        if from_start:
            fh.seek(0)
            self._events = {}
            self._consumed = set()
            self._next_seq = 1
        else:
            fh.seek(self._scan_pos)
        pos = self._scan_pos if not from_start else 0
        for raw in fh:
            pos += len(raw)
            if not raw.endswith(b"\n"):
                # torn tail (another process mid-append): retry it on
                # the next scan — the cursor does not advance past it
                break
            self._scan_pos = pos
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # corrupt complete line: skipped for good
            if not isinstance(obj, dict):
                continue
            op = str(obj.get("op") or "")
            if op == "inject":
                try:
                    ev = SteeringEvent(
                        seq=int(obj.get("seq") or 0),
                        ts=float(obj.get("ts") or 0.0),
                        text=str(obj.get("text") or ""),
                        intent=obj.get("intent"),
                        source=str(obj.get("source") or ""),
                    )
                except (TypeError, ValueError):
                    continue
                if ev.seq > 0 and ev.text:
                    self._events[ev.seq] = ev
                    self._next_seq = max(self._next_seq, ev.seq + 1)
            elif op == "consume":
                try:
                    self._consumed.add(int(obj.get("seq")))
                except (TypeError, ValueError):
                    pass

    def refresh(self) -> None:
        """Fold journal lines appended since our last scan — the
        cross-instance transport. The loop's buffer and a CLI-side
        injector buffer are DIFFERENT objects; the journal file is the
        only channel between them, so every consumer-side poll rescans
        first. Idempotent and cheap (one stat + read of the new bytes);
        a missing/shrunk/truncated journal degrades to a full replay
        or an empty inbox — never a raise.
        """
        with self._lock:
            try:
                size = self._path.stat().st_size
            except OSError:
                return  # no journal (fresh task): nothing new
            if size < self._scan_pos:
                # truncated/replaced journal (a fresh start archived the
                # old dir): rebuild from scratch
                try:
                    with open(self._path, "rb") as fh:
                        self._scan(fh, from_start=True)
                except OSError:
                    return
                return
            if size == self._scan_pos:
                return  # nothing appended
            try:
                with open(self._path, "rb") as fh:
                    self._scan(fh)
            except OSError:
                return  # vanished between stat and open: next refresh retries

    # -- producer side (any thread / any process) --------------------------

    def inject(
        self,
        text: str,
        intent: Optional[str] = None,
        source: str = "cli",
    ) -> Optional[SteeringEvent]:
        """Append one steering instruction; returns it, or None when
        refused (empty text or the pending queue is at its cap).

        Assumes `text` is the user's instruction (non-empty after
        strip; empty is refused — an empty instruction means nothing).
        The cap keeps a runaway injector from growing the journal
        without bound; the refusal is returned (the caller acks
        honestly), never silently dropped.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        self.refresh()  # pick up other instances' seqs before assigning ours
        with self._lock:
            if len(self._events) - len(self._consumed) >= self.max_pending:
                return None
            ev = SteeringEvent(
                seq=self._next_seq,
                ts=round(time.time(), 3),
                text=cleaned[:4000],  # bound one instruction's size
                intent=normalize_intent(intent),
                source=str(source or "cli"),
            )
            self._next_seq += 1
            self._events[ev.seq] = ev
            if not self._append(
                {
                    "op": "inject",
                    "seq": ev.seq,
                    "ts": ev.ts,
                    "text": ev.text,
                    "intent": ev.intent,
                    "source": ev.source,
                }
            ):
                # journal write failed: roll the in-memory state back
                # so pending() never reports an event no resume will see
                del self._events[ev.seq]
                self._next_seq -= 1
                return None
            return ev

    # -- consumer side (the loop controller) -------------------------------

    def pending(self) -> List[SteeringEvent]:
        """Unconsumed events, journal order. No side effects except the
        journal rescan (refresh — the cross-instance transport)."""
        self.refresh()
        with self._lock:
            return [
                self._events[s] for s in sorted(self._events) if s not in self._consumed
            ]

    def take(self, where: str) -> List[SteeringEvent]:
        """Return the unconsumed events and mark them consumed.

        `where` is the checkpoint slug for the audit journal
        ("step-2-turn-3", "step-boundary", "replan", "final-gate").
        Consumption is atomic with the return under the lock; each
        consume is journaled so a crash between take() and its effect
        is visible (and a resume never re-applies consumed steering).
        """
        self.refresh()
        with self._lock:
            out = [
                self._events[s] for s in sorted(self._events) if s not in self._consumed
            ]
            for ev in out:
                self._consumed.add(ev.seq)
                self._append(
                    {
                        "op": "consume",
                        "seq": ev.seq,
                        "ts": round(time.time(), 3),
                        "where": str(where),
                    }
                )
            return out

    def has_intent(self, intent: str) -> bool:
        """True when any UNCONSUMED event carries this intent.

        The loop's dispatch rule: abort > replan > guide (the strong
        intents win when they arrive together).
        """
        want = normalize_intent(intent)
        return any(ev.intent == want for ev in self.pending())

    # -- rendering helpers (prompt sections) -------------------------------

    def steering_context(self, max_chars: int = 4000) -> str:
        """All steering texts (consumed AND pending), journal order —
        the accumulated user intent for the RE-PLAN prompt.

        Consumed events shaped the work already in work/; the
        re-planner must see them too (it also sees the current diff).
        Capped with a truncation marker; empty -> "" (the caller
        renders "(none)").
        """
        self.refresh()
        with self._lock:
            lines = []
            for s in sorted(self._events):
                ev = self._events[s]
                lines.append(f"- [{ev.intent}] {ev.text}")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars] + "\n…[truncated]"
        return block

    def pending_texts(self) -> List[str]:
        """Just the unconsumed texts (checkpoint acks / feedback)."""
        return [ev.text for ev in self.pending()]

    # -- journal -----------------------------------------------------------

    def _append(self, obj: Dict[str, Any]) -> bool:
        """One atomic line append (binary mode, matching _scan's byte
        cursor); False on any I/O failure (the caller rolls back — the
        journal and memory never disagree)."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self._path, "ab") as fh:
                fh.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def journal(self) -> List[Dict[str, Any]]:
        """The full journal (both ops), oldest first — for tests and
        dashboards. Malformed lines skipped; read errors -> []."""
        out: List[Dict[str, Any]] = []
        try:
            with open(self._path, "rb") as fh:
                for raw in fh:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
        except OSError:
            return []
        return out
