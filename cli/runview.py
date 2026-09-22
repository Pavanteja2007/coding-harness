"""Live run views — the structured companions to the free-form trace feed.

This module is a PURE read-only layer over data that already exists on
disk (same discipline as cli.tracelog, Task E of the live-trace round):
it folds the SAME trace.jsonl events the harness writes into

- a live TODO CHECKLIST (the plan + step_end/attempt events — the
  harness's own task decomposition, never a parallel tracking system),
- the STATE-MACHINE state (harness/state_machine.py's transitions.jsonl
  audit trail, read with its documented "last valid to-state" contract),
- the COMPLETION CARD facts (status/attempts/cost/model calls/elapsed
  from the trace's own result + final_verify + git_output events, files
  from state.json — the run's own records, re-derived at render time so
  the card can never drift from what actually happened).

It writes NOTHING. The TUI renders it; every public function is total
(malformed events / missing files degrade to honest empties, never
raise — a view layer must not take a run down).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cli import ui

__all__ = [
    "ACTIVE",
    "DONE",
    "FAILED",
    "PENDING",
    "SKIPPED",
    "TodoModel",
    "TodoStep",
    "card_lines",
    "fmt_elapsed",
    "read_machine_state",
    "read_run_facts",
    "read_task_progress",
]

# ---------------------------------------------------------------------------
# The todo checklist — plan + step events, folded live (Task A)
# ---------------------------------------------------------------------------

PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

#: ACTIVE is not a stored state — TodoModel.active_id marks the step
#: whose session is currently in flight (from model_request {step-N}).
ACTIVE = "active"


class TodoStep:
    """One plan sub-step and its live state.

    state is PENDING / DONE / FAILED / SKIPPED; the in-flight marker
    lives on the model (active_id), not the step, so a step that is
    being retried renders as "current" while its stored state stays
    honest about the last outcome.
    """

    __slots__ = ("checkpoint", "description", "sid", "state")

    def __init__(self, sid: int, description: str, checkpoint: str = "") -> None:
        self.sid = sid
        self.description = description or ""
        self.checkpoint = checkpoint or ""
        self.state = PENDING

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"TodoStep({self.sid}, {self.state}, {self.description!r})"


class TodoModel:
    """Fold trace events into a live checklist of the task's plan.

    Data sources (all existing, none invented):
    - ``plan``            -> the step list (the harness's decomposition)
    - ``step_end``        -> ok ? check the step off : mark it failed
    - ``step_skipped_resume`` -> check off a step completed pre-crash
    - ``attempt_start`` (>1)   -> uncheck everything (the harness rolls
      the work back and resets completed steps on a retry — the trace
      replays the survivors via step_skipped_resume / step_end)
    - ``model_request`` {step-N} -> the "current" marker

    A RE-PLAN (steering) re-emits ``plan``: steps whose description
    matches one already DONE keep their checkmark — the re-plan builds
    on work/, it does not undo it. Assumes events are dicts in arrival
    order from trace.jsonl; malformed ones are ignored, never raised.
    """

    def __init__(self) -> None:
        self.steps: List[TodoStep] = []
        self.active_id: Optional[int] = None

    # -- folding -----------------------------------------------------------

    def consume(self, event: Dict[str, Any]) -> bool:
        """Fold one trace event; True when the checklist changed."""
        try:
            kind = (event or {}).get("kind") or ""
            data = (event or {}).get("data")
            if not isinstance(data, dict):
                data = {}
            if kind == "plan":
                return self._apply_plan(data.get("plan") or [])
            if kind == "step_end":
                return self._end_step(data)
            if kind == "step_skipped_resume":
                return self._skip_step(data)
            if kind == "attempt_start":
                return self._start_attempt(data)
            if kind == "model_request":
                return self._touch_active(data)
            if (
                kind in ("task_end", "result", "attempt_end")
                and self.active_id is not None
            ):
                self.active_id = None
                return True
            return False
        except Exception:
            return False

    def _apply_plan(self, plan: List[Any]) -> bool:
        prior = {
            st.description: st.state for st in self.steps if st.state in (DONE, SKIPPED)
        }
        new_steps: List[TodoStep] = []
        for st in plan:
            if not isinstance(st, dict):
                continue
            try:
                sid = int(st.get("id"))
            except (TypeError, ValueError):
                continue
            desc = str(st.get("description") or "")
            step = TodoStep(sid, desc, str(st.get("checkpoint") or ""))
            if desc and prior.get(desc) in (DONE, SKIPPED):
                step.state = prior[desc]
            new_steps.append(step)
        if not new_steps:
            return False
        changed = [(s.sid, s.description, s.state) for s in new_steps] != [
            (s.sid, s.description, s.state) for s in self.steps
        ]
        self.steps = new_steps
        if self.active_id is not None and self.active_id not in {
            s.sid for s in new_steps
        }:
            self.active_id = None
            changed = True
        return changed

    def _end_step(self, data: Dict[str, Any]) -> bool:
        try:
            sid = int(data.get("step_id"))
        except (TypeError, ValueError):
            return False
        step = self.find(sid)
        if step is None:
            return False
        step.state = DONE if data.get("ok") else FAILED
        if self.active_id == sid:
            self.active_id = None
        return True

    def _skip_step(self, data: Dict[str, Any]) -> bool:
        raw = str(data.get("step") or "")
        head = raw.split(". ", 1)[0]
        try:
            sid = int(head)
        except ValueError:
            return False
        step = self.find(sid)
        if step is None:
            return False
        step.state = SKIPPED
        return True

    def _start_attempt(self, data: Dict[str, Any]) -> bool:
        try:
            n = int(data.get("attempt") or 1)
        except (TypeError, ValueError):
            n = 1
        if n <= 1:
            return False
        changed = self.active_id is not None or any(
            s.state in (DONE, FAILED, SKIPPED) for s in self.steps
        )
        for s in self.steps:
            s.state = PENDING
        self.active_id = None
        return changed

    def _touch_active(self, data: Dict[str, Any]) -> bool:
        step = str(data.get("step") or "")
        if not step.startswith("step-"):
            if self.active_id is not None:
                self.active_id = None
                return True
            return False
        try:
            sid = int(step[len("step-") :])
        except ValueError:
            return False
        if sid == self.active_id:
            return False
        self.active_id = sid
        return True

    # -- reading -----------------------------------------------------------

    def find(self, sid: int) -> Optional[TodoStep]:
        """The step with this id, or None."""
        for s in self.steps:
            if s.sid == sid:
                return s
        return None

    def progress(self) -> Tuple[int, int]:
        """(#done-or-skipped, #total) — the honest checklist count."""
        done = sum(1 for s in self.steps if s.state in (DONE, SKIPPED))
        return done, len(self.steps)

    def state_of(self, step: TodoStep) -> str:
        """ACTIVE for the in-flight step, else its stored state."""
        if self.active_id is not None and step.sid == self.active_id:
            return ACTIVE
        return step.state


# ---------------------------------------------------------------------------
# The state-machine state (Task B) — transitions.jsonl, the documented
# live-status surface (harness/state_machine.py's reading helpers).
# ---------------------------------------------------------------------------


def read_machine_state(log_dir: Path) -> Optional[str]:
    """The task's current state-machine state from its audit trail.

    Reads logs/{task_id}/transitions.jsonl and returns the LAST record
    that carries a to_state and is not marked invalid — the same
    contract as harness.state_machine.current_phase, minus its blind
    spot: steering rounds append {"event": ...} records WITHOUT a
    to_state (they deliberately keep the phase unchanged), and a reader
    that returns the first valid record's field would surface the
    string "None" for them. Missing/unreadable file -> None ("no state
    yet", never an error).
    """
    try:
        text = (Path(log_dir) / "transitions.jsonl").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("valid") is False:
            continue
        to = rec.get("to_state")
        if to:
            return str(to)
    return None


# ---------------------------------------------------------------------------
# Completion-card facts (Task C) — re-derived from the run's own records
# ---------------------------------------------------------------------------


def read_run_facts(log_dir: Path) -> Dict[str, Any]:
    """The numbers for the completion card, from the run's own files.

    Sources: trace.jsonl's result / final_verify / git_output /
    model_response / task_start events (ts span = time taken) and
    state.json's files_touched. A missing file or malformed line leaves
    the corresponding field empty — read_run_facts never raises, and it
    never writes anything. Assumes log_dir is logs/{task_id}/.
    """
    log_dir = Path(log_dir)
    facts: Dict[str, Any] = {
        "task_id": log_dir.name,
        "status": None,
        "reason": "",
        "attempts": None,
        "cost_usd": None,
        "model_calls": 0,
        "elapsed_s": None,
        "issue": "",
        "mode": None,
        "target_passed": None,
        "regression_passed": None,
        "flaky": None,
        "verify_summary": "",
        "files": [],
        "branch": "",
        "commit_sha": "",
    }
    events: List[Dict[str, Any]] = []
    try:
        for line in (
            (log_dir / "trace.jsonl")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        ):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    except OSError:
        events = []

    ts_vals: List[float] = []
    usage_cost = 0.0
    saw_result = False
    for ev in events:
        try:
            ts = float(ev.get("ts"))
        except (TypeError, ValueError):
            ts = 0.0
        if ts:
            ts_vals.append(ts)
        kind = ev.get("kind")
        data = ev.get("data")
        if not isinstance(data, dict):
            data = {}
        if kind == "task_start":
            if not facts["issue"]:
                issue = str(data.get("issue_text") or "")
                facts["issue"] = issue.splitlines()[0][:90] if issue else ""
            if facts["mode"] is None:
                facts["mode"] = data.get("mode")
        elif kind == "model_response":
            facts["model_calls"] += 1
            usage = data.get("usage") or {}
            try:
                usage_cost += float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind == "final_verify":
            facts["target_passed"] = bool(data.get("target_passed"))
            facts["regression_passed"] = bool(data.get("regression_passed"))
            facts["flaky"] = bool(data.get("flaky"))
            raw = str(data.get("raw") or "")
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if lines:
                facts["verify_summary"] = lines[-1][:80]
        elif kind == "git_output":
            facts["branch"] = str(data.get("branch") or "")
            facts["commit_sha"] = str(data.get("commit_sha") or "")
        elif kind == "result":
            saw_result = True
            facts["status"] = str(data.get("status") or facts["status"] or "")
            try:
                facts["attempts"] = int(data.get("attempts"))
            except (TypeError, ValueError):
                pass
            try:
                cost = float(data.get("cost_usd"))
            except (TypeError, ValueError):
                cost = None
            if cost is not None:
                facts["cost_usd"] = cost
        elif kind == "task_end":
            if not saw_result:
                facts["status"] = str(data.get("status") or "")
                try:
                    facts["attempts"] = int(data.get("attempt"))
                except (TypeError, ValueError):
                    pass
            if data.get("reason"):
                facts["reason"] = str(data["reason"])[:120]
    if not saw_result and facts["cost_usd"] is None and usage_cost:
        facts["cost_usd"] = round(usage_cost, 6)
    if ts_vals:
        facts["elapsed_s"] = round(ts_vals[-1] - ts_vals[0], 1)
    try:
        state = json.loads((log_dir / "state.json").read_text(encoding="utf-8"))
        files = state.get("files_touched") if isinstance(state, dict) else None
        if isinstance(files, list):
            facts["files"] = [str(f) for f in files]
    except (OSError, ValueError):
        pass
    return facts


def fmt_elapsed(secs: Optional[float]) -> str:
    """Seconds -> a compact human duration (34s / 9m 27s / 1h 03m)."""
    if secs is None:
        return ""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s:02d}s"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


def card_lines(facts: Dict[str, Any], mode: str = "fix") -> List[str]:
    """Markup lines for the completion card (Task C).

    One polished final view pulling together what is otherwise scattered:
    status/attempts/model calls (the result event), time taken (the trace's
    own ts span), cost (the cost ledger's total), files changed
    (state.json), the verification verdict (final_verify's chips + its own
    summary line), and the git-native output (branch + commit — the
    harness's git output produces a branch/commit/PR description, not a
    hosted PR URL, so the card shows exactly what exists). Question /
    research modes get the read-only variant (no files/tests/branch rows).
    Assumes facts came from read_run_facts; any missing field simply
    drops its row.
    """
    dot = ui.DOT
    status = str(facts.get("status") or "unknown")
    ok = status == "success"
    mode = str(mode or facts.get("mode") or "fix")
    mark = ui.GLYPHS["ok" if ok else "fail"]
    style = "vex.ok" if ok else "vex.error"
    head = (
        f"[{style}]{mark} {status.upper()}[/] [vex.muted]{dot}[/] "
        f"[vex.accent]{mode}[/] [vex.muted]{dot}[/] [{ui.TEXT_PRIMARY}]{_escape_issue(facts)}[/]"
    )
    rows: List[str] = [head]

    chips: List[str] = []
    if facts.get("attempts") is not None:
        chips.append(f"{facts['attempts']} attempt(s)")
    if facts.get("model_calls"):
        n = int(facts["model_calls"])
        chips.append(f"{n} model call{'s' if n != 1 else ''}")
    if facts.get("elapsed_s") is not None:
        chips.append(fmt_elapsed(facts["elapsed_s"]))
    if facts.get("cost_usd") is not None:
        chips.append(ui.fmt_cost(float(facts["cost_usd"])))
    if chips:
        rows.append(
            f"[vex.muted]   {dot}[/] [{ui.TEXT_PRIMARY}]"
            + f" {dot} ".join(chips)
            + "[/]"
        )

    if mode in ("question", "research"):
        rows.append(
            f"[vex.muted]   {dot} trace[/] [{ui.TEXT_PRIMARY}]{facts.get('task_id') or ''}[/]"
        )
        return rows

    files = facts.get("files") or []
    if files:
        shown = ", ".join(files[:4]) + (
            f" (+{len(files) - 4} more)" if len(files) > 4 else ""
        )
        rows.append(f"[vex.muted]   {dot} files[/] [{ui.TEXT_PRIMARY}]{shown}[/]")
    if facts.get("target_passed") is not None:
        t = "[vex.ok]PASS[/]" if facts["target_passed"] else "[vex.error]FAIL[/]"
        r = "[vex.ok]PASS[/]" if facts["regression_passed"] else "[vex.error]FAIL[/]"
        flaky = " · flaky!" if facts.get("flaky") else ""
        summary = (
            f" [vex.muted]—[/] [{ui.TEXT_PRIMARY}]{facts['verify_summary']}[/]"
            if facts.get("verify_summary")
            else ""
        )
        rows.append(
            f"[vex.muted]   {dot} tests[/] target {t} [vex.muted]{dot}[/] suite {r}"
            f"[vex.warn]{flaky}[/]{summary}"
        )
    if facts.get("branch"):
        sha = str(facts.get("commit_sha") or "")[:8]
        branch = facts["branch"]
        tail = f" [vex.muted]{dot}[/] [{ui.TEXT_PRIMARY}]{sha}[/]" if sha else ""
        rows.append(f"[vex.muted]   {dot} branch[/] [vex.accent2]{branch}[/]{tail}")
    if facts.get("reason") and not ok:
        rows.append(
            f"[vex.muted]   {dot} note[/] [{ui.TEXT_PRIMARY}]{facts['reason'][:100]}[/]"
        )
    return rows


def _short_model(name: str) -> str:
    """A model id shortened for a narrow dashboard column.
    'openai/gpt-4o' -> 'gpt-4o'; 'z-ai/glm-5.3-free' -> 'glm-5.3-free'."""
    return (name or "").rsplit("/", 1)[-1]


def read_task_progress(log_root: Path, task_id: str) -> Dict[str, Any]:
    """Live per-task facts for the multi-task benchmark dashboard
    (interaction-polish round Task D), re-derived from the run's OWN
    records the same way the completion card is — never a second
    tracking system, never a write:

    - ``trace.jsonl``   -> status (running / result), model calls, cost
      (usage sum with the result event as the authority), phase (the
      last lifecycle-ish event kind), first-event ts (the elapsed clock)
    - ``{tid}.runtime/model_ledger.jsonl`` (runtime's per-call routing
      ledger, Boundary 2's own surface) -> the tier hint(s) actually
      routed on + the model names in play

    A task not started yet (no dir / empty trace) still gets an honest
    record (status "queued", zeros everywhere) so the dashboard can
    show the WHOLE set at once, not only what has begun. total=False
    is not an option here: a benchmark view must never raise over a
    half-written file a worker is still appending to.
    """
    facts: Dict[str, Any] = {
        "task_id": task_id,
        "status": "queued",
        "phase": "",
        "model_calls": 0,
        "cost_usd": 0.0,
        "tokens": 0,
        "elapsed_s": None,
        "tier": "",
        "models": [],
        "started_ts": None,
    }
    root = Path(log_root)
    trace = root / task_id / "trace.jsonl"
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    usage_cost = 0.0
    result_cost: Optional[float] = None
    phase = ""
    try:
        text = (
            trace.read_text(encoding="utf-8", errors="replace")
            if trace.is_file()
            else ""
        )
    except OSError:
        text = ""
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        try:
            ts = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        kind = str(ev.get("kind") or "")
        data = ev.get("data")
        if not isinstance(data, dict):
            data = {}
        if kind == "model_response":
            facts["model_calls"] += 1
            usage = data.get("usage") or {}
            try:
                usage_cost += float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                facts["tokens"] += int(usage.get("tokens") or 0)
            except (TypeError, ValueError):
                pass
            m = str(usage.get("model") or "")
            if m and m not in facts["models"]:
                facts["models"].append(m)
        elif kind == "task_start":
            facts["status"] = "running"
            phase = "starting"
        elif kind in ("attempt_start", "plan", "baseline_verify", "retrieval"):
            facts["status"] = "running"
            phase = {
                "attempt_start": "editing",
                "plan": "planning",
                "baseline_verify": "baseline",
                "retrieval": "retrieval",
            }.get(kind, phase)
        elif kind == "verify":
            phase = "verifying"
        elif kind == "final_verify":
            phase = "final verify"
        elif kind == "result":
            facts["status"] = str(data.get("status") or "running")
            try:
                result_cost = float(data.get("cost_usd"))
            except (TypeError, ValueError):
                result_cost = None
            phase = facts["status"]
        elif kind == "task_end":
            if facts["status"] in ("queued", "running"):
                facts["status"] = str(data.get("status") or "running")
            phase = facts["status"]
    if first_ts is not None:
        facts["started_ts"] = first_ts
        facts["last_ts"] = last_ts
        # A finished run reports its OWN trace ts span; a running one
        # reports nothing here and lets the caller tick elapsed against
        # the wall clock (a task stuck in a 300s model call has no new
        # events for 300s but the time is genuinely burning).
        if facts["status"] not in ("queued", "running"):
            facts["elapsed_s"] = round((last_ts or first_ts) - first_ts, 1)
    facts["cost_usd"] = (
        round(result_cost, 6) if result_cost is not None else round(usage_cost, 6)
    )
    facts["phase"] = phase

    # routing ledger: the tier hint(s) this task's calls actually used
    ledger = root / f"{task_id}.runtime" / "model_ledger.jsonl"
    hints: List[str] = []
    try:
        if ledger.is_file():
            for line in ledger.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                hint = rec.get("difficulty_hint") or rec.get("routed_via_hint")
                if hint and str(hint) not in hints:
                    hints.append(str(hint))
                m = str(rec.get("model") or "")
                if m and m not in facts["models"]:
                    facts["models"].append(m)
    except OSError:
        pass
    if hints:
        facts["tier"] = "/".join(h[:1].upper() + h[1:] for h in hints[:3])
    facts["models"] = [_short_model(m) for m in facts["models"][:3]]
    return facts


def _escape_issue(facts: Dict[str, Any]) -> str:
    """The issue fragment for the card head, display-safe (no markup)."""
    from rich.markup import escape

    return escape(str(facts.get("issue") or facts.get("task_id") or "")[:60])
