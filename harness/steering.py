"""Mid-task steering: new user instructions injected while a task runs.

The gap this module closes (steering round, Task A): once a fix/build
task started, the only mid-run control was Ctrl+C (cancel everything)
or waiting. Steering lets the user REDIRECT a live task â€” "actually,
only touch file X", "stop, that's the wrong approach" â€” without losing
the task's current progress, the way Claude Code accepts a new message
mid-response.

DESIGN â€” one mechanism, three surfaces, zero boundary changes:

- The transport is a FILE: logs/{task_id}/steering.jsonl, an
  append-only journal. Any process can inject (the TUI's UI thread, the
  REPL's listener thread, a second terminal, a test driver, an MCP
  client) and the harness polls it at SAFE CHECKPOINTS (see below).
  In-process handoff and cross-process handoff are the same code path.
  CROSS-INSTANCE CORRECTNESS: an injector builds its OWN SteeringBuffer
  on the same directory â€” the loop's instance and the CLI's instance
  are different objects, so the FILE is the only channel. Every poll
  (pending/take/steering_context) RE-SCANS the journal first (refresh)
  so events appended by any other instance become visible; the journal
  is the single source of truth, memory is a cache rebuilt from it.
- The consumer is the loop controller (harness/core.py): between step
  sessions, at bash-session TURN boundaries (between model calls â€” a
  message list at a well-defined state, never mid-tool-execution), and
  at the final success gate. This is the "pause cleanly at the next
  safe checkpoint" of the steering round's Task B.
- Intents are EXPLICIT, never guessed from prose:
    "guide"  â€” incorporate into the CURRENT step session / the next
               step's context; the model continues with the new
               instruction visible (no rollback, no re-plan)
    "replan" â€” abandon the remaining plan, re-plan with the
               accumulated steering as additional planner context;
               the work already in work/ is KEPT and shown to the
               re-planner (never lose progress)
    "abort"  â€” clean stop at the checkpoint: work/, state.json and
               plan.json survive (the resume contract's artifacts),
               the task ends "failed" with an honest aborted note â€”
               explicitly resumable via `neo --resume <id>`

VERIFIER-GATE INTERACTION (steering round, Task C â€” the guarantee this
module must not break): steering NEVER mints or shortcuts success. A
pending (unconsumed) steering event at the final gate BLOCKS success
minting â€” the attempt is treated as not reflecting the user's latest
instructions and the loop re-plans/retries so the fix is re-verified
after steering is incorporated. Success remains verifier-gated always.

RESUME INTERACTION (Task C): the journal replays on construction â€”
an inject without a matching consume is STILL PENDING after a crash
and applies to the resumed run. A steered task that is interrupted
after steering resumes with the steering intact.

Config keys (harness/config.py): steering_enabled (True â€” the OFF
arm turns the whole mechanism off, exactly one code path),
max_pending_steering (16 â€” a bounded queue so a runaway injector
can't grow the journal unboundedly; the overflow inject is refused
with an honest None, never silently dropped).

Trace events (written by the LOOP, not this module â€” same split as
webfetch): steering_injected {seq, intent, where} on consume... no:
the loop logs `steering` {seqs, intents, where} at each consume point
and `steering_replan` / `steering_abort` at the two loop-level
effects. All additive, safe to surface in dashboards.

QUEUED MESSAGES AT THE BATCH BOUNDARY (AGT-10 â€” the transport, not a new
mechanism). The gap this closes: steering was observable only at
checkpoints BETWEEN model calls, so a correction typed while `sleep 300`
was still dispatched could not be seen until the command returned, and the
model then made its next call without it. `QueuedSteering` +
`QueuedSteeringWatcher` add the two missing halves WITHOUT adding a
second channel or a second consume point:

  * QUEUEING is visible. While a tool batch runs, `QueuedSteeringWatcher`
    polls the SAME journal and appends an `{"op": "queue"}` row the moment
    it first sees a seq â€” so "you typed it" and "the harness saw it" are
    two different, both-journalled facts, separated by `waited_s`.
  * CONSUMPTION is visible. `QueuedSteering.deliver(where)` goes through
    `SteeringBuffer.take`, which appends the `{"op": "consume"}` row
    atomically with the return, and adds an `{"op": "deliver"}` row naming
    the seam. A queued-but-never-delivered message is therefore
    REPORTABLE (`QueuedSteering.receipt()` -> `undelivered`) instead of
    being a silent hold.

Both new row kinds are ignored by `_scan` (only `inject` and `consume`
change state), so they are additive to a journal that an older reader
already has. NOTHING here interrupts a tool: the watcher has no hook, no
kill path and no reference to the session. Interrupting an in-flight
command remains `HardAbortWatcher`'s one job (the hard `abort` intent only),
and delivery to the model remains the loop's decision at a seam.
"""

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# The journal file (inside logs/{task_id}/, alongside trace.jsonl â€”
# survives relaunches because the resume contract keeps that dir).
STEERING_FILE = "steering.jsonl"

# Explicit intents (single source of truth; the CLI parses prose to
# one of these at INJECT time â€” the harness never guesses).
INTENT_GUIDE = "guide"
INTENT_REPLAN = "replan"
INTENT_ABORT = "abort"
INTENTS = (INTENT_GUIDE, INTENT_REPLAN, INTENT_ABORT)

# Default cap on pending events (overridable via task.config
# max_pending_steering â€” config-driven per project convention).
DEFAULT_MAX_PENDING = 16


def normalize_intent(intent: Optional[str]) -> str:
    """Validate/normalize an intent to one of INTENTS.

    Assumes intent is a string (or None). Unknown/empty values degrade
    to INTENT_GUIDE â€” the least-destructive intent (incorporate, never
    roll back or cancel) â€” because the caller may be a user typing
    free text; a wrong guess must never abort a run.
    """
    cleaned = str(intent or "").strip().lower()
    return cleaned if cleaned in INTENTS else INTENT_GUIDE


def parse_steering_line(line: str) -> tuple:
    """Parse a typed line into (intent, text) â€” the CLI-side parser.

    Accepts explicit prefixes so a user can choose the strong forms:
      "replan: <text>" / "replan <text>"  -> (replan, text)
      "abort" / "abort: <reason>"         -> (abort, reason)
    Everything else -> (guide, the line verbatim). Bare "abort" /
    bare "replan" carry a placeholder text (never empty â€” inject()
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

    def __repr__(self) -> str:  # pragma: no cover â€” debug convenience
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
    injector processes/threads â€” appends are single write() calls of
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
            return  # no journal yet (fresh task) â€” nothing to replay

    def _scan(self, fh: "Any", from_start: bool = False) -> None:
        """Fold journal lines into memory (BINARY mode â€” the byte
        cursor must match stat().st_size exactly, which text-mode
        newline translation would break). Caller holds the lock (or is
        inside __init__/_replay, single-threaded by construction).

        from_start: re-read the whole file (replay after construction or
        a truncation detected by a shrinking size). Incremental: seek to
        the last scanned byte and fold only new appends â€” the loop polls
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
                # the next scan â€” the cursor does not advance past it
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
        """Fold journal lines appended since our last scan â€” the
        cross-instance transport. The loop's buffer and a CLI-side
        injector buffer are DIFFERENT objects; the journal file is the
        only channel between them, so every consumer-side poll rescans
        first. Idempotent and cheap (one stat + read of the new bytes);
        a missing/shrunk/truncated journal degrades to a full replay
        or an empty inbox â€” never a raise.
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

    @contextlib.contextmanager
    def _journal_process_lock(self):
        """Serialize sequence allocation and consume appends across processes."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        strip; empty is refused â€” an empty instruction means nothing).
        The cap keeps a runaway injector from growing the journal
        without bound; the refusal is returned (the caller acks
        honestly), never silently dropped.
        """
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        with self._journal_process_lock():
            self.refresh()
            with self._lock:
                if len(self._events) - len(self._consumed) >= self.max_pending:
                    return None
                ev = SteeringEvent(
                    seq=self._next_seq,
                    ts=round(time.time(), 3),
                    text=cleaned[:4000],
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
                    del self._events[ev.seq]
                    self._next_seq -= 1
                    return None
                return ev

    # -- consumer side (the loop controller) -------------------------------

    def pending(self) -> List[SteeringEvent]:
        """Unconsumed events, journal order. No side effects except the
        journal rescan (refresh â€” the cross-instance transport)."""
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
        with self._journal_process_lock():
            self.refresh()
            with self._lock:
                candidates = [
                    self._events[s]
                    for s in sorted(self._events)
                    if s not in self._consumed
                ]
                out = []
                for ev in candidates:
                    if self._append(
                        {
                            "op": "consume",
                            "seq": ev.seq,
                            "ts": round(time.time(), 3),
                            "where": str(where),
                        }
                    ):
                        self._consumed.add(ev.seq)
                        out.append(ev)
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
        """All steering texts (consumed AND pending), journal order â€”
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
            block = block[:max_chars] + "\nâ€¦[truncated]"
        return block

    def pending_texts(self) -> List[str]:
        """Just the unconsumed texts (checkpoint acks / feedback)."""
        return [ev.text for ev in self.pending()]

    def append_record(self, obj: Dict[str, Any]) -> bool:
        """Append ONE informational journal row (AGT-10: `queue` / `deliver`).

        Distinct from `_append` in intent, not in mechanics: a record written
        here is AUDIT, not state. `_scan` only folds `inject` and `consume`,
        so any other `op` is ignored on replay â€” which is what makes these
        rows safe to add to a journal an older reader already has, and why a
        replayed pending set can never depend on one. Returns False on an
        I/O failure; the caller decides whether that matters (an audit row
        that could not be written is never allowed to change a run's
        outcome).
        """
        return self._append(dict(obj))

    # -- journal -----------------------------------------------------------

    def _append(self, obj: Dict[str, Any]) -> bool:
        """One atomic line append (binary mode, matching _scan's byte
        cursor); False on any I/O failure (the caller rolls back â€” the
        journal and memory never disagree)."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self._path, "ab") as fh:
                fh.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def journal(self) -> List[Dict[str, Any]]:
        """The full journal (both ops), oldest first â€” for tests and
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


# ===========================================================================
# VEX-CEILING-07 â€” hard abort of an IN-FLIGHT command (gap G14)
# ===========================================================================

# The prompt's hard bound: a hard abort must terminate a long-running child
# process within five seconds. The watcher polls far faster than that so the
# bound is met by the PROCESS kill, not by the poll interval.
ABORT_POLL_INTERVAL_S = 0.05
ABORT_JOIN_TIMEOUT_S = 5.0


class HardAbortWatcher:
    """Watch the steering journal for a HARD intent while a command runs.

    Before this existed, steering was only observable BETWEEN model calls, so
    a `sleep 300` already dispatched to the sandbox could not be interrupted:
    the loop did not look again until the command returned, up to
    `command_timeout_s` later. That is the gap (G14) â€” the user asked to stop
    and the process kept running.

    This watcher closes it. While a command is in flight it polls the SAME
    journal for an `abort` intent and, on seeing one, calls `on_abort` â€” the
    caller's kill hook â€” then stops. The caller wires `on_abort` to the
    execution handle's cancel, which terminates the process tree; because
    `execute_sandboxed` polls its own cancellation token at 50ms and
    `LocalExecutionHandle.cancel()` kills the process tree, the observable
    end-to-end bound is the kill, not this loop.

    Three properties this class is responsible for:
      * it NEVER consumes the event â€” consumption stays with the loop's single
        consume point, so the abort is still pending when the step ends and
        the resumable-stop path runs exactly once;
      * the in-flight command's RESULT is never silently discarded: the
        watcher records `observed_result` and the caller keeps the command's
        output; and
      * it is a daemon thread that always joins (bounded), so it can never
        keep a process alive.

    Assumes `buffer` is a live SteeringBuffer for the same task and
    `on_abort` is a ZERO-ARGUMENT callable (a kill hook: the caller's reason
    for cancelling is available here as `abort_texts` / `abort_reason`, and
    a kill hook has no use for prose). Never raises from its thread body: a
    broken journal or a broken hook degrades to a recorded entry in `errors`,
    which the loop writes to the trace â€” an interrupt that silently failed to
    interrupt is worse than one that reported failing.
    """

    def __init__(
        self,
        buffer: Optional["SteeringBuffer"],
        on_abort: Callable[[], None],
        *,
        poll_interval_s: float = ABORT_POLL_INTERVAL_S,
    ) -> None:
        self._buffer = buffer
        self._on_abort = on_abort
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.abort_seen_at: Optional[float] = None
        self.abort_texts: List[str] = []
        self.errors: List[str] = []
        self._started_at = time.time()
        self._lock = threading.Lock()

    @property
    def abort_reason(self) -> str:
        """The user's abort text, joined â€” what the loop records as the
        reason it stopped."""
        return "; ".join(self.abort_texts) or INTENT_ABORT

    # -- lifecycle -------------------------------------------------------

    def start(self) -> "HardAbortWatcher":
        """Begin watching. A no-op when there is no buffer to watch."""
        if self._buffer is None or self._thread is not None:
            return self
        self._started_at = time.time()
        thread = threading.Thread(
            target=self._run, name="neo-hard-abort-watcher", daemon=True
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self, *, join_timeout_s: float = ABORT_JOIN_TIMEOUT_S) -> bool:
        """Stop watching and join the thread within the bound.

        Returns True when the thread joined inside `join_timeout_s`. Never
        raises; a thread that will not join is reported, not hidden.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(join_timeout_s)))
        return not thread.is_alive()

    def __enter__(self) -> "HardAbortWatcher":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # -- observed state --------------------------------------------------

    @property
    def aborted(self) -> bool:
        """True once a hard abort has been observed (and acted on)."""
        return self.abort_seen_at is not None

    def elapsed_to_abort_s(self) -> Optional[float]:
        """Seconds between the watcher's start and the observed abort."""
        if self.abort_seen_at is None:
            return None
        return round(self.abort_seen_at - self._started_at, 3)

    # -- the loop --------------------------------------------------------

    def _run(self) -> None:
        self._started_at = time.time()
        while not self._stop.is_set():
            try:
                if self._buffer is not None and self._buffer.has_intent(INTENT_ABORT):
                    texts = [
                        ev.text
                        for ev in self._buffer.pending()
                        if ev.intent == INTENT_ABORT
                    ]
                    with self._lock:
                        if self.abort_seen_at is None:
                            self.abort_seen_at = time.time()
                            self.abort_texts = list(texts)
                    try:
                        # Zero-argument by contract: this is a kill hook, and
                        # a hook whose signature does not match is a bug the
                        # loop must SEE (recorded in `errors`) rather than an
                        # abort that silently fails to interrupt anything.
                        self._on_abort()
                    except Exception as exc:  # pragma: no cover â€” defensive
                        with self._lock:
                            self.errors.append(
                                f"on_abort hook failed: {type(exc).__name__}: {exc}"
                            )
                    # One abort is enough: the loop's own consume point owns
                    # the stop, so the watcher retires.
                    return
            except Exception as exc:  # pragma: no cover â€” defensive
                with self._lock:
                    self.errors.append(f"watcher poll failed: {exc}")
            self._stop.wait(self._poll_interval_s)


# ===========================================================================
# AGT-10 â€” QUEUED MESSAGES AT THE TOOL-BATCH BOUNDARY
# ===========================================================================

# A message typed while a tool batch is running is noticed on the same 50ms
# cadence `HardAbortWatcher` uses, so "queued" is observable on the same
# timescale the abort already is. The two watchers never run at once: the
# abort watcher owns an in-flight command's kill, the queue watcher only
# records arrivals and never touches the call.
QUEUE_POLL_INTERVAL_S = ABORT_POLL_INTERVAL_S

#: What a queue is bounded by. The unconsumed cap is already
#: `max_pending_steering` (a shared `SteeringBuffer` refusal, so a message
#: this round never refuses one an earlier round accepted). What is new is
#: the AUDIT cap: an arrival this watcher could not journal must not be
#: reported as "queued and acknowledged" when it was in fact only seen.
DEFAULT_MAX_QUEUE_ROWS = 64


def partition_intents(
    events: List[SteeringEvent],
) -> Tuple[List[SteeringEvent], List[SteeringEvent], List[SteeringEvent]]:
    """Split queued events into `(abort, replan, guide)`, each in journal order.

    ONE definition of the dispatch precedence, so a consumer at a turn
    boundary and a consumer at a batch boundary cannot disagree about which
    instruction wins when a burst carries more than one intent. Nothing is
    dropped: every event lands in exactly one bucket, and the buckets are
    ordered strongest-first so a caller can dispatch them in that order and
    leave the rest for the next checkpoint. An unrecognised intent is a
    `guide` â€” `SteeringEvent` already normalises it at construction, and
    `guide` is the least-destructive bucket, which is the right place for an
    intent nobody classified.

    Never raises and never mutates `events`.
    """
    abort: List[SteeringEvent] = []
    replan: List[SteeringEvent] = []
    guide: List[SteeringEvent] = []
    for ev in events:
        if ev.intent == INTENT_ABORT:
            abort.append(ev)
        elif ev.intent == INTENT_REPLAN:
            replan.append(ev)
        else:
            guide.append(ev)
    return abort, replan, guide


class SteeringDelivery:
    """What one batch-boundary seam consumed, already partitioned.

    The partitions are computed ONCE, here, from `partition_intents`, so a
    consumer that dispatches `abort` then `replan` then `guide` cannot
    disagree with another consumer about which instruction won â€” and cannot
    forget to look at a strong intent, because `action` is derived here and
    is the only field a caller needs to branch on for the strong case.

    `waited_s` is the per-message delay between the moment the harness first
    SAW the message (the `queue` row) and the moment it consumed it. It is
    the measurement of the gap this round exists to close, so it is a
    reported number and never a claim.
    """

    __slots__ = (
        "abort",
        "events",
        "guide",
        "replan",
        "tool",
        "turn",
        "waited_s",
        "where",
    )

    def __init__(
        self,
        where: str,
        events: Tuple[SteeringEvent, ...],
        *,
        turn: Optional[int] = None,
        tool: str = "",
        waited_s: Optional[Dict[int, float]] = None,
    ) -> None:
        self.where = str(where)
        self.events = tuple(events)
        self.turn = turn
        self.tool = str(tool or "")
        self.waited_s = dict(waited_s or {})
        self.abort, self.replan, self.guide = partition_intents(list(self.events))

    # -- shape ------------------------------------------------------------

    @property
    def empty(self) -> bool:
        """True when the seam had nothing to deliver."""
        return not self.events

    @property
    def seqs(self) -> List[int]:
        """Consumed seqs, in the order they were typed."""
        return [ev.seq for ev in self.events]

    @property
    def texts(self) -> List[str]:
        """Consumed texts, in the order they were typed."""
        return [ev.text for ev in self.events]

    @property
    def max_wait_s(self) -> float:
        """The longest a message in this delivery waited to be delivered."""
        return round(max(self.waited_s.values()), 3) if self.waited_s else 0.0

    # -- dispatch ---------------------------------------------------------

    def action(self) -> str:
        """The strongest intent in this delivery: abort | replan | guide | none.

        `action` is a METHOD, not a property, so `SteeringDelivery` can be
        used with the same truthiness conventions as the rest of the module
        and a caller cannot accidentally branch on a cached attribute that a
        later mutation made stale.
        """
        if self.abort:
            return INTENT_ABORT
        if self.replan:
            return INTENT_REPLAN
        if self.guide:
            return INTENT_GUIDE
        return "none"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "where": self.where,
            "turn": self.turn,
            "tool": self.tool,
            "action": self.action(),
            "seqs": self.seqs,
            "intents": [ev.intent for ev in self.events],
            "texts": self.texts,
            "max_wait_s": self.max_wait_s,
        }

    def __len__(self) -> int:
        return len(self.events)

    def __repr__(self) -> str:  # pragma: no cover â€” debug convenience
        return (
            f"SteeringDelivery(where={self.where!r}, action={self.action()!r}, "
            f"seqs={self.seqs})"
        )


class QueuedSteeringWatcher:
    """Record steering ARRIVALS while a tool batch runs. Never interrupts.

    Assumes `queue` is a live `QueuedSteering` over the same task's
    `SteeringBuffer`. It polls `pending()` and, the first time it sees each
    seq, appends a `{"op": "queue"}` journal row with the wait it observed.

    Three properties this class is responsible for, and they are the whole
    reason it exists separately from the loop:

      * it NEVER consumes. Delivery stays with the loop's single consume
        point, so an arrival recorded here is still PENDING if the run dies,
        and a resumed run still applies it.
      * it NEVER interrupts anything. There is no hook, no session and no
        kill path in this class. A queued message is delivered at a batch
        boundary â€” the safe seam â€” and a mutating call is never split to make
        room for it. Hard-abort interruption of an in-flight command remains
        `HardAbortWatcher`'s one job, and only for the `abort` intent.
      * it is a daemon thread that always joins (bounded), so it can never
        keep a process alive.

    Never raises from its thread body: a broken journal degrades to a
    recorded entry in `errors`, because an arrival the loop cannot see is
    worse than one the loop is told about late.
    """

    def __init__(
        self,
        queue: Optional["QueuedSteering"],
        *,
        poll_interval_s: float = QUEUE_POLL_INTERVAL_S,
    ) -> None:
        self._queue = queue
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = time.time()
        self._lock = threading.Lock()
        self.errors: List[str] = []

    # -- lifecycle -------------------------------------------------------

    def start(self) -> "QueuedSteeringWatcher":
        """Begin watching. A no-op when there is no queue to watch."""
        if self._queue is None or self._queue.buffer is None:
            return self
        if self._thread is not None:
            return self
        self._started_at = time.time()
        thread = threading.Thread(
            target=self._run, name="neo-steering-queue-watcher", daemon=True
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self, *, join_timeout_s: float = ABORT_JOIN_TIMEOUT_S) -> bool:
        """Stop watching and join the thread within the bound.

        Returns True when the thread joined inside `join_timeout_s`. Never
        raises; a thread that will not join is reported, not hidden â€” a
        watcher still polling after the loop closed would race the loop's own
        consume point.
        """
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(join_timeout_s)))
        return not thread.is_alive()

    def __enter__(self) -> "QueuedSteeringWatcher":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # -- observed state --------------------------------------------------

    def elapsed_to_first_arrival_s(self) -> Optional[float]:
        """Seconds between the watcher's start and the first arrival it saw."""
        first = self._queue.first_arrival_at if self._queue is not None else None
        if first is None:
            return None
        return round(first - self._started_at, 3)

    @property
    def joined(self) -> bool:
        """True when no thread is running (never started, or stopped)."""
        return self._thread is None

    # -- the loop --------------------------------------------------------

    def _run(self) -> None:
        assert self._queue is not None  # start() guarantees this
        while not self._stop.is_set():
            try:
                self._queue.observe_arrivals(where="in_flight")
            except Exception as exc:  # pragma: no cover â€” defensive
                with self._lock:
                    self.errors.append(f"queue poll failed: {exc}")
            self._stop.wait(self._poll_interval_s)


class QueuedSteering:
    """The batch-boundary view of one task's steering inbox (AGT-10).

    `SteeringBuffer` answers "what is unconsumed". This answers the two
    questions a QUEUE raises and the buffer cannot: was this actually
    *queued* while the tools were still running, and was it actually
    *delivered at the seam it was queued for? Both answers are journal rows
    appended through the same file and the same single-write append as
    `inject` and `consume`, so a reader of `steering.jsonl` sees the whole
    life of a message â€” written, seen, consumed â€” and
    `receipt()["undelivered"]` names anything that got stuck in between.

    It is NOT a second transport and NOT a second consume point: delivery
    goes through `SteeringBuffer.take`, which is still the only place a
    `consume` row is written. `queued_without_watcher` exists because a
    message typed while the model was THINKING has no watcher to record it,
    and reporting it as unobserved would be a lie in the other direction â€”
    the honest record is "queued, not observed by a watcher".

    A `None` buffer is the OFF arm (`steering_enabled: False`) and every
    method degrades to an empty delivery rather than raising, so a consumer
    can call the seam unconditionally.
    """

    def __init__(
        self,
        buffer: Optional[SteeringBuffer],
        *,
        task_id: str = "",
        max_queue_rows: int = DEFAULT_MAX_QUEUE_ROWS,
    ) -> None:
        self.buffer = buffer
        self.task_id = str(task_id or getattr(buffer, "task_id", "") or "")
        self.max_queue_rows = max(1, int(max_queue_rows))
        self.queued_at: Dict[int, float] = {}  # seq -> when we first saw it
        self.watched: set = set()  # seqs an in-flight WATCHER observed
        self.deliveries: List[SteeringDelivery] = []
        self.rows_unwritten: List[int] = []  # seqs whose queue row could not land
        self.first_arrival_at: Optional[float] = None
        self._lock = threading.Lock()

    # -- producer-observant side (the watcher) ----------------------------

    def observe_arrivals(self, *, where: str = "in_flight") -> List[int]:
        """Record every PENDING seq not yet recorded as queued. Returns the
        newly recorded seqs, in journal order.

        Idempotent and safe to call from the loop's own thread as well as
        the watcher's: a seq is recorded once, whichever caller sees it
        first. Order is the journal's (seq), which is the order the user
        typed, so a burst recorded by one poll keeps its order.

        Raises whatever `buffer.pending()` raises â€” see the note on the
        `pending()` call. This is the only method that can, and its only
        caller is the watcher, which is explicitly non-raising.
        """
        if self.buffer is None:
            return []
        # A `pending()` failure is NOT swallowed here: the watcher's whole job
        # is to notice arrivals, so a journal it cannot read is a fact the
        # caller must see (it records the failure in `errors`, which the loop
        # writes to the trace). Swallowing it would report "nothing arrived"
        # when the truth is "the harness could not look".
        pending = self.buffer.pending()
        fresh: List[int] = []
        for ev in sorted(pending, key=lambda e: e.seq):
            with self._lock:
                if ev.seq in self.queued_at:
                    continue
                self.queued_at[ev.seq] = time.time()
                if self.first_arrival_at is None:
                    self.first_arrival_at = self.queued_at[ev.seq]
            fresh.append(ev.seq)
            with self._lock:
                self.watched.add(ev.seq)
            self._write_queue_row(ev, where=where, seen_at=self.queued_at[ev.seq])
        return fresh

    def _write_queue_row(
        self, ev: SteeringEvent, *, where: str, seen_at: float
    ) -> bool:
        """One `{"op": "queue"}` audit row. False (and recorded) on I/O
        failure â€” an arrival we could not journal must not be reported as
        acknowledged."""
        if len(self.rows_unwritten) >= self.max_queue_rows:
            with self._lock:
                if ev.seq not in self.rows_unwritten:
                    self.rows_unwritten.append(ev.seq)
            return False
        ok = self._append(
            {
                "op": "queue",
                "seq": ev.seq,
                "ts": round(seen_at, 3),
                "intent": ev.intent,
                "source": ev.source,
                "where": str(where),
                "waited_s": round(max(0.0, seen_at - ev.ts), 3),
            }
        )
        if not ok:
            with self._lock:
                if ev.seq not in self.rows_unwritten:
                    self.rows_unwritten.append(ev.seq)
        return ok

    def _append(self, obj: Dict[str, Any]) -> bool:
        if self.buffer is None:
            return False
        try:
            return bool(self.buffer.append_record(obj))
        except Exception:  # pragma: no cover â€” defensive
            return False

    # -- consumer side (the loop's seam) ----------------------------------

    def watch(
        self, *, poll_interval_s: float = QUEUE_POLL_INTERVAL_S
    ) -> QueuedSteeringWatcher:
        """A started watcher over this queue (the in-flight observer)."""
        return QueuedSteeringWatcher(self, poll_interval_s=poll_interval_s).start()

    def deliver(
        self, where: str, *, turn: Optional[int] = None, tool: str = ""
    ) -> SteeringDelivery:
        """Consume the queued events and return them, IN THE ORDER TYPED.

        This is the seam. `where` is the checkpoint slug the consume rows
        carry ("agent-batch-7", "final-gate", â€¦); the events are taken with
        `SteeringBuffer.take`, so a consume row is journalled atomically with
        the return and a crash between the take and the loop's use of the
        text is visible as a delivery whose text never reached the model.

        Consumption is strictly paired with delivery: nothing is consumed
        here that the caller does not receive in the returned object, and the
        returned object carries the partitions (`abort` / `replan` / `guide`)
        the caller must dispatch â€” so a strong intent cannot be silently
        downgraded to guidance by forgetting to check it.
        """
        if self.buffer is None:
            return SteeringDelivery(where=where, events=(), turn=turn, tool=tool)
        events = self.buffer.take(where)
        if not events:
            return SteeringDelivery(where=where, events=(), turn=turn, tool=tool)
        # A message with no watcher behind it (typed while the model was
        # thinking) is recorded here, so the receipt never claims a message
        # was unobserved. A seq the watcher already recorded is NOT
        # re-recorded: one arrival, one `queue` row, and the wait measured
        # from the first observation rather than the last.
        seen_at = time.time()
        for ev in sorted(events, key=lambda e: e.seq):
            with self._lock:
                already = ev.seq in self.queued_at
                first = self.queued_at.setdefault(ev.seq, seen_at)
            if not already:
                self._write_queue_row(ev, where=where, seen_at=first)
        waited = {
            ev.seq: round(max(0.0, seen_at - self.queued_at.get(ev.seq, seen_at)), 3)
            for ev in events
        }
        delivery = SteeringDelivery(
            where=where,
            events=tuple(sorted(events, key=lambda e: e.seq)),
            turn=turn,
            tool=tool,
            waited_s=waited,
        )
        with self._lock:
            self.deliveries.append(delivery)
        self._append(
            {
                "op": "deliver",
                "ts": round(seen_at, 3),
                "where": str(where),
                "turn": turn,
                "tool": str(tool or ""),
                "seqs": list(delivery.seqs),
                "intents": [ev.intent for ev in delivery.events],
                "waited_s": waited,
            }
        )
        return delivery

    def pending(self) -> List[SteeringEvent]:
        """Unconsumed events, journal order (delegates to the buffer)."""
        if self.buffer is None:
            return []
        return list(self.buffer.pending())

    # -- receipts ---------------------------------------------------------

    def receipt(self) -> Dict[str, Any]:
        """The audit view: what was queued, what was delivered, what is stuck.

        `undelivered` is the load-bearing key â€” the seqs this queue SAW and
        never handed to a consumer. A non-empty list means a message the user
        typed is sitting in the journal, which is a fact the loop must
        report rather than hide.
        """
        delivered: List[int] = []
        for delivery in self.deliveries:
            delivered.extend(delivery.seqs)
        with self._lock:
            queued = sorted(self.queued_at)
            unwritten = sorted(set(self.rows_unwritten))
            watched = set(self.watched)
        delivered_set = set(delivered)
        undelivered = [s for s in queued if s not in delivered_set]
        return {
            "enabled": self.buffer is not None,
            "task_id": self.task_id,
            "queued": len(queued),
            "queued_seqs": queued,
            "delivered": len(delivered),
            "delivered_seqs": sorted(delivered_set),
            "deliveries": len(self.deliveries),
            "undelivered": undelivered,
            "queue_rows_unwritten": unwritten,
            # Queued with no watcher behind it: the user typed it while the
            # model was THINKING, not while a tool was running. Recorded
            # honestly rather than reported as an in-flight observation.
            "queued_without_watcher": [s for s in queued if s not in watched],
        }
