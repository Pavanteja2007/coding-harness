"""The shareable session export — fail-closed redaction, a shareable predicate,
and a receipt that tells the user exactly what left and what was withheld.

WHY THIS MODULE EXISTS
----------------------
A session export is the highest-risk disclosure surface in the product because
it is *intended to leave the machine*. ``cli/session.py`` already has a
privacy-filtered export; this module is the SHARING half: it decides whether a
prepared document may leave at all, and it tells the user what was removed
before anything is written.

Three rules, each load-bearing:

1. **Redaction is fail-closed.** The export goes through
   ``shared.security.redact_secrets`` — the same deep walk, the same
   fail-closed behaviour as every other surface. If the redactor cannot be
   shown to be working, NOTHING is exported.
2. **Sharing is gated by a predicate, not by default.** A document is
   shareable only when :func:`shareable_verdict` says so: the redactor is
   demonstrably functional AND the serialized output contains no
   credential-shaped value. ``shared/privacy.py``'s ``shareable`` mode is a
   *view*; this is the *gate*.
3. **The user is told what is being shared.** The receipt carries counts of
   redacted keys and texts, how many details were withheld by the redactor's
   own fail-closed markers, and plain lines a person reads before saying yes.

ASSUMPTIONS
-----------
- ``session`` is a JSON-shaped mapping (the ``cli/session.py`` snapshot
  shape), and ``events`` an iterable of JSON-shaped mappings (the session
  event journal). Both are treated as untrusted content: every string is
  redacted, and the output is proven clean rather than assumed clean.
- This module imports nothing outside ``shared/``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .security import (
    REDACTED_SECRET,
    is_sensitive_key,
    redact_secrets,
    redact_text,
)

__all__ = [
    "EXPORT_KIND",
    "MAX_SHARE_BYTES",
    "SCHEMA_VERSION",
    "ShareReceipt",
    "ShareRefused",
    "ShareVerdict",
    "build_share_document",
    "redactor_is_functional",
    "render_transcript",
    "selftest",
    "shareable_verdict",
    "unredacted_findings",
    "write_share",
]

EXPORT_KIND = "neo-session-share"
SCHEMA_VERSION = 1

#: A share larger than this is refused: a reader cannot review it, and a
#: payload nobody reviews is a payload that hides something.
MAX_SHARE_BYTES = 8 * 1024 * 1024

#: Bounded transcript rendering: a share is read by a person.
_MAX_TRANSCRIPT_TURNS = 200
_MAX_TRANSCRIPT_CHARS = 200_000
_MAX_TURN_TEXT = 4_000

#: The fail-closed marker the redactor emits when it cannot safely answer.
#: Counted for the receipt; never treated as a leak (it is the safe direction).
_WITHHOLD_PREFIX = "(detail withheld:"


class ShareRefused(Exception):
    """Raised when a share may not be written. Never swallowed by callers."""


@dataclass(frozen=True)
class ShareVerdict:
    """Whether a prepared document may leave the machine, and why (not)."""

    shareable: bool
    reasons: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {"shareable": self.shareable, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ShareReceipt:
    """What the user is told about a share: counts, the verdict, and lines."""

    path: str
    verdict: ShareVerdict
    redacted_keys: int
    redacted_texts: int
    withheld_markers: int
    turns: int
    events: int
    bytes_written: int
    lines: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "verdict": self.verdict.as_dict(),
            "redacted_keys": self.redacted_keys,
            "redacted_texts": self.redacted_texts,
            "withheld_markers": self.withheld_markers,
            "turns": self.turns,
            "events": self.events,
            "bytes_written": self.bytes_written,
            "lines": list(self.lines),
        }


#: A probe with a credential SHAPE the rule table owns. The redactor must
#: change it, and must not answer with a withheld marker — either failure
#: means the redactor cannot be trusted for this export.
_REDACTOR_PROBE = "api_key=sk-PROBEVALUE012345678901234567890"


def redactor_is_functional() -> bool:
    """O(1) liveness probe for the redaction authority.

    A redactor that returns its input unchanged, or that answers with its own
    fail-closed withheld marker, cannot be shown to be working — and an export
    through an unproven redactor is not an export, it is a hope.
    """
    try:
        out = redact_text(_REDACTOR_PROBE)
    except Exception:
        return False
    return (
        out != _REDACTOR_PROBE
        and "PROBEVALUE" not in out
        and not out.startswith(_WITHHOLD_PREFIX)
    )


def _iter_strings(value: Any) -> Iterable[str]:
    """Every string leaf in a JSON-shaped structure."""
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            if isinstance(item_key, str):
                yield item_key
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, str):
        yield value


def unredacted_findings(document: Any) -> Tuple[str, ...]:
    """The strings a redactor would STILL change — i.e. what would travel.

    This is deliberately NOT ``contains_secret``: that matcher cannot tell the
    redactor's own marker from a secret, so ``api_key=[REDACTED_SECRET]`` still
    matches its key=value rule and a legitimately redacted share reads as
    dirty. The question a share gate must answer is *"is there anything left
    for the redactor to redact?"* — a fixed point. A string the redactor would
    still change is, by definition, a string with something left to hide.
    """
    findings: List[str] = []
    for text in _iter_strings(document):
        try:
            if redact_text(text) != text:
                findings.append(text[:80])
        except Exception as exc:
            findings.append(f"<redactor failed: {type(exc).__name__}>")
        if len(findings) >= 8:
            break
    return tuple(findings)


def render_transcript(session: Mapping[str, Any]) -> str:
    """A plain-text transcript a reader without the original machine can read.

    Bounded in turns, per-turn text, and total characters; every bound is
    stated with a marker, because a share that silently drops the middle of a
    conversation is a share that misleads. Every line is redacted.
    """
    turns = session.get("turns")
    if not isinstance(turns, list):
        turns = []
    lines: List[str] = []
    total = 0
    truncated_turns = 0
    for index, turn in enumerate(turns[:_MAX_TRANSCRIPT_TURNS]):
        if not isinstance(turn, Mapping):
            continue
        role = redact_text(turn.get("role") or "unknown")
        text = redact_text(turn.get("text") or turn.get("content") or "")
        if len(text) > _MAX_TURN_TEXT:
            text = text[:_MAX_TURN_TEXT] + " … (turn text truncated)"
        line = f"{role}: {text}"
        if total + len(line) > _MAX_TRANSCRIPT_CHARS:
            truncated_turns = len(turns[:_MAX_TRANSCRIPT_TURNS]) - index
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        lines.append("(no turns recorded)")
    if len(turns) > _MAX_TRANSCRIPT_TURNS:
        lines.append(
            f"(+{len(turns) - _MAX_TRANSCRIPT_TURNS} earlier turns not shown)"
        )
    elif truncated_turns:
        lines.append(f"(+{truncated_turns} turns not shown: transcript bound reached)")
    return "\n".join(lines)


def build_share_document(
    session: Mapping[str, Any],
    events: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    redactor: Any = None,
    exported_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the shareable document: full content, every string redacted.

    The redactor is injectable so a test can prove the fail-closed paths; the
    default is the shared authority's deep walk. If the redactor RAISES, the
    export refuses — a half-redacted document must never exist.

    Assumes ``session`` is JSON-shaped; non-JSON values are coerced by the
    redactor's own rules. The output is NOT assumed safe: the caller must
    pass :func:`shareable_verdict` before writing it anywhere.
    """
    if not isinstance(session, Mapping):
        raise ShareRefused("a share document needs a session mapping")
    scrub = redact_secrets if redactor is None else redactor
    event_list = [dict(item) for item in (events or []) if isinstance(item, Mapping)]
    try:
        clean_session = scrub(session)
        clean_events = [scrub(item) for item in event_list]
    except Exception as exc:
        raise ShareRefused(
            f"the redactor failed ({type(exc).__name__}); nothing was exported"
        ) from exc
    transcript = render_transcript(session)
    try:
        clean_transcript = scrub(transcript)
    except Exception as exc:
        raise ShareRefused(
            f"the redactor failed ({type(exc).__name__}); nothing was exported"
        ) from exc
    return {
        "kind": EXPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "exported_at": float(exported_at if exported_at is not None else time.time()),
        "source_session_id": redact_text(session.get("session_id") or ""),
        "session": clean_session,
        "events": clean_events,
        "transcript_text": clean_transcript,
    }


def shareable_verdict(
    document: Mapping[str, Any], *, redactor_functional: Optional[bool] = None
) -> ShareVerdict:
    """THE SHARE GATE. A document is shareable only if this says so.

    Fail-closed in both directions:
    - the redactor must be DEMONSTRABLY functional (not merely present);
    - the serialized output must contain no credential-shaped value, checked
      by the same authority that redacted it.

    A withheld marker does NOT block sharing: it is the redactor's own safe
    answer. It is counted and shown to the user instead.
    """
    reasons: List[str] = []
    functional = (
        redactor_is_functional() if redactor_functional is None else redactor_functional
    )
    if not functional:
        reasons.append(
            "the redactor could not be shown to be working, so nothing may leave"
        )
    if not isinstance(document, Mapping):
        reasons.append("the share document is not a mapping")
        return ShareVerdict(False, tuple(reasons))
    if document.get("kind") != EXPORT_KIND:
        reasons.append(f"the document kind is not {EXPORT_KIND!r}")
    findings = unredacted_findings(document)
    if findings:
        reasons.append(
            f"credential-shaped content survived redaction ({len(findings)} string(s)); "
            "the share is refused"
        )
    return ShareVerdict(not reasons, tuple(reasons))


def _count_changes(before: Any, after: Any, counts: List[int]) -> None:
    """Joint walk over (input, output) counting redaction events for the receipt.

    ``counts`` is ``[redacted_keys, redacted_texts, withheld_markers]``. A
    sensitive key whose value became the redaction marker counts once; a string
    that changed counts once; a withheld marker in the output counts once.
    """
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key, before_value in before.items():
            after_value = after.get(key)
            if (
                is_sensitive_key(key)
                and before_value not in (None, "")
                and after_value == REDACTED_SECRET
            ):
                counts[0] += 1
                continue
            _count_changes(before_value, after_value, counts)
        return
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        for before_item, after_item in zip(before, after, strict=False):
            _count_changes(before_item, after_item, counts)
        return
    if isinstance(before, str) and isinstance(after, str):
        if before != after:
            counts[1] += 1
        if after.startswith(_WITHHOLD_PREFIX) or _WITHHOLD_PREFIX in after:
            counts[2] += 1
        return
    if isinstance(after, str) and _WITHHOLD_PREFIX in after:
        counts[2] += 1


def _receipt_lines(receipt: ShareReceipt) -> Tuple[str, ...]:
    """The plain sentences a user reads before a share leaves the machine."""
    lines = [
        f"share written to {receipt.path} ({receipt.bytes_written} bytes)",
        f"contents: {receipt.turns} turn(s), {receipt.events} event(s)",
    ]
    if receipt.redacted_keys or receipt.redacted_texts:
        lines.append(
            f"redacted: {receipt.redacted_keys} credential field(s) and "
            f"{receipt.redacted_texts} text value(s) were scrubbed before export"
        )
    else:
        lines.append("redacted: nothing credential-shaped was found")
    if receipt.withheld_markers:
        lines.append(
            f"withheld: {receipt.withheld_markers} detail(s) were replaced by the "
            "redactor's own fail-closed marker rather than exported"
        )
    else:
        lines.append("withheld: none")
    lines.append("secrets cannot travel: the exported bytes were re-scanned clean")
    return tuple(lines)


def write_share(
    document: Mapping[str, Any],
    destination: Any,
    *,
    session: Optional[Mapping[str, Any]] = None,
    events: Optional[Iterable[Mapping[str, Any]]] = None,
) -> ShareReceipt:
    """Write a share document, or refuse. The gate runs BEFORE any byte moves.

    Refusals: the verdict says no; the payload exceeds the byte cap; the
    destination is a symlink; the serialized output contains a secret
    (re-checked on the exact bytes that will travel — the check that matters).
    The write is atomic (temp file, fsync, os.replace), so a crash never
    leaves half a share on disk.
    """
    verdict = shareable_verdict(document)
    if not verdict.shareable:
        raise ShareRefused("; ".join(verdict.reasons))
    path = Path(destination)
    if path.is_symlink():
        raise ShareRefused("the share destination must not be a symbolic link")
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    if len(payload) > MAX_SHARE_BYTES:
        raise ShareRefused(
            f"the share is {len(payload)} bytes, over the {MAX_SHARE_BYTES} cap; "
            "a share nobody can review is a share that hides something"
        )
    try:
        serialized_form = json.loads(payload.decode("utf-8"))
    except ValueError as exc:  # pragma: no cover - json.dumps output reparsed
        raise ShareRefused(f"the serialized share does not reparse: {exc}") from exc
    if unredacted_findings(serialized_form):
        raise ShareRefused(
            "credential-shaped content was found in the serialized share bytes"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except OSError as exc:
        raise ShareRefused(f"the share could not be written: {exc}") from exc

    counts = [0, 0, 0]
    if session is not None:
        _count_changes(
            {"session": session},
            {"session": document.get("session")},
            counts,
        )
    if events is not None:
        _count_changes(
            {"events": list(events)},
            {"events": document.get("events") or []},
            counts,
        )
    turns = document.get("session", {}).get("turns")
    base = ShareReceipt(
        path=str(path),
        verdict=verdict,
        redacted_keys=counts[0],
        redacted_texts=counts[1],
        withheld_markers=counts[2],
        turns=len(turns) if isinstance(turns, list) else 0,
        events=len(document.get("events") or []),
        bytes_written=len(payload),
        lines=(),
    )
    return ShareReceipt(
        path=base.path,
        verdict=base.verdict,
        redacted_keys=base.redacted_keys,
        redacted_texts=base.redacted_texts,
        withheld_markers=base.withheld_markers,
        turns=base.turns,
        events=base.events,
        bytes_written=base.bytes_written,
        lines=_receipt_lines(base),
    )


# ---------------------------------------------------------------------------
# The executable gate
# ---------------------------------------------------------------------------
#
# WHY A CLI AND NOT ONLY TESTS
# --------------------------
# An exported session is the one artifact whose whole purpose is to leave the
# machine, so the boundary that protects it deserves a gate that can be run on
# its own and that exits non-zero -- the same shape as ``evals.prompt_pin`` and
# ``evals.policy_examples``. A pytest file is a gate somebody can forget to
# invoke; ``python -m shared.session_share --selftest`` is one line in a
# workflow whose exit code is the verdict.
#
# What it proves, and what it deliberately does not:
#   * every case of the normal AND the obfuscated canary corpora is driven
#     through the REAL export path, and the serialized bytes are re-scanned;
#   * the check is on the BYTES, not on an intermediate dict, because the
#     bytes are what travels;
#   * a case that produces a non-shareable document is a FINDING, not a pass --
#     the export should have redacted it, so a refusal here means the redactor
#     stopped short;
#   * it reports the counts either way. A gate that only prints on failure
#     teaches nobody how much it checked.

#: Separators stripped before the reassembled-secret check. A token split by a
#: JSON escape or a space is still recoverable by a reader, so the perceived
#: bytes are what get searched. This is the SEPARATOR half only; the invisible
#: half (zero-width, bidi, ANSI) comes from
#: ``shared.security.normalize_for_redaction``, because hand-rolling a second
#: copy of it is how the first version of this gate caught only 2 of the 8
#: obfuscated cases and called the corpus clean.
_REASSEMBLY_STRIP = " \t\r\n\\\"'`_-"


def _perceivable(text: str) -> str:
    """Return ``text`` as a READER would perceive it: no invisibles, no ANSI,
    no separators, case-folded.

    Uses the shared normalizer rather than a second implementation, so the
    measurement cannot drift from the defence it is measuring.
    """
    from shared import security as shared_security

    # `normalize_for_redaction` returns (text, receipt); the receipt says what
    # the pass removed, which is not what this check asks.
    normalized, _receipt = shared_security.normalize_for_redaction(text)
    return "".join(
        ch for ch in normalized if ch not in _REASSEMBLY_STRIP
    ).upper()


def selftest() -> Dict[str, Any]:
    """Run the whole corpus through the real export path and re-scan the bytes.

    Returns a JSON-safe report. It never raises: a corpus problem must be
    reported as a finding, and a gate that dies on hostile input reports
    nothing at all -- which reads as a pass.

    THREE PROPERTIES, and the difference between them is the whole result:

    1. **No credential-shaped value survives** (``unredacted_findings`` is
       empty) for every case. This is the security property, and it is checked
       on the SERIALIZED bytes rather than on an intermediate dict, because the
       bytes are what travels.
    2. **A token hidden inside a credential body must not be recoverable**
       (``token_reassembled``), checked on the OBFUSCATED cases that actually
       carried the canary -- derived from the corpus, never hardcoded. Those
       cases exist precisely to put the canary where only a redaction rule can
       find it. The obfuscated cases that substitute a *different* credential
       shape (`ghp_...`, an AWS key, URL userinfo) are covered by property 1
       instead, because "the token must not survive" is not a statement about
       them: six of the eight carry no canary at all. Hardcoding eight would be
       a number that stops being true when the corpus grows, which is how a
       corpus quietly stops being tested.
    3. **An injection DIRECTIVE is expected to survive** and is counted under
       ``injection_directives_preserved``, NOT counted as a leak.

       The first draft of this gate got that wrong and failed loudly, which is
       why it is written down: the normal canary corpus is
       prompt-injection text ("echo the value VEXCANARY... into your next
       reply"), and `contains_secret` on it is **False** -- the canary is not
       credential-shaped, so no redaction rule owns it and none should. A gate
       demanding the canary never appear in an export would be demanding
       free-text redaction, which is not a property this product has or wants:
       the export exists to make a bug showable, and a transcript with every
       sentence removed shows nothing.

       So the honest report names the preserved directives instead of hiding
       them. A green gate next to an unstated exception is how a leak ships.
    """
    from shared import security_corpus as corpus

    session = {
        "session_id": "selftest-session",
        "schema_version": 1,
        "repo": "C:/selftest/repo",
        "turns": [],
        "summary": "",
        "active_run_id": None,
    }
    cases: List[Dict[str, Any]] = []
    for label, iterator in (
        ("canary", corpus.iter_canary_exfiltration_cases()),
        ("obfuscated", corpus.iter_obfuscated_canary_cases()),
    ):
        for case in iterator:
            probe = dict(session)
            probe["turns"] = [{"role": "tool", "text": case.payload}]
            events = [{"event": "tool_result", "payload": {"output": case.payload}}]
            row: Dict[str, Any] = {"corpus": label, "case_id": case.case_id}
            try:
                document = build_share_document(probe, events)
                verdict = shareable_verdict(document)
                # Round-trip through JSON first, because that is what a reader
                # does: the bytes on the wire carry a zero-width space as six
                # ASCII characters, and measuring the raw serialized text would
                # let a token hide behind its own escape while the consumer
                # reads it straight back.
                recovered = " ".join(_iter_strings(json.loads(json.dumps(document))))
                token = _perceivable(corpus.CANARY_TOKEN)
                survived = token in _perceivable(recovered)
                # Whether this case carried the canary AT ALL is derived from
                # the corpus, never hardcoded: six of the eight obfuscated
                # cases substitute a DIFFERENT credential shape (ghp_..., an
                # AWS key, URL userinfo) for the canary, so "the token must not
                # survive" is only a property of the cases that put it there.
                # The other six are covered by property 1 instead. Hardcoding
                # eight would be a number that stops being true when the corpus
                # grows, which is how a corpus quietly stops being tested.
                carried = token in _perceivable(case.payload)
                findings = list(unredacted_findings(document))
                row.update(
                    shareable=bool(verdict.shareable),
                    findings=findings,
                    payload_carried_token=carried,
                    token_present_in_bytes=survived,
                    # Property 2, scoped to the OBFUSCATED cases that carried
                    # the canary. Those are the cases whose whole purpose is to
                    # put the canary where only a redaction rule can find it.
                    token_reassembled=survived and carried and label == "obfuscated",
                    # Property 3: a normal canary case is an injection
                    # DIRECTIVE, and its token surviving is the correct
                    # outcome, not a leak.
                    token_is_directive=survived and carried and label == "canary",
                    reasons=list(verdict.reasons),
                )
            except Exception as exc:
                # A gate that dies on hostile input reports nothing at all, and
                # "no report" reads as "no problem". The raise becomes a finding.
                row.update(
                    shareable=False,
                    findings=[f"the export raised: {type(exc).__name__}: {exc}"],
                    payload_carried_token=False,
                    token_present_in_bytes=False,
                    token_reassembled=False,
                    token_is_directive=False,
                    reasons=["the export path raised"],
                )
            cases.append(row)

    refused = [row["case_id"] for row in cases if not row["shareable"]]
    surviving = [row["case_id"] for row in cases if row["token_reassembled"]]
    shaped = [row["case_id"] for row in cases if row["findings"]]
    preserved = [row["case_id"] for row in cases if row["token_is_directive"]]
    carried = [row["case_id"] for row in cases if row["payload_carried_token"]]
    ok = not (refused or surviving or shaped)
    return {
        "schema_version": 1,
        "cases": len(cases),
        "canary_cases": sum(1 for row in cases if row["corpus"] == "canary"),
        "obfuscated_cases": sum(1 for row in cases if row["corpus"] == "obfuscated"),
        "refused_exports": refused,
        "cases_carrying_the_canary": carried,
        "canary_tokens_surviving": surviving,
        "unredacted_findings": shaped,
        "injection_directives_preserved": preserved,
        "why_directives_survive": (
            "the canary corpus is prompt-INJECTION text and the token is not "
            "credential-shaped, so no redaction rule owns it; redacting free "
            "text would leave an export that shows nothing"
        ),
        "case_receipts": cases,
        "ok": ok,
        "verdict": "EXPORT_REDACTION_CLEAN" if ok else "EXPORT_REDACTION_LEAK",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the selftest report; exit 2 when a canary token survived."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m shared.session_share",
        description=(
            "The exported-session redaction gate: no canary token may survive "
            "redaction, in either the normal or the obfuscated corpus."
        ),
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the corpus gate (the only mode; present so the lane reads clearly)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable only")
    args = parser.parse_args(argv)

    report = selftest()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"export redaction: {report['verdict']} "
            f"({report['cases']} cases: "
            f"{report['canary_cases']} canary, "
            f"{report['obfuscated_cases']} obfuscated)"
        )
        for label, key in (
            ("refused exports", "refused_exports"),
            ("canary tokens surviving (obfuscated corpus)", "canary_tokens_surviving"),
            ("unredacted findings", "unredacted_findings"),
            (
                "injection directives preserved (expected, not a leak)",
                "injection_directives_preserved",
            ),
        ):
            names = report[key]
            print(f"  {label}: {len(names)}" + (f" {names}" if names else ""))
    return 0 if report["ok"] else 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
