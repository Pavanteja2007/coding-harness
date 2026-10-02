"""Headless adapter over the shared slash-command contract (Prompt 04).

One command system, three surfaces. The TUI and the REPL render commands
through ``cli/commands.py``; this module is the third surface: it resolves
a command with the SAME registry, runs the SAME handler the REPL runs (the
rich output is captured instead of printed to a human's screen), and turns
the result into a stable exit code from ``cli/exit_codes.py``.

Three properties this module guarantees:

- **No faked interactivity.** Commands that need a live run, a prompt, or a
  modal are refused (exit 2, usage error) with an honest reason — a
  non-TTY session never pretends to have answered a question. Commands
  whose work a first-class CLI flag already owns point at that flag.
- **No silent state.** The run's own ``trace.jsonl`` is the only authority
  for run state; this module never writes application state and never
  treats captured console text as a verification result.
- **Stable exit codes.** Success is 0, a usage/refusal is 2, and anything
  that raises is classified by the shared ``cli.exit_codes`` contract
  rather than leaking a traceback.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cli import commands as _commands
from cli.exit_codes import EXIT_CODES

__all__ = [
    "HeadlessCommandResult",
    "headless_help",
    "run_command_line",
]


#: The verdict reported when a command has no RUN behind it. Declared in
#: ``cli.commands`` (R2-18) and re-exported here for callers that already
#: imported it from this module; the value has ONE definition, so the two
#: surfaces cannot disagree about a no-run document.
NO_RUN_VERDICT = _commands.NO_RUN_VERDICT


@dataclass(frozen=True)
class HeadlessCommandResult:
    """One headless command invocation, ready for a caller to render."""

    command: str
    args: str
    status: str
    text: str
    exit_code: int
    presentation: str = "inline"
    recovery: Tuple[str, ...] = ()
    unavailable_reason: str = ""
    requires: str = ""
    state_before: str = "idle"
    state_after: str = "idle"
    events: Tuple[Dict[str, Any], ...] = ()
    task_id: str = ""
    verification_state: str = "not_run"
    #: The RUN's status and the journal's own verification evidence, kept
    #: separate from ``status`` (the command's lifecycle word) because the
    #: verdict is reduced from these and must not be reduced from that.
    run_status: str = ""
    evidence: Tuple[Dict[str, Any], ...] = ()
    model: str = ""
    provider: str = ""
    log_root: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command completed with the success exit code."""
        return self.exit_code == EXIT_CODES["success"]

    @property
    def verdict(self) -> str:
        """The honest outcome, from the ONE fail-closed authority.

        R2-17: `status` is the command's own lifecycle word and can be
        `ok` for a command that inspected an UNVERIFIED run. `verdict` is
        the reduction a script can branch on safely, and it can only be
        `verified` with clean verifier evidence.

        R2-18: the reduction is `cli.commands.command_verdict`, shared
        with the TUI and the REPL, and it is fed the RUN's status and the
        journal's own evidence. It used to be fed `self.status` — the
        command's word — so a headless command against a run whose
        journal said `completed_verified` with clean evidence reported
        `unverified` / `verified: false`. A machine record that denies a
        verified run is the same class of lie as one that promotes an
        unverified one.

        A command with NO run behind it (`/help`, `/doctor`, `/theme`)
        reports :data:`cli.commands.NO_RUN_VERDICT` rather than a run
        verdict at all. Its own success is `exit_code == 0`, which is
        already in the document; inventing a run verdict for it would
        either claim a verification nobody ran or read as a failed run,
        and both are lies. Never raises.
        """
        if not self.task_id:
            return _commands.NO_RUN_VERDICT
        return _commands.command_verdict(
            str(self.run_status or "").strip() or self.status,
            task_id=self.task_id,
            verification_state=self.verification_state,
            evidence=self.evidence,
        )

    def cost(self) -> Dict[str, Any]:
        """Ledger reconciliation for this invocation's run, or ``{}``.

        R2-17 (item 4): a script that asked `/cost` gets the same
        numbers a human does, read from the router's own per-call ledger
        rather than only the conversation's usage rows. A command with no
        run behind it returns an empty document, never a zero that reads
        as "we checked and it was free".
        """
        if not self.task_id or not self.log_root:
            return {}
        try:
            from cli.runview import cost_reconciliation

            return dict(cost_reconciliation(self.log_root, self.task_id))
        except Exception:
            return {}

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable record of this invocation."""
        verdict = self.verdict
        return {
            "command": self.command,
            "args": self.args,
            "status": self.status,
            "verdict": verdict,
            "verified": verdict == "verified",
            "text": self.text,
            "exit_code": self.exit_code,
            "presentation": self.presentation,
            "recovery": list(self.recovery),
            "unavailable_reason": self.unavailable_reason,
            "requires": self.requires,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "state": self.state_after,
            "events": [dict(event) for event in self.events],
            "task_id": self.task_id,
            "verification_state": self.verification_state,
            "cost": self.cost(),
            "model": self.model,
            "provider": self.provider,
        }


def _usage_error(
    command: str, args: str, message: str, status: str = "refused"
) -> HeadlessCommandResult:
    """Build a usage-error result (exit 2) with an actionable reason."""
    return HeadlessCommandResult(
        command=command,
        args=args,
        status=status,
        text=message,
        exit_code=EXIT_CODES["usage_error"],
        recovery=("edit-input", "return-safe-state"),
    )


def _with_idle_events(result: HeadlessCommandResult) -> HeadlessCommandResult:
    """Attach a complete idle command envelope to an early input refusal."""
    outcome = _commands.command_record(
        command=result.command,
        args=result.args,
        surface="headless",
        status=result.status,
        state_before="idle",
        state_after="idle",
        exit_code=result.exit_code,
        message=result.text,
        recovery=result.recovery,
    )
    return replace(
        result,
        state_before=outcome.state_before,
        state_after=outcome.state_after,
        events=tuple(event.to_dict() for event in outcome.events),
    )


def headless_help() -> str:
    """Render the headless command table from the shared registry.

    Every required command appears with its availability in a non-TTY
    session, so a script author sees the same contract the TUI palette
    shows — never a shorter or diverging list.
    """
    lines = ["neo commands (headless surface)", ""]
    context = _commands.CommandContext(surface="headless")
    for spec in _commands.COMMAND_SPECS:
        availability = _commands.command_availability(spec, context)
        policy = _commands.headless_policy(spec.name)
        usage = _commands.command_usage(spec)
        if availability.available and policy == "mapped":
            detail = ""
        elif availability.available and policy == "flag-only":
            detail = f" -> {_commands.headless_equivalent(spec.name)}"
        else:
            detail = f" [{availability.reason}]"
        lines.append(f"  {usage}{detail}")
    lines.append("")
    lines.append("Refused commands need an interactive session; the TUI and REPL")
    lines.append("provide it. Captured output never mints run state or verification.")
    return "\n".join(lines)


@contextlib.contextmanager
def _captured_console():
    """Capture the shared rich console so REPL handlers can run headlessly.

    The REPL handlers print through ``cli.ui.console()``. Capturing that one
    console is what lets the headless surface reuse the SAME handler code
    instead of maintaining a second rendering path that could drift. The
    captured text is returned to the caller, which decides how to render it
    (``--json`` embeds it, a human run prints it). Captured text is display
    only: it is never parsed for status or verification.
    """
    from cli import ui

    console = ui.console()
    with console.capture() as capture:
        yield capture
    return capture.get()


def _default_artifact_root() -> Path:
    """The harness-owned, repo-outside artifact root for a headless run.

    Same authority as the interactive shells (``cli.session``'s
    ``resolve_artifact_root``) so `neo run` cannot silently write a run
    directory into the user's repository.
    """
    try:
        from cli.session import resolve_artifact_root

        return Path(resolve_artifact_root(None, None)["log_root"])
    except Exception:
        return Path("logs")


def run_command_line(
    line: str,
    *,
    log_root: Optional[Path] = None,
    repo: Optional[Path] = None,
    state: Optional[Dict[str, Any]] = None,
) -> HeadlessCommandResult:
    """Execute one slash command headlessly with normalized state and events.

    The command uses the shared registry and REPL handler. Journal-derived
    state, verifier evidence, task identity, and the normalized command events
    are returned separately from captured display text.
    """
    from cli import interactive as _interactive

    raw = str(line or "").strip()
    if not raw:
        return _with_idle_events(_usage_error("", "", "empty command line"))
    if not raw.startswith("/"):
        return _with_idle_events(
            _usage_error(
                "",
                raw,
                "headless commands start with '/': try /help",
            )
        )

    pieces = raw.split(None, 1)
    name = pieces[0].lower()
    args = pieces[1].strip() if len(pieces) > 1 else ""
    effective_log_root = Path(log_root) if log_root else _default_artifact_root()
    session_state: Dict[str, Any] = dict(state or {})
    session_state.setdefault("approval_policy", _commands.ApprovalPolicy())
    # An explicitly supplied log root is the caller's read model. Mark it so
    # session browsers do not fall back to the machine-wide cross-repo index
    # and return this machine's real sessions to an isolated headless run.
    if log_root is not None:
        session_state["_headless_scoped"] = True
    if repo is not None:
        session_state["repo"] = str(repo)
        session_state.setdefault("file_config", {})
    last: Dict[str, Any] = {}
    target_hint = str(session_state.get("active_task_id") or "")
    if target_hint:
        last = {"task_id": target_hint}
    try:
        sessions = _interactive.list_sessions(effective_log_root, limit=1)
    except Exception:
        sessions = []
    if not target_hint and sessions and sessions[0].get("task_id"):
        last = {"task_id": str(sessions[0]["task_id"])}
    live = _interactive._live_run()
    target = _interactive.active_task_id(effective_log_root, last, live)
    snapshot: Dict[str, Any] = {}
    if target:
        task_dir = _interactive._safe_task_dir(target, effective_log_root)
        if task_dir is not None:
            try:
                from cli.runview import read_live_projection

                snapshot = read_live_projection(task_dir)
            except Exception:
                snapshot = {}
    pending = bool(
        target
        and _interactive._pending_approval_request(effective_log_root, target)
        is not None
    )
    state_before = _commands.normalize_terminal_state(
        snapshot,
        in_flight=live is not None,
        waiting_for_approval=pending,
        has_task=target is not None,
    )
    context = _commands.surface_command_context(
        "headless",
        in_flight=live is not None,
        snapshot=snapshot,
        task_id=str(target or ""),
        pending_approval=pending,
    )
    resolution = _commands.resolve_command_line(raw, context)

    def finish(
        result: HeadlessCommandResult,
        after_snapshot: Optional[Dict[str, Any]] = None,
    ) -> HeadlessCommandResult:
        """Attach journal state, shared events, and provider facts to a result."""
        data = dict(after_snapshot or {})
        if not data and target:
            task_dir = _interactive._safe_task_dir(target, effective_log_root)
            if task_dir is not None:
                try:
                    from cli.runview import read_live_projection

                    data = read_live_projection(task_dir)
                except Exception:
                    data = {}
        state_after = _commands.normalize_terminal_state(
            data,
            in_flight=_interactive._live_run() is not None,
            waiting_for_approval=pending,
            has_task=target is not None,
        )
        # R2-18: the headless envelope is BUILT here by the shared reducer,
        # never re-emitted from the REPL handler's `last_command`. It used
        # to copy that record's `events` verbatim, so a mapped command
        # published `events[*].surface == "repl"` while an early refusal
        # published `"headless"` — the same event kinds under two different
        # surface labels depending only on which branch happened to build
        # the envelope. A stream whose producer label changes with the code
        # path is not a contract a script can read.
        recorded = session_state.get("last_command")
        if (
            isinstance(recorded, dict)
            and recorded.get("command") == result.command
            and int(recorded.get("exit_code") or 0) == int(result.exit_code)
        ):
            # The REPL handler ran and its DECISION is authoritative for a
            # mapped command; the envelope around it is this surface's.
            status = str(recorded.get("status") or result.status)
            exit_code = int(result.exit_code)
        else:
            status = result.status
            exit_code = int(result.exit_code)
        run_status = str(data.get("status") or snapshot.get("status") or "")
        evidence_rows = [
            row
            for row in (data.get("verification_evidence") or [])
            if isinstance(row, dict)
        ]
        outcome = _commands.command_record(
            command=result.command or name,
            args=result.args,
            surface="headless",
            spec=resolution.spec,
            status=status,
            exit_code=exit_code,
            state_before=state_before,
            state_after=state_after,
            message=result.text[:240],
            recovery=result.recovery,
            task_id=str(target or ""),
            verification_state=str(
                data.get("verification_state")
                or snapshot.get("verification_state")
                or "not_run"
            ),
            run_status=run_status,
            evidence=evidence_rows,
        )
        file_config = session_state.get("file_config")
        if not isinstance(file_config, dict):
            file_config = {}
        return replace(
            result,
            status=outcome.status,
            exit_code=outcome.exit_code,
            state_before=outcome.state_before,
            state_after=outcome.state_after,
            events=tuple(event.to_dict() for event in outcome.events),
            task_id=str(target or result.task_id or ""),
            verification_state=outcome.verification_state,
            run_status=run_status,
            evidence=tuple(dict(row) for row in evidence_rows),
            model=str(session_state.get("model") or file_config.get("model") or ""),
            provider=str(
                session_state.get("provider") or file_config.get("provider") or ""
            ),
            # R2-17 (item 4): the `--json` document carries the same
            # ledger reconciliation a human gets from `/cost`, read from
            # this invocation's own resolved root — not a guessed one.
            log_root=str(effective_log_root),
        )

    if resolution.spec is None:
        # R2-18: an unregistered name is only "unknown" when no project or
        # global template backs it. A custom command IS a real command on
        # the two interactive surfaces, so a headless refusal has to say
        # which of the two cases it is — "unknown command" for a command
        # the user can see in `.neo/commands/` is a lie about their repo.
        if _commands.is_custom_command_line(raw, session_state.get("repo") or repo):
            return finish(
                _usage_error(
                    name,
                    args,
                    f"{name} is a custom command that runs as a live agent "
                    f"task, which needs an interactive session:\n"
                    f"  neo {name} <arguments>\n"
                    f"{_commands.command_recovery_hint(None)}",
                    status="refused",
                )
            )
        return finish(
            _usage_error(
                name,
                args,
                f"unknown command: {name}\n"
                f"{_commands.command_recovery_hint(None)}\n"
                "run /help for the headless command list",
                status="unknown",
            )
        )

    spec = resolution.spec
    recovery = spec.failure_recovery

    if resolution.status == "invalid":
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status="invalid",
                text=f"{resolution.message}\n{_commands.command_recovery_hint(spec)}",
                exit_code=EXIT_CODES["usage_error"],
                presentation=spec.result_presentation,
                recovery=recovery,
            )
        )

    if resolution.status in ("disabled", "hidden", "refused", "queued"):
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status=resolution.status,
                text=(
                    f"{spec.name} unavailable: {resolution.message}\n"
                    f"{_commands.command_recovery_hint(spec)}"
                ),
                exit_code=EXIT_CODES["usage_error"],
                presentation=spec.result_presentation,
                recovery=recovery,
                unavailable_reason=resolution.message,
            )
        )

    policy = _commands.headless_policy(spec.name)
    if policy == "flag-only":
        equivalent = _commands.headless_equivalent(spec.name)
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status="flag",
                text=(
                    f"{spec.name} has a dedicated flag in this surface: {equivalent}\n"
                    f"{_commands.command_recovery_hint(spec)}"
                ),
                exit_code=EXIT_CODES["usage_error"],
                presentation=spec.result_presentation,
                recovery=recovery,
                requires=equivalent,
            )
        )

    if spec.name == "/help":
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status="ok",
                text=headless_help(),
                exit_code=EXIT_CODES["success"],
                presentation=spec.result_presentation,
                recovery=recovery,
            )
        )

    handled = "unknown"
    rendered = ""
    try:
        with _captured_console() as capture:
            handled = _interactive._slash_command(
                raw, raw.lower(), last, effective_log_root, session_state
            )
        from cli import ui

        rendered = ui.strip_ansi(capture.get())
    except KeyboardInterrupt:
        # R2-18: `commands.command_failure` decides this, and it is the
        # same decision the REPL and the TUI record for a Ctrl+C
        # (`cancelled` / 130). The REPL re-raises after recording, so a
        # non-TTY run's own interrupt still has to be answered here.
        failure_status, failure_exit = _commands.command_failure(KeyboardInterrupt())
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status=failure_status,
                text=f"{spec.name} interrupted",
                exit_code=failure_exit,
                presentation=spec.result_presentation,
                recovery=recovery,
            )
        )
    except Exception as exc:
        from cli import ui as _ui

        # R2-18: the SHARED classification. It used to be this local
        # `classify_exit_code` call, which the two shells did not share —
        # so a dead Docker daemon was exit 3 for a script and exit 1
        # (a "retry the run" task failure) in both shells.
        failure_status, failure_exit = _commands.command_failure(exc)
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status=failure_status,
                text=_ui.strip_ansi(f"{spec.name} failed: {type(exc).__name__}: {exc}"),
                exit_code=failure_exit,
                presentation=spec.result_presentation,
                recovery=recovery,
            )
        )

    if handled == "unknown":
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status="unknown",
                text=f"unknown command: {name}",
                exit_code=EXIT_CODES["usage_error"],
                recovery=("edit-input", "return-safe-state"),
            )
        )
    recorded = session_state.get("last_command")
    if (
        isinstance(recorded, dict)
        and recorded.get("command") == spec.name
        and str(recorded.get("status") or "ok") != "ok"
        and int(recorded.get("exit_code") or 0) != 0
    ):
        return finish(
            HeadlessCommandResult(
                command=spec.name,
                args=args,
                status=str(recorded.get("status") or "failed"),
                text=rendered,
                exit_code=int(recorded.get("exit_code") or 1),
                presentation=spec.result_presentation,
                recovery=recovery,
            )
        )
    return finish(
        HeadlessCommandResult(
            command=spec.name,
            args=args,
            status="ok",
            text=rendered,
            exit_code=EXIT_CODES["success"],
            presentation=spec.result_presentation,
            recovery=recovery,
        )
    )
