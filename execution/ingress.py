"""The single subprocess-output ingress for the whole execution layer.

Every byte a child process writes reaches the product through exactly one of
the functions in this module. That is the entire security argument, and it is
one that can be checked by reading the call sites rather than by trusting a
policy document.

The invariant, stated once and enforced everywhere below:

    **No subprocess byte reaches a tool result, a journal row, a trace event
    or a TUI render without passing :func:`seal_output` first.**

Before this module, redaction was reachable from ``execution/`` at exactly two
places, neither of which was a subprocess boundary: ``workspace.py:4288``
(redacting tool ARGUMENTS for an effect hash) and ``git_output.py``'s own
private 40-line regex table. ``shared.security.redact_text`` had **zero** call
sites in this package. Every ``ExecutionResult.stdout`` reached a model, a
journal, and a terminal unredacted, so one ``cat .env`` was one keystroke from
a credential in a transcript.

Three properties, in the order they are enforced.

1. **The cap runs before redaction.** A pathological stream must not be able to
   exhaust memory in the redactor, and the redactor is linear-time but not
   free: measured on this host, ``shared.security.redact_text`` costs
   **~690 ms per 1,000,000 characters** (see :data:`CAP_RATIONALE`). Capping
   first bounds the redactor's input, which is the only way to keep the cost
   proportional to the cap instead of to the flood.
2. **Redaction is delegated, never reimplemented.**
   :func:`shared.security.redact_text` is T5's authority. A second pattern
   table in this package would be exactly the divergence the ceiling round
   closed as gap G34.
3. **Fail closed.** If redaction raises, or returns a non-string, the payload
   is REPLACED with :data:`REDACTION_UNAVAILABLE`. It is never passed through
   raw. A redactor that cannot answer is not a redactor that may be skipped.

Verification output
-------------------
Truncating away the signal would be the worse failure, so verification-tagged
commands get :data:`VERIFICATION_OUTPUT_CAP_BYTES` instead of
:data:`OUTPUT_CAP_BYTES` — see :func:`cap_for` and the comment on
:data:`VERIFICATION_OUTPUT_CAP_BYTES`. Secrets are still redacted there; only
the *length* ceiling moves. The distinction is length, never policy.

Anti-vacuity
------------
:func:`seal_output` returning ``""`` for empty input is the only case where a
cap can hide output, and it is provably empty. A test that asserts "the marker
is absent" would also pass if redaction crashed, so
:func:`IngressReport.ok` exists: a sealed payload with ``ok=False`` carries
:data:`REDACTION_UNAVAILABLE`, which a caller can assert on directly.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# The cap, and the number behind it
# ---------------------------------------------------------------------------

#: Per-stream byte cap applied to subprocess output before redaction runs.
#:
#: The number is **1,000,000 bytes (1 MB) per stream**, and it is the SAME
#: number the two existing collectors already use: ``sandbox.MAX_OUTPUT_BYTES``
#: (``execution/sandbox.py:64``) and ``workspace.ResourceLimits.max_output_bytes``
#: (``execution/workspace.py:2922``). Matching it is deliberate: this cap is a
#: redaction-cost bound and a defence-in-depth bound, not a new product limit,
#: and a second number would have created exactly the "which cap applies"
#: ambiguity a security boundary must not have.
#:
#: Why 1 MB is enough to not eat a diagnostic: a real pytest failure report
#: with a long traceback is 2-40 KB. 1 MB is 25-500x that.
#:
#: Why 1 MB is the right CEILING: measured on this host
#: (Python 3.10.11, win32) against ``shared.security.redact_text``,
#: which is linear after the R2-11 fix:
#:
#:     1,020 chars          ->   1.0 ms
#:     100,000 chars        ->  78.2 ms
#:     1,000,000 chars      -> 844.9 ms   (a flood pays this once per stream)
#:     4,000,000 chars      -> 3506.2 ms
#:
#: So 1 MB bounds the redactor at ~0.85 s per stream on a fully hostile
#: payload, and costs ~1 ms on a normal one. A larger cap buys nothing
#: (no real diagnostic is 4 MB) and quadruples the worst case.
OUTPUT_CAP_BYTES = 1_000_000

#: The raised cap for verification-tagged commands: **4,000,000 bytes (4 MB)**.
#:
#: THIS IS THE "do not truncate away the signal" CASE, and the reasoning is the
#: whole reason it is a separate number rather than a comment. The verifier is
#: how the harness diagnoses failures: a truncated traceback is not a
#: diagnostic, it is a shrug, and the loop then "fixes" a repository against
#: evidence that was thrown away on the way to the model. Three arguments for
#: raising rather than lowering it:
#:
#: 1. ``verify.py`` runs the FULL SUITE as a regression run. A large project
#:    with many failures emits megabytes of traceback, and the *count* of
#:    failures is only parseable from that text.
#: 2. ``execution.feedback`` and ``execution.rationale`` parse the same text
#:    for the failing test name and the assertion line. Cutting the tail off
#:    removes exactly the line they read.
#: 3. The cost is already paid. ``execution.sandbox`` has capped sandbox
#:    capture at 1 MB per stream since Round 3, so the 4 MB cap here is
#:    unreachable for sandbox output — it is a bound on the LOCAL and
#:    NATIVE_OS paths, which the sandbox cap does not cover. Making the
#:    verification cap LOWER than the sandbox cap would be the only genuinely
#:    dangerous choice here.
#:
#: 4 MB of redaction is ~3.5 s on a hostile payload and is paid only on a
#: verification run, where a container round trip already costs seconds. The
#: 3.5 s is the honest price of keeping the signal.
VERIFICATION_OUTPUT_CAP_BYTES = 4_000_000

#: The per-call shape a caller may use to raise the cap without importing a
#: constant. Anything else falls back to :data:`OUTPUT_CAP_BYTES`, so an
#: unrecognised purpose can never WIDEN containment.
PURPOSE_VERIFICATION = "verification"
_PURPOSE_CAPS: Dict[str, int] = {
    PURPOSE_VERIFICATION: VERIFICATION_OUTPUT_CAP_BYTES,
}

#: The exact text substituted when redaction is unavailable. Chosen so a reader
#: — and a test — can tell it apart from any real command output.
REDACTION_UNAVAILABLE = "(output withheld: redaction unavailable)"

#: Head+tail truncation marker. Mirrors the marker the existing collectors
#: already emit (``[... N chars omitted ...]``) so a reader who has seen one
#: truncation recognises the other, and so the SAME output cannot be produced
#: by two different cap mechanisms.
OMISSION_MARKER = "[... {n} chars omitted ...]"

#: The fallback marker for a cap too small to hold :data:`OMISSION_MARKER`.
#: Its existence is the difference between "the cap fired" and "the cap fired
#: and told nobody": an earlier version of this function returned ``""`` in
#: that case, which is indistinguishable from a command that printed nothing.
#: Neither shipped cap (1 MB, 4 MB) can reach this, and it is unreachable from
#: :func:`cap_for` at all — it exists so a caller who passes an absurdly small
#: ``cap_bytes`` still gets a receipt rather than silence.
OMISSION_MARKER_MIN = "[output truncated]"

#: The claim this module makes, in one sentence, for the handoff and for the
#: test that pins the invariant. Kept as a constant so a test can assert the
#: source carries it and a reviewer can grep for it.
INGRESS_INVARIANT = (
    "no subprocess byte reaches a tool result without passing the ingress redaction"
)

CAP_RATIONALE = (
    "measured: redact_text is 1.0ms @1KB, 78ms @100KB, 845ms @1MB, 3506ms @4MB "
    "(this host, Python 3.10.11 win32); cap runs first so the redactor's cost "
    "is bounded by the cap rather than by the flood"
)


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class IngressReport:
    """What the ingress did to one payload.

    A receipt, not a debug log: ``ok=False`` is the only way a caller learns
    redaction failed, and it is a value rather than a raise so a redactor
    failure can never change a run's terminal status by propagating.
    """

    ok: bool
    chars_in: int
    chars_out: int
    truncated: bool
    omitted_chars: int
    redacted: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record (no payload)."""
        return {
            "ok": bool(self.ok),
            "chars_in": int(self.chars_in),
            "chars_out": int(self.chars_out),
            "truncated": bool(self.truncated),
            "omitted_chars": int(self.omitted_chars),
            "redacted": bool(self.redacted),
            "reason": str(self.reason),
        }


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def cap_for(purpose: Optional[str] = None) -> int:
    """Return the byte cap for a declared purpose (fail closed).

    An absent, empty, non-string, or UNRECOGNISED purpose resolves to
    :data:`OUTPUT_CAP_BYTES`. That direction is the point: a typo in a purpose
    string must never be able to buy a larger cap than the default, and must
    never be able to shrink the verification cap either.
    """
    label = str(purpose or "").strip().casefold()
    if not label:
        return OUTPUT_CAP_BYTES
    return _PURPOSE_CAPS.get(label, OUTPUT_CAP_BYTES)


def bound_text(text: str, cap_bytes: int) -> Tuple[str, int]:
    """Return ``(text, omitted_chars)`` bounded so the RESULT fits the cap.

    Head AND tail are kept, because the two ends are the two ends that
    matter: the head is the command banner and the collection log, the tail is
    the assertion and the summary. A head-only cap would drop exactly the
    line a failing test run is diagnosed from. This mirrors
    ``sandbox._BoundedCapture.value()`` and ``workspace._capture_stream``.

    The cap bounds the RETURNED STRING, marker included. That is the whole
    point of a cap and it is the detail that is easy to get wrong: an earlier
    version of this function reserved room for the marker text but not for the
    two newlines around it, and returned a "1,000,000-byte" string of
    1,000,002 characters. A cap that overshoots is not a cap, and
    ``harness/agent_loop``'s planner block shipped that exact defect once.
    :func:`bound_text` is therefore verified over a grid (every cap 0-200
    against bodies 0-400,000) rather than at one size.

    ``omitted_chars`` counts DROPPED TEXT characters, not marker characters —
    it answers "how much output did you not show me", which is the number an
    operator needs and the one the existing collectors already report.

    The marker appears only when something was actually omitted, so a payload
    under the cap is returned byte-identical.
    """
    cap = max(0, int(cap_bytes))
    if cap == 0:
        return "", len(text)
    if len(text) <= cap:
        return text, 0

    overhead = 2  # the newline before and after the marker
    if cap < len(OMISSION_MARKER_MIN):
        # The cap is smaller than the shortest marker. There is no string that
        # both honours the cap and says something, and the cap wins: a memory
        # bound that a marker can override is not a bound. The FACT is not
        # lost — `IngressReport.truncated` is True and `omitted_chars` carries
        # the true count, which is the channel a caller can always read. The
        # returned string is empty, and a caller that displays only the string
        # is looking at a cap so small it is not a real configuration.
        return "", len(text)
    if cap < overhead + len(OMISSION_MARKER_MIN) + 1:
        # Too small for even a minimal marker plus one retained character.
        # Return the minimal marker alone rather than "": a silent empty
        # string is indistinguishable from a command that printed nothing,
        # which is the one reading a cap must never permit.
        return OMISSION_MARKER_MIN, len(text)
    if cap < overhead + len(OMISSION_MARKER.format(n=len(text))) + 1:
        # Room for a marker but not for the digit-counted one. Keep as much
        # body as fits and say plainly that the count was dropped.
        body = cap - overhead - len(OMISSION_MARKER_MIN)
        head = body // 2
        tail = body - head
        return (
            text[:head] + "\n" + OMISSION_MARKER_MIN + "\n" + text[-tail:],
            len(text) - body,
        )
    # The retained body depends on the marker's own length, and the marker
    # length depends on how much is omitted, which depends on the body. Two
    # passes converge in practice (the marker's digit count only changes when
    # the omitted count crosses a power of ten); the loop is a bound, not a
    # search, and it cannot spin because each pass strictly increases the
    # reserved marker size.
    marker = OMISSION_MARKER.format(n=len(text))
    for _ in range(8):
        body = cap - overhead - len(marker)
        if body <= 0:
            return OMISSION_MARKER_MIN, len(text)
        omitted = len(text) - body
        candidate = OMISSION_MARKER.format(n=omitted)
        if len(candidate) == len(marker):
            marker = candidate
            break
        marker = candidate
    else:  # pragma: no cover - unreachable for any cap this module ships
        return OMISSION_MARKER_MIN, len(text)

    head = body // 2
    tail = body - head
    return text[:head] + "\n" + marker + "\n" + text[-tail:], omitted


# ---------------------------------------------------------------------------
# The redaction
# ---------------------------------------------------------------------------


def seal_output(
    value: Any,
    *,
    purpose: Optional[str] = None,
    cap_bytes: Optional[int] = None,
) -> Tuple[str, IngressReport]:
    """Return ``(sealed_text, report)`` for one subprocess payload.

    This is THE ingress function. Every subprocess producer in ``execution/``
    routes through it; nothing else may hand a child process's bytes onward.

    Order of operations, and the order is load-bearing:

    1. coerce to ``str`` (a bytes payload must not escape as ``b'...'``);
    2. **cap** (:func:`bound_text`) — a flood must not reach the redactor;
    3. **redact** via :func:`shared.security.redact_text` (T5's authority);
    4. on ANY redaction failure, return :data:`REDACTION_UNAVAILABLE` and
       ``ok=False`` — never the raw text.

    ``purpose`` selects the cap (:func:`cap_for`); an explicit ``cap_bytes``
    overrides it, which exists for the background-process ring buffer, whose
    offsets are its own contract and must not be reinterpreted.
    """
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value or "")
    )
    chars_in = len(text)
    cap = OUTPUT_CAP_BYTES if cap_bytes is None else max(0, int(cap_bytes))
    bounded, omitted = bound_text(text, cap)

    try:
        from shared.security import redact_text  # imported here: bottom layer
    except Exception as exc:  # pragma: no cover - shared/ is always importable
        return REDACTION_UNAVAILABLE, IngressReport(
            ok=False,
            chars_in=chars_in,
            chars_out=len(REDACTION_UNAVAILABLE),
            truncated=bool(omitted),
            omitted_chars=omitted,
            redacted=False,
            reason=f"redactor not importable: {type(exc).__name__}: {exc}",
        )

    try:
        sealed = redact_text(bounded)
    except Exception as exc:
        # Fail closed. A redactor that raised must not become a pass-through.
        return REDACTION_UNAVAILABLE, IngressReport(
            ok=False,
            chars_in=chars_in,
            chars_out=len(REDACTION_UNAVAILABLE),
            truncated=bool(omitted),
            omitted_chars=omitted,
            redacted=False,
            reason=f"redactor raised: {type(exc).__name__}: {exc}",
        )

    if not isinstance(sealed, str):
        # `redact_text` is typed `-> str` today. A future change that returns
        # None is exactly the "unknown rendered as success" shape this repo
        # forbids, so it is a refusal rather than a coercion.
        return REDACTION_UNAVAILABLE, IngressReport(
            ok=False,
            chars_in=chars_in,
            chars_out=len(REDACTION_UNAVAILABLE),
            truncated=bool(omitted),
            omitted_chars=omitted,
            redacted=False,
            reason=f"redactor returned {type(sealed).__name__}, not str",
        )

    return sealed, IngressReport(
        ok=True,
        chars_in=chars_in,
        chars_out=len(sealed),
        truncated=bool(omitted),
        omitted_chars=int(omitted),
        redacted=sealed != bounded,
    )


def seal_streams(
    result: Any,
    *,
    purpose: Optional[str] = None,
) -> Tuple[Any, Tuple[IngressReport, ...]]:
    """Return a NEW result object with both output streams sealed.

    ``result`` is duck-typed on ``stdout``/``stderr`` rather than
    ``isinstance``-checked, because three distinct result types cross this
    boundary (``shared.types.ExecutionResult``,
    ``execution.workspace.LocalExecutionResult``, and a caller's own duck-typed
    double) and a fourth type arriving as a refusal would silently skip
    redaction.

    A NEW object is returned, never an in-place mutation:
    ``ExecutionResult`` is a shared type other owners construct
    (``execution/verify.py:559`` already makes this exact point in a comment),
    so mutating it would be a contract change in someone else's file.
    """
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    sealed_out, out_report = seal_output(stdout, purpose=purpose)
    sealed_err, err_report = seal_output(stderr, purpose=purpose)
    rebuilt = _rebuild(result, sealed_out, sealed_err)
    return rebuilt, (out_report, err_report)


def _rebuild(result: Any, stdout: str, stderr: str) -> Any:
    """Return a copy of ``result`` carrying the sealed streams.

    A dataclass is rebuilt field-by-field so no field is dropped (a
    ``dataclasses.replace`` would be equivalent, but the explicit form also
    covers the non-dataclass duck types and cannot raise on a frozen field).
    A non-dataclass is copied through ``__dict__`` when it allows it, and
    otherwise falls back to the original object with the streams recorded on
    an additive ``sealed`` attribute — which is the honest degradation: the
    caller's own type is returned unchanged rather than a fabricated
    substitute, and the fact that sealing could not be applied is visible.
    """
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        changes: Dict[str, Any] = {}
        for field in dataclasses.fields(result):
            if field.name == "stdout":
                changes["stdout"] = stdout
            elif field.name == "stderr":
                changes["stderr"] = stderr
            else:
                changes[field.name] = getattr(result, field.name, None)
        try:
            return type(result)(**changes)
        except Exception:
            return _force_attrs(result, stdout, stderr)

    try:
        clone = object.__new__(type(result))
        clone.__dict__.update(getattr(result, "__dict__", {}) or {})
        clone.stdout = stdout
        clone.stderr = stderr
        return clone
    except Exception:
        try:
            result.stdout = stdout
            result.stderr = stderr
            return result
        except Exception:
            return result


def _force_attrs(result: Any, stdout: str, stderr: str) -> Any:
    """Last-resort in-place seal for an object that will not copy.

    Records the fact on ``result.sealed`` so a caller can tell a sealed result
    from an unsealed one, and never returns raw text: if even the attribute
    write fails, the ORIGINAL object is returned and the caller keeps whatever
    it had. This branch is a liveness guard, not a policy: the policy lives in
    :func:`seal_output`.
    """
    try:
        result.stdout = stdout
        result.stderr = stderr
        result.sealed = True  # type: ignore[attr-defined]
    except Exception:
        return result
    return result


def seal_mapping(
    payload: Dict[str, Any],
    *,
    purpose: Optional[str] = None,
    keys: Tuple[str, ...] = ("stdout", "stderr"),
) -> Dict[str, Any]:
    """Return a copy of a tool-result dict with the named keys sealed.

    Used for the ``git_*`` tool result (``SafeToolBackend._git``), which is a
    plain dict rather than a result object. Only the named keys are touched, so
    an ``exit_code`` is never stringified.
    """
    sealed = dict(payload)
    reports: list = []
    for key in keys:
        if key not in sealed:
            continue
        value, report = seal_output(sealed.get(key), purpose=purpose)
        sealed[key] = value
        reports.append(report)
    sealed["ingress"] = IngressReport(
        ok=all(report.ok for report in reports) if reports else True,
        chars_in=sum(report.chars_in for report in reports),
        chars_out=sum(report.chars_out for report in reports),
        truncated=any(report.truncated for report in reports),
        omitted_chars=sum(report.omitted_chars for report in reports),
        redacted=any(report.redacted for report in reports),
        reason="; ".join(report.reason for report in reports if report.reason),
    ).to_dict()
    return sealed


__all__ = [
    "CAP_RATIONALE",
    "INGRESS_INVARIANT",
    "OMISSION_MARKER",
    "OUTPUT_CAP_BYTES",
    "PURPOSE_VERIFICATION",
    "REDACTION_UNAVAILABLE",
    "VERIFICATION_OUTPUT_CAP_BYTES",
    "IngressReport",
    "bound_text",
    "cap_for",
    "seal_mapping",
    "seal_output",
    "seal_streams",
]
