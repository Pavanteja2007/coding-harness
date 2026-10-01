"""One-shot headless agent surface: ``vex -p "sentence"`` and ``vex -``.

``cli/command_exec.py`` is the headless surface for SLASH COMMANDS
(``vex run "/diff"``). This module is the headless surface for AGENT WORK -
the thing a script or a CI job actually wants: give me a sentence, give me
a verified answer, give me an exit code.

Four properties this module exists to guarantee. Each one is a real way a
headless agent surface usually lies, so each is enforced by a named
function rather than by convention.

1. **One JSON envelope, derived from the journal.** ``result_envelope`` is
   the only producer of the headless document, and it is built from
   ``cli.runview.read_live_projection`` - the SAME journal-derived
   projection the TUI renders and ``runview.status_label`` styles. A
   script and a human therefore cannot disagree about what happened, and
   the contract does not drift when the projection gains a field.

2. **The verifier still mints success.** The envelope carries
   ``runview.effective_terminal_status`` and ``runview.verification_state``
   verbatim. ``completed_unverified`` is a first-class status that maps to a
   NON-ZERO exit code; nothing in this module can promote a model's finish
   claim into verified success, because nothing in this module decides
   success at all - it only reports what the verifier already decided.

3. **One session-id grammar.** ``--session-id`` accepts a conversation id
   from ``cli.session`` (the same ``sess-<8 hex>`` the TUI and REPL use),
   so a headless turn can continue a conversation a human started and vice
   versa. With no id supplied the caller gets a fresh one; a supplied id
   that names no conversation is CREATED rather than silently ignored, and
   the envelope says so via ``session_created``.

4. **Explicit policies, not faked interactivity.** ``HEADLESS_MODES`` is the
   declared policy for each supported entry point, including the three
   that ``cli.commands`` refuses in the bare ``vex run`` surface
   (``/plan``, ``/review``, ``/ask``). Every mode states whether it is
   read-only and whether it can mint verification. A read-only mode can
   never report a verification state other than ``not_run``, and that is
   asserted rather than hoped for.

Exit codes come from ``cli.exit_codes`` (0 success, 1 task failure,
2 usage, 3 environment, 4 model/network, 130 interrupted). This module
never invents one.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from cli.exit_codes import EXIT_CODES

__all__ = [
    "ENVELOPE_SCHEMA",
    "HEADLESS_MODES",
    "HeadlessMode",
    "HeadlessOutcome",
    "compose_prompt",
    "read_piped_context",
    "render_human",
    "resolve_mode",
    "result_envelope",
    "run_headless",
]

#: Schema tag carried by every headless document. Bump ONLY with a
#: compatibility story; scripts key on it.
ENVELOPE_SCHEMA = "vex.headless/1"

#: Cap on piped context. A pipe can be arbitrarily large; the agent prompt
#: cannot. Truncation is explicit (``stdin_truncated``) rather than silent.
MAX_PIPED_CHARS = 200_000

#: Prefix used when a pipe is folded into a prompt.
PIPED_CONTEXT_HEADER = "Context read from standard input:"


@dataclass(frozen=True)
class HeadlessMode:
    """The declared policy for one headless entry point.

    Attributes:
        name: the mode slug carried in the envelope and the journal.
        command: the slash command that selects it (empty for plain text).
        read_only: True when the mode cannot edit the repository.
        verifies: True only when the mode can produce verifier evidence.
        exit_nonzero_on_unverified: True when a completed-but-unverified run
            must not exit 0. A read-only answer legitimately cannot verify,
            so it sets this False and reports ``not_run``.
        description: the one line ``vex --help`` and ``/help`` show.
    """

    name: str
    command: str
    read_only: bool
    verifies: bool
    exit_nonzero_on_unverified: bool
    description: str


HEADLESS_MODES: Dict[str, HeadlessMode] = {
    "agent_task": HeadlessMode(
        name="agent_task",
        command="",
        read_only=False,
        verifies=True,
        exit_nonzero_on_unverified=True,
        description="default agent work on the live repository (may edit)",
    ),
    "plan": HeadlessMode(
        name="plan",
        command="/plan",
        read_only=True,
        verifies=False,
        exit_nonzero_on_unverified=False,
        description="plan only; no edits, no verification minted",
    ),
    "review": HeadlessMode(
        name="review",
        command="/review",
        read_only=True,
        verifies=False,
        exit_nonzero_on_unverified=False,
        description="read-only review of the working tree or a ref",
    ),
    "ask": HeadlessMode(
        name="ask",
        command="/ask",
        read_only=True,
        verifies=False,
        exit_nonzero_on_unverified=False,
        description="read-only repository question; no edits",
    ),
    "context": HeadlessMode(
        name="context",
        command="",
        read_only=True,
        verifies=False,
        exit_nonzero_on_unverified=False,
        description="ingest piped context only; no model call",
    ),
}


@dataclass
class HeadlessOutcome:
    """One headless turn: the envelope plus the rendered text, ready to print."""

    envelope: Dict[str, Any]
    text: str = ""
    exit_code: int = EXIT_CODES["success"]

    @property
    def task_id(self) -> str:
        """The run's task id (empty when no run happened)."""
        return str(self.envelope.get("task_id") or "")

    @property
    def session_id(self) -> str:
        """The conversation id this turn belongs to."""
        return str(self.envelope.get("session_id") or "")

    @property
    def ok(self) -> bool:
        """Whether the process exit code is the success code."""
        return self.exit_code == EXIT_CODES["success"]

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-serializable envelope."""
        return dict(self.envelope)


def resolve_mode(prompt: str) -> Tuple[HeadlessMode, str]:
    """Resolve the mode and the remaining text for one headless prompt.

    A leading ``/plan``, ``/review`` or ``/ask`` selects that mode and is
    stripped from the text. Anything else is ``agent_task``.

    A read-only mode with EMPTY remaining text is a usage error the caller
    reports; this function still returns the mode so the error can name the
    declared policy rather than guessing.

    Assumes nothing about the repository, the provider, or the terminal.
    """
    raw = str(prompt or "").strip()
    if raw.startswith("/"):
        head = raw.split(None, 1)[0].lower()
        rest = raw[len(head) :].strip()
        mode = HEADLESS_MODES.get(
            {"/plan": "plan", "/review": "review", "/ask": "ask"}.get(head, ""),
            HEADLESS_MODES["agent_task"],
        )
        if mode.command:
            return mode, rest
    return HEADLESS_MODES["agent_task"], raw


def read_piped_context(
    stream: Any = None,
    *,
    max_chars: int = MAX_PIPED_CHARS,
) -> Tuple[str, bool]:
    """Read piped standard input and report whether it was truncated.

    Returns ``(text, truncated)``. A pipe that is not readable (no data, a
    closed handle, a decode error mid-stream) yields ``("", False)`` rather
    than raising: a headless agent must not die because a redirect was
    empty. Decoding is ``errors="replace"`` so a stray non-UTF-8 byte in a
    log dump cannot abort the run.

    Truncation keeps the HEAD of the stream. A pipe is a prefix of work in
    practice (a build log, a diff, a file), and the tail of a truncated
    context is where truncation notices live.
    """
    handle = stream if stream is not None else sys.stdin
    try:
        if handle is None or not hasattr(handle, "read"):
            return "", False
        raw = handle.read()
    except Exception:
        return "", False
    if raw is None:
        return "", False
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            return "", False
    else:
        text = str(raw)
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def compose_prompt(prompt: str, context: str = "") -> str:
    """Fold piped context and the typed prompt into one request.

    A prompt with no context is returned unchanged (minus surrounding
    whitespace) so a plain ``-p`` run produces byte-identical request text
    to a TUI turn of the same sentence. Context is labelled explicitly so
    the model can tell supplied material from instructions - the ceiling
    invariant that untrusted content is labeled before it enters trusted
    context.
    """
    body = str(prompt or "").strip()
    piped = str(context or "").strip()
    if not piped:
        return body
    if not body:
        return piped
    return f"{PIPED_CONTEXT_HEADER}\n\n{piped}\n\n---\n\nRequest: {body}"


def _projection(task_dir: Optional[Path], mode: str) -> Dict[str, Any]:
    """Read the journal-derived projection for one task directory."""
    if task_dir is None:
        return {}
    try:
        from cli.runview import read_live_projection

        return read_live_projection(Path(task_dir), mode=mode)
    except Exception:
        return {}


def _task_dir(log_root: Any, task_id: str) -> Optional[Path]:
    """Resolve one run directory through the shared traversal guard."""
    if not task_id:
        return None
    try:
        from cli import interactive

        return interactive._safe_task_dir(str(task_id), Path(log_root))
    except Exception:
        return None


def result_envelope(
    *,
    mode: HeadlessMode,
    session_id: str,
    session_created: bool,
    task_id: str = "",
    log_root: Any = None,
    status: str = "",
    error: str = "",
    exit_code: int = EXIT_CODES["success"],
    answer: str = "",
    diff: str = "",
    model: str = "",
    provider: str = "",
    elapsed_s: float = 0.0,
    cost_usd: float = 0.0,
    files: Sequence[str] = (),
    stdin_truncated: bool = False,
    stdin_chars: int = 0,
) -> Dict[str, Any]:
    """Build THE headless JSON document.

    Every headless agent turn - ``-p``, ``-``, a read-only mode, a refused
    mode - is rendered by this one function, and the status/verification
    fields are read from the journal projection rather than from whatever
    the engine returned. That is what makes the document equal to what the
    TUI shows.

    The fail-closed rules live here:

    - a read-only mode reports ``verification_state: not_run`` and can
      never report a verified status, whatever the engine claimed;
    - ``completed_unverified`` never yields exit 0 for a mode that can
      verify (``exit_nonzero_on_unverified``);
    - a run with no journal directory reports ``verification_state:
      not_run`` and status ``unknown`` rather than inventing an outcome.
    """
    from cli.runview import (
        effective_terminal_status,
        status_is_completed,
        status_is_verified,
        verification_state,
    )

    task_dir = _task_dir(log_root, task_id) if log_root is not None else None
    projection = _projection(task_dir, mode.name)
    evidence = projection.get("verification_evidence") or []
    if not evidence and projection.get("latest_verification"):
        evidence = [projection["latest_verification"]]

    if mode.read_only:
        # A read-only mode has no verifier in its path. Whatever the engine
        # said, the honest statement is "not verified because never asked".
        effective = "completed" if status_is_completed(
            effective_terminal_status(status or projection.get("status"), [])
        ) else effective_terminal_status(status or projection.get("status"), [])
        vstate = "not_run"
    else:
        effective = effective_terminal_status(
            status or projection.get("status"), evidence
        )
        vstate = verification_state(evidence, effective)

    verified = status_is_verified(effective)
    completed = status_is_completed(effective)
    # Exit code derivation. A caller-supplied non-success code (2 usage, 3
    # environment, 4 model, 130 interrupted) is authoritative and never
    # overwritten; only a caller's "success" is re-derived from the facts,
    # because the whole point of this surface is that a script's exit code
    # cannot disagree with the run's outcome.
    #
    # The four rules, and the failure each one prevents:
    #   read-only + completed  -> 0  (an answer legitimately cannot verify)
    #   read-only + NOT done   -> 1  (a refused/failed answer is not success)
    #   verified               -> 0
    #   completed, unverified  -> 1  (completed_unverified is never success)
    #   anything else          -> 1  (failed/timeout/unknown/blocked)
    if exit_code == EXIT_CODES["success"]:
        if effective == "cancelled":
            exit_code = EXIT_CODES["interrupted"]
        elif mode.read_only:
            exit_code = (
                EXIT_CODES["success"] if completed else EXIT_CODES["task_failure"]
            )
        elif verified or (completed and not mode.exit_nonzero_on_unverified):
            exit_code = EXIT_CODES["success"]
        else:
            exit_code = EXIT_CODES["task_failure"]

    changed = list(files) or [
        str(item) for item in (projection.get("changed_files") or projection.get("files") or [])
    ]
    return {
        "schema": ENVELOPE_SCHEMA,
        "mode": mode.name,
        "read_only": mode.read_only,
        "verifies": mode.verifies,
        "session_id": str(session_id or ""),
        "session_created": bool(session_created),
        "task_id": str(task_id or ""),
        "status": effective or "unknown",
        "verified": verified,
        "completed": completed,
        "verification_state": vstate,
        "exit_code": int(exit_code),
        "exit_reason": _exit_reason(int(exit_code), effective, verified),
        "answer": str(answer or ""),
        "diff": str(diff or ""),
        "files": changed,
        "model": str(model or ""),
        "provider": str(provider or ""),
        "elapsed_s": round(float(elapsed_s or 0.0), 3),
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "log_root": str(log_root or ""),
        "trace_path": str(task_dir / "trace.jsonl") if task_dir is not None else "",
        "stdin_chars": int(stdin_chars or 0),
        "stdin_truncated": bool(stdin_truncated),
        "error": str(error or ""),
    }


def _exit_reason(exit_code: int, status: str, verified: bool) -> str:
    """Name the exit code's meaning from the shared exit-code contract."""
    from cli.exit_codes import reason_for

    if exit_code == EXIT_CODES["success"] and not verified and status not in (
        "unknown",
        "",
    ):
        return "success (read-only mode; no verification was requested)"
    return reason_for(int(exit_code))


def render_human(envelope: Mapping[str, Any]) -> str:
    """Render the envelope for a person, from the envelope only.

    Deliberately reads nothing but the document: a human reading the text
    and a script reading the JSON are looking at the same facts, so the two
    renderers cannot drift. The verifier verdict is spelled out and
    ``completed_unverified`` is never dressed as success.
    """
    from cli import ui
    from cli.runview import status_label

    data = dict(envelope or {})
    status = str(data.get("status") or "unknown")
    verified = bool(data.get("verified"))
    completed = bool(data.get("completed"))
    label = status_label(status) if status != "unknown" else "UNKNOWN"
    if verified:
        style, mark = "vex.ok", ui.GLYPHS["ok"]
    elif completed:
        style, mark = "vex.warn", ui.GLYPHS["wait"]
    else:
        style, mark = "vex.error", ui.GLYPHS["fail"]
    lines = [
        f"[{style}]{mark} {label}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{data.get('mode', 'agent_task')}[/]"
        + (" [vex.muted](read-only)[/]" if data.get("read_only") else ""),
        f"[vex.muted]task[/] [{ui.TEXT_PRIMARY}]{data.get('task_id', '') or '-'}[/]"
        f" [vex.muted]{ui.DOT}[/] [vex.muted]session[/] "
        f"[{ui.TEXT_PRIMARY}]{data.get('session_id', '') or '-'}[/]",
        f"[vex.muted]verification[/] [{ui.TEXT_PRIMARY}]"
        f"{data.get('verification_state', 'not_run')}[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.muted]{data.get('elapsed_s', 0)}s[/] [vex.muted]{ui.DOT}[/] "
        f"[vex.accent2]{ui.fmt_cost(float(data.get('cost_usd') or 0.0))}[/]",
    ]
    if data.get("error"):
        lines.append(f"[vex.error]{ui.strip_ansi(str(data['error']))}[/]")
    answer = str(data.get("answer") or "")
    if answer:
        lines.append("")
        lines.append(ui.strip_ansi(answer))
    diff = str(data.get("diff") or "")
    if diff:
        lines.append("")
        lines.append("[vex.muted]diff:[/]")
        lines.append(ui.strip_ansi(diff))
    elif data.get("files"):
        lines.append(
            f"[vex.muted]files[/] [{ui.TEXT_PRIMARY}]"
            f"{', '.join(str(f) for f in data['files'][:10])}[/]"
        )
    if data.get("trace_path"):
        lines.append(
            f"[vex.muted]trace[/] [{ui.TEXT_PRIMARY}]{data['trace_path']}[/]"
        )
    return "\n".join(lines)


def _resolve_session(
    log_root: Path,
    repo: Optional[Path],
    session_id: str,
) -> Tuple[Dict[str, Any], bool, Any]:
    """Load or create the conversation this turn belongs to, UNDER THE GUARD.

    Returns ``(session, created, stack)``. A headless turn writes real turns
    into the real conversation journal - it is the SAME conversation a TUI
    session reads - so ``vex -p`` and the shells share history rather than
    keeping two views of one run. That also makes a headless turn a WRITER,
    and two writers on one worktree is how work is lost, so the
    single-writer guard is taken here and held until the caller closes the
    stack.

    **This was the last unmounted writer.** `cli.session.open_session` is
    unit-proven and was called by nothing in the product, which meant two
    `vex` processes mutating one repository were NOT refused. It is a
    `cli.session` context manager entered through `ExitStack` rather than
    re-implemented, so the guard's own rules - dead-owner takeover, the
    `refuse`/`warn` mode, the unreadable-lock refusal - stay in one place.

    Raises `shared.instance_guard.ConcurrentInstanceError` when a live peer
    holds the repository. It is NOT swallowed here: the previous
    `except Exception: return {}, True` would have turned a refusal into an
    empty session and carried on, which is a fail-OPEN guard and the exact
    opposite of what it is for.
    """
    from contextlib import ExitStack

    from cli import session as _session

    stack = ExitStack()
    try:
        if session_id:
            path = _session._session_path(log_root, session_id)
            created = not Path(path).exists()
        else:
            created = True
        data = stack.enter_context(
            _session.open_session(
                log_root,
                repo,
                session_id or None,
                command="vex (headless)",
            )
        )
    except BaseException:
        stack.close()
        raise
    return dict(data or {}), created, stack


def _save_session(log_root: Path, session: Mapping[str, Any]) -> None:
    """Persist the conversation; a failure is never fatal to the run."""
    try:
        from cli import session as _session

        _session.save_session(log_root, dict(session))
    except Exception:
        return


def _effective_setting(
    state: Mapping[str, Any],
    file_config: Mapping[str, Any],
    key: str,
    env_name: str,
) -> str:
    """Report which model/provider a headless turn actually ran with.

    Same precedence the engines themselves use (env over settings), read
    from the same places. An empty value is reported as empty rather than
    filled in with a default: a document that names a model the run did not
    use is worse than one that names none.
    """
    import os

    return str(
        os.environ.get(env_name) or state.get(key) or file_config.get(key) or ""
    )


def run_headless(
    prompt: str,
    *,
    repo: Optional[Path] = None,
    log_root: Optional[Path] = None,
    session_id: str = "",
    context: str = "",
    stdin_truncated: bool = False,
    stdin_chars: int = 0,
    as_json: bool = False,
) -> HeadlessOutcome:
    """Run ONE headless agent turn and return the canonical outcome.

    Chooses the declared mode from the prompt, folds piped context in, runs
    the SAME engine the interactive shells run
    (``cli.interactive._run_one_agent`` / ``_run_one_question``), records
    the turn in the SAME conversation journal, and renders the result
    through ``result_envelope``.

    Never raises: an environment, model, or usage failure becomes an
    envelope with the matching exit code, because a headless caller gets
    a document and a number, not a traceback.
    """
    from rich.markup import escape

    from cli import ui

    repo_dir = Path(repo) if repo else Path.cwd()
    try:
        if not repo_dir.is_dir():
            return _usage_outcome(
                prompt,
                repo_dir,
                log_root,
                f"not a directory: {repo_dir}",
                session_id=session_id,
                context=context,
                stdin_truncated=stdin_truncated,
                stdin_chars=stdin_chars,
                as_json=as_json,
            )
    except OSError as exc:
        return _usage_outcome(
            prompt,
            repo_dir,
            log_root,
            f"repository is unreadable: {exc}",
            session_id=session_id,
            as_json=as_json,
        )

    mode, body = resolve_mode(prompt)
    if mode.read_only and not body.strip() and mode.name != "context":
        return _usage_outcome(
            prompt,
            repo_dir,
            log_root,
            f"{mode.command} needs a request headlessly, e.g. "
            f'vex -p "{mode.command} add retries to the fetch call"',
            session_id=session_id,
            as_json=as_json,
        )
    if not body.strip() and not str(context or "").strip():
        return _usage_outcome(
            prompt,
            repo_dir,
            log_root,
            'nothing to do: pass a sentence, e.g. vex -p "explain cli/main.py" '
            'or pipe context with vex -',
            session_id=session_id,
            as_json=as_json,
        )

    effective_root = Path(log_root) if log_root else _default_log_root()
    request = compose_prompt(body, context) if mode.name != "context" else ""

    state: Dict[str, Any] = {"quiet": True, "repo": str(repo_dir)}
    file_config: Dict[str, Any] = {}
    try:
        from cli.vexconfig import merged_settings

        merged = merged_settings(start=str(repo_dir))
        if isinstance(merged, dict):
            file_config = merged
    except Exception:
        file_config = {}
    state["file_config"] = file_config
    for key in ("model", "provider", "log_root"):
        if file_config.get(key):
            state[key] = file_config[key]

    try:
        session, created, _guard_stack = _resolve_session(
            effective_root, repo_dir, session_id
        )
    except Exception as exc:
        # A refusal is an ENVIRONMENT fact, not a usage error and not a task
        # failure: the command is well-formed, the machine is not free. Exit
        # 3 (`environment_error`) is the one the shared vocabulary already
        # has, and choosing it is a UX decision this round makes rather than
        # defers - `cli/UNMOUNTED.md` records it as one.
        from shared.instance_guard import ConcurrentInstanceError

        if not isinstance(exc, ConcurrentInstanceError):
            raise
        lines = list(exc.lines())
        envelope = result_envelope(
            mode=mode,
            session_id=session_id,
            session_created=False,
            task_id="",
            log_root=effective_root,
            status="refused",
            error="concurrent_instance",
            exit_code=EXIT_CODES["environment_error"],
            answer="; ".join(line.strip() for line in lines),
        )
        for line in lines:
            ui.err_console().print(f"[vex.error]{escape(str(line))}[/]")
        return HeadlessOutcome(
            envelope=envelope,
            text=render_human(envelope),
            exit_code=EXIT_CODES["environment_error"],
        )
    resolved_session_id = str(session.get("session_id") or session_id or "")

    try:
        return _run_guarded_headless(
            mode=mode,
            effective_root=effective_root,
            repo_dir=repo_dir,
            session_id=session_id,
            resolved_session_id=resolved_session_id,
            session=session,
            created=created,
            state=state,
            file_config=file_config,
            request=request,
            context=context,
            stdin_truncated=stdin_truncated,
            stdin_chars=stdin_chars,
            as_json=as_json,
        )
    finally:
        _guard_stack.close()


def _run_guarded_headless(
    *,
    mode: Any,
    effective_root: Path,
    repo_dir: Optional[Path],
    session_id: str,
    resolved_session_id: str,
    session: Dict[str, Any],
    created: bool,
    state: Dict[str, Any],
    file_config: Dict[str, Any],
    request: str,
    context: str,
    stdin_truncated: bool,
    stdin_chars: int,
    as_json: bool = False,
) -> HeadlessOutcome:
    """The body of a headless turn, with the guard already held.

    Split out so the lease is released in ONE place - a `finally` around the
    whole run rather than at four return sites - and so a refusal can be
    returned before any of the body is entered.
    """
    from cli import ui
    from cli.exit_codes import classify_exit_code

    started = time.time()
    outcome: Optional[Dict[str, Any]] = None
    error = ""
    exit_code = EXIT_CODES["success"]
    if mode.name == "context":
        # Explicit no-model ingest: the piped context becomes a conversation
        # turn and nothing else. It cannot mint success because it did
        # nothing that could succeed.
        envelope = result_envelope(
            mode=mode,
            session_id=resolved_session_id,
            session_created=created,
            task_id="",
            log_root=effective_root,
            status="completed",
            exit_code=EXIT_CODES["success"],
            answer="context accepted; no model call was made",
            stdin_truncated=stdin_truncated,
            stdin_chars=stdin_chars,
        )
        _record_turn(
            effective_root, session, "user", str(context or ""), resolved_session_id
        )
        _save_session(effective_root, session)
        return HeadlessOutcome(envelope=envelope, text=render_human(envelope), exit_code=0)

    _record_turn(effective_root, session, "user", request, resolved_session_id)
    if resolved_session_id:
        state["conversation"] = {"session_id": resolved_session_id}

    try:
        if mode.read_only:
            from cli.interactive import _run_one_question

            with ui.console().capture():
                outcome = _run_one_question(
                    request, repo_dir, state, effective_root, file_config
                )
        else:
            from cli.interactive import _run_one_agent

            with ui.console().capture():
                outcome = _run_one_agent(
                    request, repo_dir, state, effective_root, file_config
                )
    except KeyboardInterrupt:
        exit_code = EXIT_CODES["interrupted"]
        error = "interrupted"
    except BaseException as exc:
        exit_code = classify_exit_code(exc)
        error = f"{type(exc).__name__}: {exc}"

    elapsed = time.time() - started
    task_id = str((outcome or {}).get("task_id") or "")
    if outcome is None and not error:
        error = "the run produced no result"
    if outcome is not None:
        _record_turn(
            effective_root,
            session,
            "assistant",
            str(outcome.get("answer") or ""),
            resolved_session_id,
            task_id=task_id,
        )
    _save_session(effective_root, session)

    envelope = result_envelope(
        mode=mode,
        session_id=resolved_session_id,
        session_created=created,
        task_id=task_id,
        log_root=effective_root,
        status=str((outcome or {}).get("status") or ""),
        error=error,
        exit_code=exit_code,
        answer=str((outcome or {}).get("answer") or ""),
        diff=str((outcome or {}).get("diff") or ""),
        model=_effective_setting(state, file_config, "model", "VEX_MODEL"),
        provider=_effective_setting(state, file_config, "provider", "VEX_PROVIDER"),
        elapsed_s=float((outcome or {}).get("elapsed_s") or elapsed),
        cost_usd=float((outcome or {}).get("cost_usd") or 0.0),
        files=list((outcome or {}).get("files_touched") or []),
        stdin_truncated=stdin_truncated,
        stdin_chars=stdin_chars,
    )
    final = int(envelope.get("exit_code") or exit_code)
    if as_json:
        return HeadlessOutcome(
            envelope=envelope, text=json.dumps(envelope, indent=2), exit_code=final
        )
    return HeadlessOutcome(envelope=envelope, text=render_human(envelope), exit_code=final)


def _record_turn(
    log_root: Path,
    session: Dict[str, Any],
    role: str,
    text: str,
    session_id: str,
    task_id: str = "",
) -> None:
    """Append one turn to the shared conversation journal."""
    if not session:
        return
    try:
        from cli import session as _session

        _session.append_turn(
            session,
            role,
            text,
            task_id or None,
            run_id=session_id or None,
            trace_path=str(Path(log_root) / task_id / "trace.jsonl")
            if task_id
            else None,
        )
    except Exception:
        return


def _default_log_root() -> Path:
    """The harness-owned, repo-outside artifact root (ceiling-03 contract)."""
    try:
        from cli.session import resolve_artifact_root

        return Path(resolve_artifact_root(None, None)["log_root"])
    except Exception:
        try:
            from memory.paths import default_logs_dir

            return Path(default_logs_dir())
        except Exception:
            return Path("logs")


def _usage_outcome(
    prompt: str,
    repo: Path,
    log_root: Optional[Path],
    message: str,
    *,
    session_id: str = "",
    context: str = "",
    stdin_truncated: bool = False,
    stdin_chars: int = 0,
    as_json: bool = False,
) -> HeadlessOutcome:
    """Build a usage-error outcome (exit 2) in the same envelope shape.

    A usage error uses the SAME document as a run so a script parses one
    shape whether the sentence was accepted or not - including under
    ``--json``, where a human-rendered refusal on stdout would be a parse
    error in the caller.
    """
    mode, _ = resolve_mode(prompt)
    envelope = result_envelope(
        mode=mode,
        session_id=session_id,
        session_created=False,
        task_id="",
        log_root=log_root,
        status="refused",
        error=message,
        exit_code=EXIT_CODES["usage_error"],
        stdin_truncated=stdin_truncated,
        stdin_chars=stdin_chars,
    )
    envelope["exit_reason"] = "usage"
    return HeadlessOutcome(
        envelope=envelope,
        text=json.dumps(envelope, indent=2)
        if as_json
        else render_human(envelope),
        exit_code=EXIT_CODES["usage_error"],
    )
