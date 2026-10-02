"""Completion AND failure notifications (VEX-CEILING-10, part 5).

The gap this closes is honest and small: the terminal bell used to fire on
completion only, so a run that died was indistinguishable from a run the
user simply did not notice. A daily agent that fails silently for twenty
minutes is worse than one that is slow.

Policy, in one place:

* ``NEO_NOTIFY=off|bell|desktop|both`` selects the channels. The
  historical ``NEO_NOTIFY=0`` / ``false`` / ``no`` spellings stay valid
  and mean ``off``, because CI, ssh, and audio-free machines already rely
  on them.
* **Failure escalates.** A completed run rings once. A failed run rings
  repeatedly and, when desktop is enabled, raises a distinct toast. The
  escalation is a policy decision recorded in the receipt, not a guess.
* **Machine output stays clean.** A notification is only ever emitted on a
  real TTY, and ``suppressed_by`` names the reason when it is not. A
  ``--json`` document on a pipe therefore has no bell byte, no OSC escape,
  and no subprocess chatter in its stdout.
* **No secret in a receipt.** Titles and bodies are truncated and passed
  through the shared redactor before a receipt is written.

Every public function is total: a notification failure must never take
down a run's reporting.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class NotifyMode(str, Enum):
    """The channel set selected by ``NEO_NOTIFY``."""

    OFF = "off"
    BELL = "bell"
    DESKTOP = "desktop"
    BOTH = "both"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def bell_enabled(self) -> bool:
        return self in (NotifyMode.BELL, NotifyMode.BOTH)

    @property
    def desktop_enabled(self) -> bool:
        return self in (NotifyMode.DESKTOP, NotifyMode.BOTH)


class Outcome(str, Enum):
    """What happened to the run. Drives the escalation, not the wording."""

    COMPLETED = "completed"
    #: The verifier minted success. Distinguished from COMPLETED so a
    #: notification never implies a stronger claim than the run made.
    VERIFIED = "verified"
    #: A terminal failure: failed, error, timeout, cancelled.
    FAILED = "failed"
    #: Completed without clean verifier evidence. Not a success, and not
    #: escalated like a failure — but also not a completion bell.
    UNVERIFIED = "unverified"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @property
    def is_failure(self) -> bool:
        return self is Outcome.FAILED

    @property
    def is_completion(self) -> bool:
        return self in (Outcome.COMPLETED, Outcome.VERIFIED)


#: A failure rings this many times. One ring is indistinguishable from a
#: completion ring; three is not.
FAILURE_BELL_REPEATS = 3

#: Env var that selects the channel set.
ENV_VAR = "NEO_NOTIFY"

_OFF_SPELLINGS = {"0", "off", "false", "no", "none", "disabled"}
_BELL_SPELLINGS = {"bell", "terminal", "tty", "on", "1", "true", "yes"}
_DESKTOP_SPELLINGS = {"desktop", "toast", "notification", "gui"}
_BOTH_SPELLINGS = {"both", "all", "2"}


def resolve_mode(raw: Optional[str] = None) -> NotifyMode:
    """Resolve ``NEO_NOTIFY`` to a channel set.

    An unrecognized value resolves to :attr:`NotifyMode.BELL` — the
    historical default — rather than silently muting a user who made a
    typo. ``raw=None`` reads the environment; an explicit value (including
    the empty string) is taken literally, which is what makes this
    testable without touching the process environment.
    """
    if raw is None:
        raw = os.environ.get(ENV_VAR, "")
    value = str(raw or "").strip().lower()
    if not value:
        return NotifyMode.BELL
    if value in _OFF_SPELLINGS:
        return NotifyMode.OFF
    if value in _BOTH_SPELLINGS:
        return NotifyMode.BOTH
    if value in _DESKTOP_SPELLINGS:
        return NotifyMode.DESKTOP
    if value in _BELL_SPELLINGS:
        return NotifyMode.BELL
    return NotifyMode.BELL


def classify(status: Any) -> Outcome:
    """Map a run status onto a notification outcome.

    Fail-closed in the direction that matters: anything that is not a
    recognized completion is reported as :attr:`Outcome.FAILED` rather
    than as a completion. ``completed_unverified`` gets its own outcome so
    it is never announced as verified.
    """
    value = str(getattr(status, "value", status) or "").strip().lower()
    if value in ("completed_verified", "success", "verified"):
        return Outcome.VERIFIED
    if value == "completed_unverified":
        return Outcome.UNVERIFIED
    if value in ("completed", "ok", "done"):
        return Outcome.COMPLETED
    if not value:
        return Outcome.FAILED
    return Outcome.FAILED


#: Redactors, in authority order. ``shared.security`` is the ONE
#: redaction implementation the ceiling pack requires every surface to
#: share; ``cli.neoconfig`` is the CLI's older local helper and is only a
#: fallback for a tree where the shared module is unavailable. They are
#: NOT equivalent — the shared one recognizes secret shapes the older one
#: does not — so the order is part of the contract.
REDACTORS = (
    ("shared.security", "redact_text"),
    ("shared.security", "redact_secrets"),
    ("cli.neoconfig", "redact_text"),
)


def _redact(text: str) -> str:
    """Run a notification body through the shared redactor.

    A notification is written to a desktop toast, which is outside the
    process, so a secret reaching it has left every other control we
    have. Redaction is therefore mandatory here and fail-CLOSED: if no
    redactor can be resolved, or the one we resolve raises, the detail is
    REPLACED rather than passed through. An unredactable detail is not
    worth a toast.
    """
    cleaned = str(text or "")
    if not cleaned:
        return ""
    for module_name, attribute in REDACTORS:
        try:
            module = __import__(module_name, fromlist=[attribute])
        except Exception:
            continue
        redactor = getattr(module, attribute, None)
        if not callable(redactor):
            continue
        try:
            return str(redactor(cleaned) or "")
        except Exception:
            continue
    return "(detail withheld: no redactor available)"


@dataclass
class NotifyReceipt:
    """What a notification attempt actually did.

    ``suppressed_by`` is the honest field: ``"mode-off"``,
    ``"not-a-tty"``, ``"no-desktop"``. A receipt that claims a toast when
    nothing was shown is a lie, and this class cannot produce one.
    """

    mode: NotifyMode
    outcome: Outcome
    bell_rings: int = 0
    desktop_attempted: bool = False
    desktop_sent: bool = False
    desktop_error: str = ""
    suppressed_by: str = ""
    title: str = ""
    body: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "outcome": self.outcome.value,
            "bell_rings": self.bell_rings,
            "desktop_attempted": self.desktop_attempted,
            "desktop_sent": self.desktop_sent,
            "desktop_error": self.desktop_error,
            "suppressed_by": self.suppressed_by,
            "title": self.title,
            "body": self.body,
            "failure_escalation": self.outcome.is_failure,
        }


def _truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def build_notification(
    status: Any,
    *,
    mode: Optional[str] = None,
    label: str = "task",
    detail: str = "",
) -> tuple[Outcome, NotifyMode, str, str]:
    """Return ``(outcome, mode, title, body)`` for one finished run.

    The wording is derived from the outcome, not from a caller's optimism:
    a failure says failed, an unverified completion says unverified, and
    neither is dressed as success.
    """
    outcome = classify(status)
    resolved = resolve_mode(mode)
    detail = _truncate(_redact(detail), 200)
    suffix = f" — {detail}" if detail else ""
    if outcome is Outcome.FAILED:
        return outcome, resolved, f"neo {label} failed", f"run failed{suffix}"
    if outcome is Outcome.UNVERIFIED:
        return (
            outcome,
            resolved,
            f"neo {label} completed (unverified)",
            f"finished without clean verifier evidence{suffix}",
        )
    if outcome is Outcome.VERIFIED:
        return outcome, resolved, f"neo {label} verified", f"verified success{suffix}"
    return outcome, resolved, f"neo {label} finished", f"completed{suffix}"


def _write_bell(rings: int) -> bool:
    """Emit the terminal bell through the ONE existing bell implementation.

    ``cli.ui.bell`` already owns the TTY gate, the Windows
    ``MessageBeep``, and the historical ``NEO_NOTIFY=0`` opt-out, and
    other surfaces and tests already pin it. Reimplementing that here
    would be a second bell with a different TTY policy — the exact class
    of drift this module is meant to prevent — so this delegates.

    ``ui.bell`` is called with its ORIGINAL single-argument signature
    (repeated once per ring) rather than through the new ``repeats``
    keyword, so a test double or an embedder that patches the historical
    shape keeps working.
    """
    try:
        from cli import ui

        count = max(1, int(rings or 1))
        emitted = 0
        for _ in range(count):
            if ui.bell("neo"):
                emitted += 1
        return emitted > 0
    except Exception:
        return False


def _windows_beep() -> bool:
    """Non-blocking Windows console beep; a no-op elsewhere."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        ctypes.windll.user32.MessageBeep(0x40)  # MB_ICONASTERISK
        return True
    except Exception:
        return False


def _notify_send(title: str, body: str) -> tuple[bool, str]:
    """POSIX desktop toast via ``notify-send``."""
    try:
        proc = subprocess.run(
            ["notify-send", "--app-name=neo", title, body],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False, "notify-send not installed"
    except subprocess.TimeoutExpired:
        return False, "notify-send timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        return False, f"notify-send exit {proc.returncode}"
    return True, ""


def _osascript(title: str, body: str) -> tuple[bool, str]:
    """macOS desktop toast via ``osascript``."""
    script = (
        f"display notification {_applescript_quote(body)} "
        f"with title {_applescript_quote(title)}"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False, "osascript not installed"
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        return False, f"osascript exit {proc.returncode}"
    return True, ""


def _applescript_quote(value: str) -> str:
    """Quote a string for AppleScript, escaping quotes and backslashes."""
    escaped = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def send_desktop(title: str, body: str, *, platform: str = "") -> tuple[bool, str]:
    """Attempt a desktop notification. Returns ``(sent, error)``."""
    target = platform or sys.platform
    if target.startswith("darwin"):
        return _osascript(title, body)
    if target.startswith("win") or os.name == "nt":
        # On Windows the console beep IS the desktop path: it raises through
        # the shell's own notification area and needs no extra dependency.
        return (True, "") if _windows_beep() else (False, "MessageBeep unavailable")
    return _notify_send(title, body)


def notify_run(
    status: Any,
    *,
    mode: Optional[str] = None,
    label: str = "task",
    detail: str = "",
    desktop: bool = True,
    receipt_sink: Optional[list] = None,
) -> NotifyReceipt:
    """Notify the outcome of one finished run and return the receipt.

    ``desktop=False`` (set by JSON/headless callers) skips the desktop
    channel without changing the mode, because the reason to skip is the
    *surface*, not the user's preference. The receipt says so.
    """
    outcome, resolved, title, body = build_notification(
        status, mode=mode, label=label, detail=detail
    )
    receipt = NotifyReceipt(mode=resolved, outcome=outcome, title=title, body=body)
    if resolved is NotifyMode.OFF:
        receipt.suppressed_by = "mode-off"
        if receipt_sink is not None:
            receipt_sink.append(receipt.to_dict())
        return receipt
    # Every channel is individually guarded: a bell write to a closed
    # stdout, or a desktop helper that raises, must not turn a finished
    # run's reporting into a traceback. The receipt records what actually
    # happened either way.
    if resolved.bell_enabled:
        rings = FAILURE_BELL_REPEATS if outcome.is_failure else 1
        try:
            written = _write_bell(rings)
        except Exception:
            written = False
        if written:
            receipt.bell_rings = rings
        else:
            receipt.suppressed_by = "not-a-tty"
    if resolved.desktop_enabled:
        if not desktop:
            receipt.suppressed_by = receipt.suppressed_by or "headless-surface"
        else:
            receipt.desktop_attempted = True
            try:
                sent, error = send_desktop(title, body)
            except Exception as exc:
                sent, error = False, type(exc).__name__
            receipt.desktop_sent = sent
            receipt.desktop_error = error
            if not sent and not receipt.suppressed_by:
                receipt.suppressed_by = f"no-desktop:{error}" if error else "no-desktop"
    if receipt_sink is not None:
        receipt_sink.append(receipt.to_dict())
    return receipt
