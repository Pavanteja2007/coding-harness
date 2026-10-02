"""The ONE fail-closed redaction boundary for every harness journal write.

**This module does not implement redaction.** ``shared.security.redact_text``
and ``shared.security.redact_secrets`` are the single authority (T5 owns them,
and re-implementing a secret pattern here is how two implementations end up
disagreeing about the same credential). What this module owns is the part the
authority cannot: the boundary that decides *what happens when the redactor is
not available or does not answer*.

## The three properties this exists for

1. **Fail closed.** If the shared redactor raises, or answers ``None`` for a
   value that was not ``None``, the value is REPLACED with
   ``(detail withheld: <reason>)``. It is never passed through raw. This is the
   same behaviour ``cli.notify._redact`` already has, and it is the same
   behaviour for the same reason: a notification is the same kind of boundary
   as a journal row - once the value has left the process, every control we
   still had is gone. **A withheld detail must never be readable as an empty
   one**, so the reason is in the text and the counters below are readable by a
   test.

2. **Bounded input.** A minified asset, a base64 blob or a single-character
   run can be enormous, and this boundary sits on the path of *every* journal
   write. ``REDACTION_SCAN_SPAN_CAP``-style pathologies are handled inside the
   authority (measured: ``redact_text("y"*40000)`` is 0.025s), but an unbounded
   caller still hands the authority a payload no reader will ever look at. Every
   string is therefore capped at :data:`JOURNAL_TEXT_CAP` characters BEFORE the
   redactor sees it, with an explicit marker naming the original length. The
   cap is a value, not a truncation to nothing: the head is kept, because the
   head is where a secret's prefix usually is and a cap that discarded it
   would be a cap that changed what the redactor could find.

3. **One helper, not scattered calls.** ``harness/trace.py`` and
   ``harness/agent_kernel/events.py`` are the two journal authorities in this
   module and both call :func:`redact_for_journal`. Nothing else in ``harness/``
   calls ``shared.security.redact_*`` on a journal path.

## What is deliberately NOT here

* No secret pattern, no allow-list, no placeholder of our own. The authority
  owns all three.
* No swallowing of the authority's own verdict. If the authority decides a value
  is safe, this module says so and passes it through; it never second-guesses.
* No presentation-time redaction. See ``harness/AGENTS.md`` for the
  path -> decision table and the reason each path is a boundary redaction
  rather than a presentation one.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "JOURNAL_TEXT_CAP",
    "JOURNAL_TRUNCATION_MARKER",
    "REDACTION_FAILURE_PREFIX",
    "RedactionBoundaryReport",
    "journal_redaction_report",
    "redact_for_journal",
    "redact_text_for_journal",
    "reset_journal_redaction_report",
]

#: Hard ceiling on the characters of ONE string handed to the journal. Chosen
#: above every bound the harness already applies to tool output
#: (``agent_conversation_tool_chars`` 4000, ``max_tool_fanout_chars`` 24000,
#: ``cap_tool_output`` 4000 tokens, ``EditCheck``'s 1200-char whole-block cap)
#: so a normal value is never affected, and low enough that a pathological one
#: cannot make the journal write unbounded. Named here because
#: ``AGENTS.md``/the handoff must state the cap value at every call site.
JOURNAL_TEXT_CAP = 200_000

#: Appended to a capped value. Carries the ORIGINAL length, because "the value
#: was cut" and "the value was this long" are different claims and only one of
#: them is what a reader needs.
JOURNAL_TRUNCATION_MARKER = "\n.[journal value truncated: {kept} of {total} chars]"

#: The exact shape ``cli.notify._redact`` uses. Kept identical on purpose: a
#: second wording would make a grep for the withheld marker find one surface
#: and miss the other.
REDACTION_FAILURE_PREFIX = "(detail withheld: "

#: Depth limit for the recursive walk. A journal payload is a receipt, not a
#: document; a cycle or a pathological nesting is a bug, not something to
#: serialize forever.
_MAX_DEPTH = 12


class RedactionBoundaryReport:
    """Counters describing what the journal boundary actually did.

    Exists because "we redacted everything" is a claim and "we redacted
    everything" is also what a boundary that silently passed raw text would
    report. A test asserts these are zero, and a test asserts the boundary
    actually moved a value, so it cannot pass vacuously in either direction.
    """

    __slots__ = (
        "characters_withheld",
        "redactor_failures",
        "strings_capped",
        "values_seen",
        "values_withheld",
    )

    def __init__(self) -> None:
        self.values_seen = 0
        self.strings_capped = 0
        self.values_withheld = 0
        self.redactor_failures = 0
        self.characters_withheld = 0

    def as_dict(self) -> Dict[str, int]:
        """Return the counters as a JSON-safe mapping."""
        return {name: int(getattr(self, name)) for name in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"RedactionBoundaryReport({self.as_dict()})"


_REPORT = RedactionBoundaryReport()
_REPORT_LOCK = threading.Lock()


def _note(**counts: int) -> None:
    """Bump the process-wide counters. Never raises."""
    try:
        with _REPORT_LOCK:
            for name, amount in counts.items():
                setattr(_REPORT, name, int(getattr(_REPORT, name)) + int(amount))
    except Exception:  # pragma: no cover - a counter must never break a run
        pass


def journal_redaction_report() -> RedactionBoundaryReport:
    """Return a snapshot of the boundary counters for this process.

    Callers must treat the returned object as a snapshot; the live counters
    keep moving.
    """
    with _REPORT_LOCK:
        snapshot = RedactionBoundaryReport()
        for name in RedactionBoundaryReport.__slots__:
            setattr(snapshot, name, int(getattr(_REPORT, name)))
    return snapshot


def reset_journal_redaction_report() -> None:
    """Zero the counters. For tests; never call this from a run path."""
    with _REPORT_LOCK:
        for name in RedactionBoundaryReport.__slots__:
            setattr(_REPORT, name, 0)


def _withheld(reason: str) -> str:
    """Return the replacement text for a value that could not be redacted."""
    return f"{REDACTION_FAILURE_PREFIX}{reason})"


def _resolve_redactor() -> Tuple[Optional[Callable[..., Any]], str]:
    """Return ``(callable, name)`` for the ONE authority, or ``(None, why)``.

    Resolution is deliberately narrow: ``shared.security`` and nothing else.
    ``cli.notify`` falls back to a second, weaker local redactor because a
    desktop toast still has to say something; a journal row does not have that
    problem, and a weaker fallback on this path would be a second answer to
    "is this credential-shaped" - the exact divergence ``harness/trace.py``'s
    module docstring records having already paid for once.
    """
    try:
        from shared import security
    except Exception as exc:  # pragma: no cover - the tree always has shared/
        return None, f"shared.security is not importable ({type(exc).__name__})"
    for name in ("redact_secrets", "redact_text"):
        candidate = getattr(security, name, None)
        if callable(candidate):
            return candidate, f"shared.security.{name}"
    return None, "shared.security exposes no redactor"  # pragma: no cover


def _cap(text: str, cap: int) -> str:
    """Cap one string, keeping the head and naming the original length.

    ``cap <= 0`` means "no cap". A cap that does not exist must be
    expressible without emptying every value, for the same reason
    ``cap_tool_output`` documents its ``0``.
    """
    total = len(text)
    if cap <= 0 or total <= cap:
        return text
    kept = max(0, cap)
    _note(strings_capped=1, characters_withheld=total - kept)
    return text[:kept] + JOURNAL_TRUNCATION_MARKER.format(kept=kept, total=total)


def _cap_tree(
    value: Any,
    cap: int,
    depth: int,
) -> Any:
    """Recursively cap every string, PRESERVING the key/value structure.

    This pass deliberately does NOT touch the redactor. Two reasons, and the
    second one is a bug this module shipped in its first draft:

    1. **Order.** Bounding the input before the authority sees it is the whole
       point of the cap - redacting first and capping afterwards would let the
       authority spend its time on a megabyte no reader will look at, which is
       the failure `harness/AGENTS.md` records having already paid for on a
       ``"y" * 40000`` journal write.
    2. **Context.** ``shared.security.redact_secrets(value, key)`` decides
       whether a string is a secret partly from the KEY it sits under
       (``{"api_key": "top-secret"}`` is redacted; the bare string
       ``"top-secret"`` is not). Recursing into leaves and redacting each one
       separately throws that relationship away, and the first draft of this
       module did exactly that - so a nested credential passed through
       untouched while every test that used a bare string still passed.
       ``tests/test_secret_egress.py`` and the pre-existing
       ``tests/test_config_trace_state.py::test_trace_redacts_nested_credentials``
       are both what caught it.

    So: cap here, hand the WHOLE capped structure to the authority once.
    """
    if depth > _MAX_DEPTH:  # pragma: no cover - a receipt is never this deep
        return _withheld(f"nesting deeper than {_MAX_DEPTH} levels")
    if isinstance(value, str):
        return _cap(value, cap)
    if isinstance(value, bytes):
        # Journal payloads are JSON; a bytes value is a caller passing through
        # something the writer will str() anyway. Decoded once, capped, and
        # then handed to the authority as part of the capped structure.
        return _cap(value.decode("utf-8", errors="replace"), cap)
    if isinstance(value, Mapping):
        return {
            str(key): _cap_tree(item, cap, depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        capped = [_cap_tree(item, cap, depth + 1) for item in value]
        return tuple(capped) if isinstance(value, tuple) else capped
    if isinstance(value, set):  # pragma: no cover - journals are JSON-only
        return sorted(_cap_tree(item, cap, depth + 1) for item in value)
    # bool/int/float/None pass through uncapped and unredacted for the cap pass;
    # the authority still sees them as part of the whole structure.
    return value


def redact_for_journal(
    value: Any,
    *,
    where: str = "",
    cap: int = JOURNAL_TEXT_CAP,
) -> Any:
    """Redact one journal payload. Never raises, never passes raw text through.

    ``value`` is anything a trace row or an event payload can carry: a nested
    dict of tool output, a model response, an error string, a configuration
    snapshot. The return value is safe to serialize into
    ``logs/{task_id}/trace.jsonl`` and to hand to any downstream consumer -
    which is the property that makes "redact at the boundary" the right place
    to do this rather than at each presentation surface.

    ``where`` names the call site in the withheld reason, so a reader who finds
    a withheld value can find the boundary that withheld it. It is deliberately
    not used to build a success claim.

    Assumes ``shared.security`` is importable, which the repository guarantees;
    an unimportable authority is a DENIAL, not a pass-through.
    """
    _note(values_seen=1)
    location = f" at {where}" if where else ""
    redactor, name = _resolve_redactor()
    if redactor is None:
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"no redactor available{location}: {name}")

    # 1. bound every string, 2. hand the whole structure to the authority once.
    try:
        bounded = _cap_tree(value, cap, 0)
    except Exception as exc:  # pragma: no cover - _cap_tree is total
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"journal capping failed: {type(exc).__name__}{location}")

    try:
        cleaned = redactor(bounded)
    except Exception as exc:
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"{name} raised {type(exc).__name__}{location}")
    if cleaned is None and bounded is not None:
        # `unknown != False`: a redactor that answers "I do not know" to a
        # value it was given has not cleared the value, and reporting the raw
        # text here would be reporting a non-answer as a pass.
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"{name} returned None{location}")
    return cleaned


def redact_text_for_journal(
    text: Any,
    *,
    where: str = "",
    cap: int = JOURNAL_TEXT_CAP,
) -> str:
    """Redact one string for a journal row or an error string.

    Same fail-closed contract as :func:`redact_for_journal`, narrowed to text.
    Use this where the caller holds a bare string (an exception message, a
    rendered lint finding, an error detail) rather than a payload.
    """
    _note(values_seen=1)
    location = f" at {where}" if where else ""
    redactor, name = _resolve_redactor()
    if redactor is None:
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"no redactor available{location}: {name}")
    original = text if isinstance(text, str) else str(text or "")
    bounded = _cap(original, cap)
    try:
        cleaned = redactor(bounded)
    except Exception as exc:
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"{name} raised {type(exc).__name__}{location}")
    if cleaned is None:
        _note(values_withheld=1, redactor_failures=1)
        return _withheld(f"{name} returned None{location}")
    return cleaned if isinstance(cleaned, str) else str(cleaned)


def redact_iterable_for_journal(
    values: Iterable[Any],
    *,
    where: str = "",
    cap: int = JOURNAL_TEXT_CAP,
) -> list:
    """Redact every element of ``values`` and return a list.

    For a caller that holds a list of already-rendered strings (tool output
    lines, lint findings, approval prompts) and wants one boundary call rather
    than a comprehension of raw ``redact_text`` calls at the call site.
    """
    return [redact_for_journal(item, where=where, cap=cap) for item in values]
