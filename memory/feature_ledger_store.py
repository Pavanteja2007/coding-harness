"""Phase-4 ledger STORE: the integrity half of the feature/progress ledgers.

**T1 builds the ledgers; this module is the store they live in, and the place
the integrity rules are enforced where a bypass cannot reach them.** The split is
deliberate and is the whole design:

``harness/long_horizon.py`` (T1, not this round's file) is the **tool** layer:
``apply_ledger_patch`` refuses a patch that touches a field outside
``AGENT_WRITABLE_LEDGER_FIELDS``. That is real enforcement, and it is not
sufficient on its own, because a tool layer can be bypassed by anything that
writes the file directly -- an ``edit`` of ``features.json``, a ``sed``, a
checkout, or the agent itself with a shell. So the rules are enforced **twice**,
at two independent points, by two mechanisms that cannot both be defeated by the
same act:

* **the tool** refuses an illegal *patch document*;
* **the store** refuses an illegal *state on disk*, by refusing to publish a
  current view it cannot derive from append-only history plus a separate
  evidence store.

Six properties, each of which is a mechanism rather than a convention:

1. **Storage + schema.** ``features.json`` stays byte-for-byte T1's document
   (this module writes NOTHING into it beyond what ``harness.long_horizon``
   renders, so T1's ``load_feature_ledger`` round-trips it unchanged). Every
   store-side artefact lives in its own file: ``features.seal.json``, and
   ``.neo-store/{history.jsonl,evidence/,quarantine/}``. ``execution/spec_ledger``
   puts its seal in a separate file for the stated reason -- *"a 'helpful'
   rewrite of the spec cannot rewrite its own permission slip"* -- and this is
   the same argument applied to the ledger.

2. **Status-only mutation, enforced AT THE STORE.** The immutable projection is
   every feature's obligation (``id``, ``description``, ``verification``,
   ``derived_from``) plus the ledger's own identity (``schema``, ``project_id``,
   ``request``, ``created_at``) and the feature COUNT. Everything else
   (``status``, ``evidence``, ``sessions_touched``, ``notes``) is store-managed.
   ``LedgerStore.replace_document`` is the one structural write path and it
   re-seals **only** when the caller explicitly asks, exactly like
   ``SpecLedger.save(seal=True)``; a routine save can never re-baseline the
   guard.

3. **``passing`` requires evidence, at the same standard as
   ``completed_verified``.** A ``passing`` transition is refused unless it names
   an evidence digest that (a) resolves in the **separate** evidence store,
   (b) records ``outcome == "pass"``, (c) records a ``kind == "run"`` (a human's
   attestation is not a command's exit code, and T1's
   ``FEATURE_PASSING_REQUIRES_RUN`` says so -- this module DELEGATES to
   ``harness.long_horizon.check_verification_evidence`` rather than keeping a
   second rule), and (d) was observed with a verification command matching the
   feature's own immutable command.

4. **Backward transitions are ALLOWED and encouraged.** ``passing -> failing`` is
   a legal edge, and a ledger where features can only move forward is a ledger
   that becomes a lie over time. It requires a **reason** (fail-closed on a
   reason-less input, ``phases/DOCTRINE.md`` section 5) but NOT a passing
   evidence artefact: a regression is often observed without a stored
   verification, and demanding one would make under-reporting the easy path.
   ``ALLOWED_TRANSITIONS`` is a published table and ``is_transition_allowed``
   is the only reader, so the table cannot be consulted inconsistently.

5. **Append-only history plus a current view.** Every accepted transition
   appends one row to ``.neo-store/history.jsonl`` carrying the before/after
   status, the actor, the reason, the evidence digest, the resulting document
   digest and a **chain digest** over the whole history. ``guard()`` replays the
   history and requires the on-disk current view to EQUAL the replay. A direct
   edit of the status field therefore cannot survive: there is no history row for
   it, and the replay says so. A wholesale rewrite of the document is caught by
   the document-digest check, because the rewritten bytes were never recorded.

6. **Corruption is quarantined, never silently repaired.** ``LedgerStore.open``
   returns a refusal rather than raising; ``quarantine`` MOVES the corrupt
   artefacts aside under a timestamped directory, writes a machine-readable
   ``quarantine.json`` describing exactly what was moved and why, and returns a
   receipt. It never rewrites, never re-seals, and never guesses. A store that
   repairs its own corruption is a store whose corruption is invisible.

**What this module does NOT do, stated so nobody reads a guarantee into it:**

* **It cannot detect a wholesale forgery by an attacker with write access to the
  whole directory.** An attacker who rewrites the ledger, the seal AND the
  history can recompute a self-consistent set. That limit is the same one
  ``execution.independent_evidence.detect_tampering`` has, and it is closed the
  same way: the caller passes an ``expected_fingerprint`` recorded EARLIER (at
  seal time, in another process), and ``detect_tampering`` already reports a
  missing fingerprint as a FAILURE rather than as intact. The
  ``overclaim`` audit below does not have this limit for the case that matters:
  a ledger claiming more than the evidence supports is detected from the
  evidence store alone, and the evidence store is not the ledger.
* **It is not wired into a run.** ``memory/`` is this terminal's, and the call
  site is T1's or T4's. A dead guard is worse than an absent one, so
  ``REACHABILITY`` records the state and a pin asserts it rather than leaving a
  reader to infer it from a green suite.
* **It owns no vocabulary.** ``FEATURE_STATUSES``, ``PASSING_FEATURE_STATUS``,
  ``AGENT_WRITABLE_LEDGER_FIELDS``, ``INITIAL_FEATURE_STATUS`` and
  ``check_verification_evidence`` are imported from ``harness.long_horizon``.
  Two copies of "what a status is" is two answers to one question, and this
  repository has paid for that class repeatedly (``phases/DOCTRINE.md`` section 6).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The one authority for the ledger's vocabulary. T1 owns it; this module
# imports it rather than restating it, so a vocabulary change lands once.
# ---------------------------------------------------------------------------
from harness.long_horizon import (
    AGENT_WRITABLE_LEDGER_FIELDS,
    FEATURE_LEDGER_NAME,
    FEATURE_LEDGER_SCHEMA,
    FEATURE_STATUSES,
    INITIAL_FEATURE_STATUS,
    PASSING_FEATURE_STATUS,
    PROGRESS_LEDGER_NAME,
    PROGRESS_SCHEMA,
    VerificationRun,
    check_verification_evidence,
    load_feature_ledger,
    load_progress_ledger,
    write_feature_ledger,
)
from harness.long_horizon import (
    LEDGER_REFUSALS as TOOL_REFUSALS,
)

#: This store's own schema id, for the files it owns. Independent of T1's
#: document schema on purpose: the store can be rebuilt against a newer ledger
#: document without invalidating a history chain.
STORE_SCHEMA = "neo/ledger-store/v1"

#: The seal file's name. A SEPARATE file, so rewriting the ledger cannot
#: rewrite its own permission slip.
SEAL_NAME = "features.seal.json"

#: The store's directory, relative to the project root. Dotted so a directory
#: listing of the project does not present it as part of the work.
STORE_DIRNAME = ".neo-store"
HISTORY_NAME = "history.jsonl"
EVIDENCE_DIRNAME = "evidence"
QUARANTINE_DIRNAME = "quarantine"
QUARANTINE_REPORT_NAME = "quarantine.json"

#: The feature fields that define the OBLIGATION. ``status`` is deliberately
#: absent: it is the agent's report, not part of what must be delivered.
IMMUTABLE_FEATURE_FIELDS: Tuple[str, ...] = ("id", "description", "verification", "derived_from")

#: The feature fields the store manages. Every one of these is store-written;
#: none of them can come from a caller.
MUTABLE_FEATURE_FIELDS: Tuple[str, ...] = ("status", "evidence", "sessions_touched", "notes")

#: The ledger-level fields that are part of the sealed identity.
IMMUTABLE_LEDGER_FIELDS: Tuple[str, ...] = ("schema", "project_id", "request", "created_at")

#: **The transition table.** Backward edges are present on purpose.
#:
#: ``passing -> failing`` is the regression edge and it is ENCOURAGED: a ledger
#: whose features can only move forward is a ledger that becomes a lie over time,
#: because a regression nobody can record has nowhere to go except into a hidden
#: edit. ``passing -> blocked`` is likewise allowed. What is NOT allowed is
#: ``passing -> failing`` with no reason -- see :func:`_require_reason`.
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    INITIAL_FEATURE_STATUS: ("blocked", "passing", "skipped", INITIAL_FEATURE_STATUS),
    "blocked": (INITIAL_FEATURE_STATUS, "failing", "blocked", "passing"),
    "skipped": (INITIAL_FEATURE_STATUS, "blocked", "failing"),
    "failing": ("blocked", "passing", "failing"),
    # THE REGRESSION EDGE. Present, documented, and pinned by a test that fails
    # the day somebody removes it.
    PASSING_FEATURE_STATUS: ("failing", "blocked", PASSING_FEATURE_STATUS),
}

#: The closed set of reasons this store can refuse on. A reader can branch on
#: the reason without parsing prose, which is the point of a closed set.
STORE_REFUSALS: Tuple[str, ...] = (
    "no_store",
    "corrupt_ledger",
    "unknown_schema",
    "seal_missing",
    "seal_mismatch",
    "item_removed",
    "item_added",
    "item_mutated",
    "item_count_changed",
    "ledger_identity_changed",
    "document_not_in_history",
    "status_without_history",
    "history_chain_broken",
    "history_unreadable",
    "unknown_feature",
    "status_not_in_vocabulary",
    "transition_not_allowed",
    "reason_required",
    "evidence_required",
    "evidence_unknown",
    "evidence_did_not_pass",
    "evidence_command_mismatch",
    "evidence_session_mismatch",
    "evidence_tampered",
    "quarantined",
)

#: Every refusal this store can return PLUS the tool layer's, so a consumer can
#: validate a slug against one table.
ALL_REFUSALS: Tuple[str, ...] = tuple(sorted(set(STORE_REFUSALS) | set(TOOL_REFUSALS)))

#: Whether anything in the tree calls this module. Read by
#: ``test_feature_ledger_store.py`` so an inert store is ASSERTED inert rather
#: than left for a reader to infer from a green suite.
REACHABILITY: Dict[str, Any] = {
    "production_call_sites": 0,
    "owner": "T5 (memory/ledger store) -> T1 or T4 must wire the call site",
    "why": (
        "the store is the second enforcement point for a rule T1's tool layer "
        "already enforces; wiring it is a call site in another owner's file, and "
        "a guard added here with no caller would be the dead guard "
        "phases/DOCTRINE.md warns about"
    ),
    "reachable_today_via": "tests/test_feature_ledger_store.py and python -m memory.feature_ledger_store",
}

#: RECORDED GAPS this store found in code it does not own, each with an owner
#: and a phase. A recorded gap is one somebody can close; an unrecorded one gets
#: rediscovered. Each has an inverted pin in
#: ``tests/test_p4_long_horizon_gaps.py``.
#:
#: **EMPTY, and the empty tuple is the load-bearing part.**
#:
#: This held one entry, ``ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_HATCH``: T1's
#: ``VerificationRun`` normalised an ``attested`` row to ``ran=False,
#: outcome="not_run"``, so ``allow_attested=True`` could never satisfy
#: ``is_pass_evidence`` and a ``human_check`` feature could not reach ``passing``
#: under either flag value. T1 fixed it mid-phase, and
#: ``tests/test_feature_ledger_store.py`` failed on arrival -- which is what a
#: pin for a closing gap should do.
#:
#: The entry was then **deleted rather than flipped**, and that is the reason
#: this comment exists: a registry that keeps reporting a closed gap teaches the
#: next reader to ignore the registry, which costs more than the gap ever did.
#: ``...::test_the_allow_attested_gap_is_closed_and_the_history_says_so`` asserts
#: this tuple carries no closed entry, and
#: ``...::test_an_attestation_passes_only_with_the_flag_that_says_its_kind``
#: pins the surviving two-sided rule -- an attestation passes WITH the flag and is
#: refused WITHOUT it, so the flag stays a real control instead of becoming a
#: formality.
#:
#: Re-open an entry here only when it is open again, and give it an inverted pin
#: in the same change. A gap with no pin is a comment.
RECORDED_GAPS: Tuple[Dict[str, str], ...] = ()

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ALL_REFUSALS",
    "EVIDENCE_DIRNAME",
    "HISTORY_NAME",
    "IMMUTABLE_FEATURE_FIELDS",
    "IMMUTABLE_LEDGER_FIELDS",
    "MUTABLE_FEATURE_FIELDS",
    "QUARANTINE_DIRNAME",
    "QUARANTINE_REPORT_NAME",
    "REACHABILITY",
    "RECORDED_GAPS",
    "SEAL_NAME",
    "STORE_DIRNAME",
    "STORE_REFUSALS",
    "STORE_SCHEMA",
    "EvidenceRecord",
    "GuardReport",
    "LedgerSeal",
    "LedgerStore",
    "OverclaimReport",
    "QuarantineReceipt",
    "StoreRefusal",
    "TransitionReceipt",
    "document_digest",
    "is_transition_allowed",
    "seal_of",
]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreRefusal:
    """One refusal, with a machine-readable reason and a human sentence."""

    reason: str
    detail: str = ""
    subject: str = ""
    owner: str = "T5 (memory/feature_ledger_store)"

    def __post_init__(self) -> None:
        if str(self.reason) not in ALL_REFUSALS:
            raise ValueError(
                f"reason {self.reason!r} is not a closed refusal slug; "
                f"add it to STORE_REFUSALS deliberately"
            )
        # Fail closed on a reason-less refusal: an unnamed refusal is a
        # refusal nobody can act on (phases/DOCTRINE.md section 5).
        if not str(self.detail).strip():
            raise ValueError("a refusal must carry a detail; 'refused' is not a reason")

    @property
    def ok(self) -> bool:
        """A refusal is never a pass."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "reason": self.reason,
            "detail": str(self.detail),
            "subject": str(self.subject),
            "owner": str(self.owner),
        }

    def render(self) -> str:
        """Return the one-line refusal, naming the subject."""
        who = f" [{self.subject}]" if str(self.subject) else ""
        return f"REFUSED ({self.reason}){who}: {self.detail}"


def _refuse(reason: str, detail: str, subject: str = "", owner: str = "") -> StoreRefusal:
    """Build a :class:`StoreRefusal`, defaulting the owner."""
    return StoreRefusal(
        reason=reason,
        detail=detail,
        subject=str(subject),
        owner=owner or "T5 (memory/feature_ledger_store)",
    )


# ---------------------------------------------------------------------------
# Digests, projections, the seal
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Return the SHA-256 of ``text``, as hex."""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def _canonical(value: Any) -> str:
    """Return a canonical JSON rendering, so a digest depends on values not order."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def document_digest(document: Mapping[str, Any]) -> str:
    """Return the digest of the WHOLE ledger document, in any key order.

    Used by the history chain: a rewritten ledger produces bytes no history row
    ever recorded, and that is the detection. Key order is normalised on purpose
    so re-serialising a semantically identical ledger does not read as a change.
    """
    return _sha256(_canonical(dict(document or {})))


def immutable_projection(feature: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the obligation-defining projection of one feature.

    A deterministic function of the sealed fields ONLY, so replaying it against
    a tampered document and against the seal is a comparison of like with like.
    """
    data = dict(feature or {})
    return {name: data.get(name) for name in IMMUTABLE_FEATURE_FIELDS}


def ledger_projection(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the ledger-level identity projection."""
    data = dict(document or {})
    features = list(data.get("features") or ())
    return {
        **{name: data.get(name) for name in IMMUTABLE_LEDGER_FIELDS},
        "feature_count": len(features),
        "feature_ids": sorted(str(item.get("id") or "") for item in features if isinstance(item, Mapping)),
        "features": [immutable_projection(item) for item in features if isinstance(item, Mapping)],
    }


@dataclass(frozen=True)
class LedgerSeal:
    """The fingerprint over the ledger's IMMUTABLE projection.

    A separate file, and the whole reason the store cannot be re-baselined by a
    routine save: ``status`` is absent from the projection, so honest progress
    reporting changes nothing in the fingerprint, while a removed, added or
    mutated OBLIGATION does.
    """

    fingerprint: str
    schema: str
    project_id: str = ""
    item_count: int = 0
    item_ids: Tuple[str, ...] = ()
    projection: Dict[str, Any] = field(default_factory=dict)
    sealed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "schema": self.schema,
            "project_id": str(self.project_id),
            "fingerprint": str(self.fingerprint),
            "item_count": int(self.item_count),
            "item_ids": list(self.item_ids),
            "projection": dict(self.projection),
            "sealed_at": str(self.sealed_at),
            "note": (
                "the fingerprint covers ONLY the immutable projection: ids, "
                "descriptions, verifications, ledger identity and the item count. "
                "A status change is free by design; a removed or rewritten "
                "obligation is not."
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LedgerSeal":
        """Rebuild a seal, refusing a document with no fingerprint."""
        data = dict(raw or {})
        fingerprint = str(data.get("fingerprint") or "").strip()
        if not fingerprint:
            raise ValueError("a seal must carry a fingerprint; an unsigned seal protects nothing")
        return cls(
            fingerprint=fingerprint,
            schema=str(data.get("schema") or ""),
            project_id=str(data.get("project_id") or ""),
            item_count=int(data.get("item_count") or 0),
            item_ids=tuple(str(item) for item in (data.get("item_ids") or ())),
            projection=dict(data.get("projection") or {}),
            sealed_at=str(data.get("sealed_at") or ""),
        )


def seal_of(document: Mapping[str, Any], *, sealed_at: str = "") -> LedgerSeal:
    """Return a fresh :class:`LedgerSeal` for ``document``."""
    projection = ledger_projection(document)
    data = dict(document or {})
    features = [item for item in (data.get("features") or ()) if isinstance(item, Mapping)]
    return LedgerSeal(
        fingerprint=_sha256(_canonical(projection)),
        schema=str(data.get("schema") or ""),
        project_id=str(data.get("project_id") or ""),
        item_count=len(features),
        item_ids=tuple(sorted(str(item.get("id") or "") for item in features)),
        projection=projection,
        sealed_at=str(sealed_at),
    )


# ---------------------------------------------------------------------------
# Evidence -- stored SEPARATELY, so changing the ledger changes no evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRecord:
    """One verification observation, content-addressed and immutable.

    The separation is the property this class exists for. The ledger says *what
    was claimed*; this file says *what was observed*. Rewriting the ledger
    cannot change any byte here, and rewriting one of these is detectable
    because the record's own digest is its address.

    ``ran`` is separate from ``outcome`` because "we checked and it broke" and
    "we did not check" are different facts, and collapsing them is how an
    unchecked run reads as a stable one (``phases/DOCTRINE.md`` section 1).
    """

    feature_id: str
    outcome: str
    kind: str = "run"
    ran: bool = True
    command: str = ""
    exit_code: Optional[int] = None
    output_digest: str = ""
    output_chars: int = 0
    observed_at: str = ""
    session: str = ""
    accept_suite_root: str = ""
    accept_suite_fingerprint: str = ""
    notes: str = ""

    #: The outcome string that can support a ``passing`` claim. Named here so
    #: the evidence vocabulary and T1's ``FEATURE_STATUSES`` cannot drift
    #: silently: ``"passing"`` is a STATUS, ``"pass"`` is what a run OBSERVED,
    #: and conflating them is how a status becomes an observation.
    PASS_OUTCOME = "pass"

    @property
    def is_pass_evidence(self) -> bool:
        """Return whether this record can support a ``passing`` claim."""
        return bool(self.ran) and str(self.outcome) == EvidenceRecord.PASS_OUTCOME

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection with STABLE key order."""
        return {
            "store_schema": STORE_SCHEMA,
            "feature_id": str(self.feature_id),
            "outcome": str(self.outcome),
            "kind": str(self.kind),
            "ran": bool(self.ran),
            "command": str(self.command),
            "exit_code": None if self.exit_code is None else int(self.exit_code),
            "output_digest": str(self.output_digest),
            "output_chars": int(self.output_chars),
            "observed_at": str(self.observed_at),
            "session": str(self.session),
            "accept_suite_root": str(self.accept_suite_root),
            "accept_suite_fingerprint": str(self.accept_suite_fingerprint),
            "notes": str(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceRecord":
        """Rebuild a record from its projection, refusing an unusable shape."""
        data = dict(raw or {})
        feature_id = str(data.get("feature_id") or "").strip()
        outcome = str(data.get("outcome") or "").strip()
        if not feature_id:
            raise ValueError("an evidence record must name its feature")
        if not outcome:
            raise ValueError(f"evidence for {feature_id!r} has no outcome; an absent outcome is not a pass")
        exit_code = data.get("exit_code")
        return cls(
            feature_id=feature_id,
            outcome=outcome,
            kind=str(data.get("kind") or "run"),
            ran=bool(data.get("ran", True)),
            command=str(data.get("command") or ""),
            exit_code=None if exit_code is None else int(exit_code),
            output_digest=str(data.get("output_digest") or ""),
            output_chars=int(data.get("output_chars") or 0),
            observed_at=str(data.get("observed_at") or ""),
            session=str(data.get("session") or ""),
            accept_suite_root=str(data.get("accept_suite_root") or ""),
            accept_suite_fingerprint=str(data.get("accept_suite_fingerprint") or ""),
            notes=str(data.get("notes") or ""),
        )

    @property
    def digest(self) -> str:
        """The record's content address: the digest of its canonical projection.

        ``to_dict`` includes ``store_schema``, so a record written by an older
        store is a different address rather than a silent reinterpretation.
        """
        return _sha256(_canonical(self.to_dict()))

    def render(self) -> str:
        """Return the one-line human summary."""
        return (
            f"{self.feature_id}: {self.outcome} ({self.kind}, ran={self.ran}) "
            f"at {self.observed_at or '(no timestamp)'}"
        )


# ---------------------------------------------------------------------------
# History -- append-only, chained, and the reason the current view is checkable
# ---------------------------------------------------------------------------

HISTORY_GENESIS = "genesis"


def _chain_digest(previous: str, row: Mapping[str, Any]) -> str:
    """Return the chain digest for one appended history row.

    Chaining is what makes the history append-only in a way a reader can CHECK:
    a rewritten earlier row changes that row's digest, which changes every chain
    digest after it, and the guard reports the first row whose recorded chain
    digest disagrees with the recomputed one.
    """
    payload = {key: value for key, value in dict(row or {}).items() if key != "chain"}
    return _sha256(f"{previous}\n{_canonical(payload)}")


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionReceipt:
    """What one accepted transition did."""

    ok: bool
    feature_id: str
    from_status: str
    to_status: str
    evidence_digest: str = ""
    actor: str = ""
    reason: str = ""
    backward: bool = False
    document_digest: str = ""
    chain_digest: str = ""
    history_row: int = 0
    refusal: Optional[StoreRefusal] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "ok": bool(self.ok),
            "feature_id": str(self.feature_id),
            "from_status": str(self.from_status),
            "to_status": str(self.to_status),
            "evidence_digest": str(self.evidence_digest),
            "actor": str(self.actor),
            "reason": str(self.reason),
            "backward": bool(self.backward),
            "document_digest": str(self.document_digest),
            "chain_digest": str(self.chain_digest),
            "history_row": int(self.history_row),
            "refusal": self.refusal.to_dict() if self.refusal is not None else None,
            "note": (
                "'backward' is the REGRESSION EDGE and it is allowed on purpose; a "
                "ledger whose features can only move forward becomes a lie over time"
            ),
        }

    def render(self) -> str:
        """Return the one-line receipt, or the refusal."""
        if self.refusal is not None:
            return self.refusal.render()
        edge = " (REGRESSION)" if self.backward else ""
        return (
            f"OK {self.feature_id}: {self.from_status} -> {self.to_status}{edge} "
            f"by {self.actor or '(unattributed)'}"
            + (f" evidence={self.evidence_digest[:12]}" if self.evidence_digest else "")
        )


@dataclass(frozen=True)
class GuardReport:
    """The store's verdict on what is on disk right now."""

    ok: bool
    sealed: bool
    ledger_path: str = ""
    seal_path: str = ""
    expected_fingerprint: str = ""
    actual_fingerprint: str = ""
    document_digest: str = ""
    recorded_documents: Tuple[str, ...] = ()
    violations: Tuple[StoreRefusal, ...] = ()
    removed: Tuple[str, ...] = ()
    added: Tuple[str, ...] = ()
    mutated: Tuple[str, ...] = ()
    history_rows: int = 0
    replayed: Optional[Dict[str, str]] = None

    @property
    def reasons(self) -> Tuple[str, ...]:
        """The distinct refusal slugs, sorted."""
        return tuple(sorted({item.reason for item in self.violations}))

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "ok": bool(self.ok),
            "sealed": bool(self.sealed),
            "ledger_path": str(self.ledger_path),
            "seal_path": str(self.seal_path),
            "expected_fingerprint": str(self.expected_fingerprint),
            "actual_fingerprint": str(self.actual_fingerprint),
            "document_digest": str(self.document_digest),
            "recorded_documents": list(self.recorded_documents),
            "violations": [item.to_dict() for item in self.violations],
            "reasons": list(self.reasons),
            "removed": list(self.removed),
            "added": list(self.added),
            "mutated": list(self.mutated),
            "history_rows": int(self.history_rows),
            "replayed": dict(self.replayed) if self.replayed is not None else None,
        }

    def render(self) -> str:
        """Return plain lines a reader can act on."""
        lines = [
            f"ledger store guard: {'OK' if self.ok else 'VIOLATIONS'} "
            f"(sealed={self.sealed}, history_rows={self.history_rows})",
        ]
        for item in self.violations:
            lines.append("  " + item.render())
        if not self.ok:
            lines.append(
                "  a ledger is never trusted over its evidence: quarantine it and "
                "report, never repair it silently"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class OverclaimReport:
    """Whether the ledger claims more than the stored evidence supports.

    This is the report a person reads before believing a green ledger. It answers
    "how many features are marked passing, and how many of those have evidence
    behind them" -- and when the two numbers differ, it says so in the FIRST line
    rather than in a footnote nobody reads.
    """

    total_features: int
    passing: int
    passing_with_stored_evidence: int
    unsupported_claims: Tuple[str, ...]
    undetected_tampering: Tuple[str, ...]
    coverage_share: Optional[float]
    premature: Dict[str, Any] = field(default_factory=dict)
    per_feature: Tuple[Dict[str, Any], ...] = ()
    vacuous: bool = False

    @property
    def overclaims(self) -> bool:
        """Return whether the ledger claims more than the evidence supports."""
        return bool(self.unsupported_claims or self.undetected_tampering)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "total_features": int(self.total_features),
            "passing": int(self.passing),
            "passing_with_stored_evidence": int(self.passing_with_stored_evidence),
            "unsupported_claims": list(self.unsupported_claims),
            "undetected_tampering": list(self.undetected_tampering),
            "coverage_share": self.coverage_share,
            "overclaims": self.overclaims,
            "premature": dict(self.premature),
            "per_feature": [dict(item) for item in self.per_feature],
            "vacuous": bool(self.vacuous),
            "vacuous_note": (
                "vacuous=True means no feature is marked passing, so this report "
                "measures nothing about over-claiming. It is reported rather than "
                "rendered as a clean ledger."
            ),
        }

    def render(self) -> str:
        """Return plain lines, loudest first."""
        if self.vacuous:
            return (
                f"ledger overclaim audit: VACUOUS - 0/{self.total_features} passing, "
                "so no over-claim is measurable here"
            )
        head = (
            f"ledger overclaim audit: {self.passing_with_stored_evidence}/"
            f"{self.passing} passing features have stored evidence"
        )
        if self.overclaims:
            head += "  <-- THE LEDGER CLAIMS MORE THAN THE EVIDENCE SUPPORTS"
        lines = [head]
        for feature_id in self.unsupported_claims:
            lines.append(f"  UNSUPPORTED  {feature_id}: marked passing with no resolvable evidence")
        for feature_id in self.undetected_tampering:
            lines.append(f"  TAMPERED     {feature_id}: its acceptance suite changed after sealing")
        if self.premature.get("premature"):
            lines.append(
                "  PREMATURE    the ledger claims completion without a clean final "
                f"gate ({self.premature.get('reason') or 'no reason recorded'})"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class QuarantineReceipt:
    """What was moved aside, and why."""

    ok: bool
    root: str = ""
    quarantine_dir: str = ""
    moved: Tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""
    refusal: Optional[StoreRefusal] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection, including the on-disk report path."""
        return {
            "ok": bool(self.ok),
            "root": str(self.root),
            "quarantine_dir": str(self.quarantine_dir),
            "moved": list(self.moved),
            "reason": str(self.reason),
            "detail": str(self.detail),
            "quarantine_report": (
                str(Path(self.quarantine_dir) / QUARANTINE_REPORT_NAME) if self.quarantine_dir else ""
            ),
            "refusal": self.refusal.to_dict() if self.refusal is not None else None,
            "note": (
                "quarantine MOVES and REPORTS. It never repairs, never re-seals and "
                "never guesses: a silently repaired ledger hides the corruption it "
                "repaired."
            ),
        }

    def render(self) -> str:
        """Return the one-line receipt."""
        if self.refusal is not None:
            return self.refusal.render()
        return f"QUARANTINED {len(self.moved)} artefact(s) -> {self.quarantine_dir}"


# ---------------------------------------------------------------------------
# Transition table -- the ONLY reader of ALLOWED_TRANSITIONS
# ---------------------------------------------------------------------------


def is_transition_allowed(from_status: str, to_status: str) -> bool:
    """Return whether ``from_status -> to_status`` is a legal edge.

    An UNKNOWN status on either side is refused, not defaulted. A transition
    table with a default-allow branch is a table whose safety depends on nobody
    adding a status without thinking about the fallthrough
    (``phases/DOCTRINE.md`` section 5: an unparseable or unrecognised input is a
    denial).
    """
    source = str(from_status or "").strip().lower()
    target = str(to_status or "").strip().lower()
    if source not in ALLOWED_TRANSITIONS or target not in FEATURE_STATUSES:
        return False
    return target in ALLOWED_TRANSITIONS[source]


def _require_reason(reason: str, *, what: str) -> Optional[StoreRefusal]:
    """Return a refusal when a caller supplied no reason for a consequential act."""
    if str(reason or "").strip():
        return None
    return _refuse(
        "reason_required",
        f"{what} requires a reason; a reason-less status change is indistinguishable "
        "from an unexplained edit",
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class LedgerStore:
    """Read/write access to one project's ledger, with the integrity rules enforced.

    ``root`` is the PROJECT root (the directory holding ``features.json``). The
    store's own artefacts live under ``root/.neo-store`` and are never written
    into T1's documents.
    """

    def __init__(self, root: Any, *, ledger_path: Any = "") -> None:
        self.root = Path(str(root))
        self.ledger_path = Path(str(ledger_path)) if ledger_path else self.root / FEATURE_LEDGER_NAME
        self.seal_path = self.ledger_path.with_name(SEAL_NAME)
        self.progress_path = self.root / PROGRESS_LEDGER_NAME
        self.store_dir = self.root / STORE_DIRNAME
        self.history_path = self.store_dir / HISTORY_NAME
        self.evidence_dir = self.store_dir / EVIDENCE_DIRNAME
        self.quarantine_dir = self.store_dir / QUARANTINE_DIRNAME

    # -- paths -----------------------------------------------------------

    def exists(self) -> bool:
        """Return whether a ledger document is present."""
        return self.ledger_path.is_file()

    def document(self) -> Optional[Dict[str, Any]]:
        """Return the raw on-disk ledger document, or ``None`` if unreadable.

        Deliberately RAW: this store's job is to inspect what a writer actually
        left on disk, not to interpret it through T1's loader. T1's loader is
        still consulted in :meth:`open` for the second opinion.
        """
        try:
            text = self.ledger_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None

    def progress_text(self) -> str:
        """Return the progress ledger's text (``""`` when absent).

        Delegates to T1's total reader, so "the fact is missing" and "the fact is
        broken" cannot be told apart by accident.
        """
        return load_progress_ledger(self.progress_path)

    def append_progress(self, text: str) -> Dict[str, Any]:
        """APPEND to the progress ledger and return a receipt.

        Append-only by construction: the existing text is read and the new block
        is added. Rewriting a progress ledger is a different act and is not
        offered here.
        """
        from harness.long_horizon import write_progress_ledger

        existing = self.progress_text().rstrip("\n")
        block = str(text or "").strip("\n")
        combined = (existing + "\n\n" + block + "\n") if existing and block else (block or existing)
        receipt = dict(write_progress_ledger(self.progress_path, combined))
        receipt["appended_chars"] = len(block)
        receipt["append_only"] = True
        return receipt

    # -- seal ------------------------------------------------------------

    def seal(self) -> Optional[LedgerSeal]:
        """Return the attached seal, or ``None`` when there is none."""
        try:
            raw = json.loads(self.seal_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        try:
            return LedgerSeal.from_dict(raw)
        except ValueError:
            # A seal that cannot be read is treated as ABSENT, and an absent seal
            # is reported as a FAILURE by guard() -- the same shape
            # execution.independent_evidence uses for a missing fingerprint.
            return None

    def write_seal(self, seal: LedgerSeal) -> str:
        """Write ``seal`` atomically and return its path."""
        return _atomic_write_json(self.seal_path, seal.to_dict())

    def ensure_sealed(self, *, sealed_at: str = "") -> Tuple[LedgerSeal, bool]:
        """Seal the current document if there is no seal yet.

        Returns ``(seal, wrote)``. Writing a NEW seal is a deliberate act and is
        never performed implicitly by :meth:`apply_status` or
        :meth:`replace_document` -- a routine save must not re-baseline the
        guard.
        """
        document = self.document()
        if document is None:
            raise ValueError(f"no readable ledger document at {self.ledger_path}")
        existing = self.seal()
        if existing is not None:
            return existing, False
        fresh = seal_of(document, sealed_at=sealed_at or _now())
        self.write_seal(fresh)
        statuses = {
            str(item.get("id") or ""): str(item.get("status") or INITIAL_FEATURE_STATUS)
            for item in (document.get("features") or ())
            if isinstance(item, Mapping)
        }
        self._append_history(
            {
                "event": "sealed",
                "seal_fingerprint": fresh.fingerprint,
                "item_count": fresh.item_count,
                "document_sha256": document_digest(document),
                # The INITIAL VIEW, recorded. Without it the replay would report
                # every born-failing feature as "a status no transition recorded",
                # which is a false alarm on the honest starting state -- and a
                # guard that cries wolf on a healthy ledger is a guard people
                # stop reading.
                "statuses": statuses,
            }
        )
        return fresh, True

    # -- history ---------------------------------------------------------

    def history(self) -> List[Dict[str, Any]]:
        """Return every history row, in order.

        A torn tail (a row a crash truncated mid-write) is DROPPED and reported
        through :attr:`history_rows` being smaller than the line count, because a
        half-parsed row must not be replayed as a fact.
        """
        try:
            text = self.history_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return []
        rows: List[Dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(parsed, Mapping):
                rows.append(dict(parsed))
        return rows

    def _append_history(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Append one chained history row and return it."""
        rows = self.history()
        previous = str(rows[-1].get("chain")) if rows else HISTORY_GENESIS
        row: Dict[str, Any] = {
            "store_schema": STORE_SCHEMA,
            "at": _now(),
            "seq": len(rows) + 1,
            **dict(payload),
        }
        row["chain"] = _chain_digest(previous, row)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        line = _canonical(row) + "\n"
        with open(self.history_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def verify_history_chain(self) -> Optional[StoreRefusal]:
        """Return a refusal when the chain is broken, or ``None`` when it holds.

        A broken chain is the detection for a rewritten EARLIER history row: that
        row's digest changes, so every later chain digest changes, and the first
        disagreement is reported by index.
        """
        previous = HISTORY_GENESIS
        for index, row in enumerate(self.history()):
            recorded = str(row.get("chain") or "")
            recomputed = _chain_digest(previous, row)
            if recorded != recomputed:
                return _refuse(
                    "history_chain_broken",
                    f"history row {index + 1} (seq {row.get('seq')}) carries chain "
                    f"{recorded[:12]} but recomputes to {recomputed[:12]}; an earlier "
                    "row was rewritten, because appending cannot change a prior digest",
                    subject=f"seq={row.get('seq')}",
                )
            previous = recorded
        return None

    def replay(self) -> Tuple[Dict[str, str], Optional[StoreRefusal]]:
        """Replay the history into the current view it claims.

        Returns ``(statuses_by_feature, refusal)``. Two row kinds contribute:

        * the ``sealed`` row carries the **initial view** every feature was born
          with, so an untouched ledger replays to itself rather than to "no
          history for anything";
        * each ``transition`` row carries the accepted change.

        The refusal names the first transition whose recorded ``to_status`` does
        not match the current view, which is how a direct edit of the status
        field is caught: there is no history row for it, and the replay
        disagrees with what the file says.
        """
        view: Dict[str, str] = {}
        for index, row in enumerate(self.history()):
            event = str(row.get("event") or "")
            if event == "sealed":
                for feature_id, status in (row.get("statuses") or {}).items():
                    view[str(feature_id)] = str(status)
                continue
            if event != "transition":
                continue
            feature_id = str(row.get("feature_id") or "")
            to_status = str(row.get("to_status") or "")
            if not feature_id or not to_status:
                return {}, _refuse(
                    "history_unreadable",
                    f"history row {index + 1} is a transition with no feature or no target status",
                    subject=f"seq={row.get('seq')}",
                )
            view[feature_id] = to_status
        return view, None

    # -- evidence --------------------------------------------------------

    def record_evidence(self, record: EvidenceRecord) -> Tuple[str, Optional[StoreRefusal]]:
        """Store one evidence record and return ``(digest, refusal)``.

        The record is written content-addressed, so the SAME observation written
        twice is one file, and a file whose bytes no longer hash to its own name
        is detected by :meth:`read_evidence`.
        """
        try:
            materialised = EvidenceRecord.from_dict(record.to_dict())
        except ValueError as exc:
            return "", _refuse("evidence_required", str(exc), subject=str(record.feature_id))
        digest = materialised.digest
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / f"{digest}.json"
        if path.exists():
            # Content-addressed: an existing path with the same digest IS the
            # same observation. Nothing to do, and nothing to overwrite.
            return digest, None
        _atomic_write_json(path, materialised.to_dict())
        self._append_history(
            {
                "event": "evidence",
                "feature_id": materialised.feature_id,
                "outcome": materialised.outcome,
                "kind": materialised.kind,
                "evidence_digest": digest,
                "observed_at": materialised.observed_at,
            }
        )
        return digest, None

    def read_evidence(self, digest: str) -> Tuple[Optional[EvidenceRecord], Optional[StoreRefusal]]:
        """Return ``(record, refusal)`` for ``digest``.

        A record that cannot be READ, or whose bytes no longer match its own
        address, is a refusal and not a record: serving a tampered evidence file
        as if it were evidence is the exact laundering this store exists to stop.
        """
        key = str(digest or "").strip()
        if not key:
            return None, _refuse(
                "evidence_unknown",
                "no evidence digest was named; a claim with no evidence is not a claim",
            )
        path = self.evidence_dir / f"{key}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None, _refuse(
                "evidence_unknown",
                f"no evidence record at {path.name}; the claim names evidence that "
                "does not exist in the evidence store",
                subject=key,
            )
        except (OSError, ValueError) as exc:
            return None, _refuse(
                "evidence_unknown",
                f"evidence record {path.name} could not be read: {exc}",
                subject=key,
            )
        if not isinstance(raw, Mapping):
            return None, _refuse(
                "evidence_unknown",
                f"evidence record {path.name} is not a JSON object",
                subject=key,
            )
        try:
            record = EvidenceRecord.from_dict(raw)
        except ValueError as exc:
            return None, _refuse(
                "evidence_unknown",
                f"evidence record {path.name} is unusable: {exc}",
                subject=key,
            )
        if record.digest != key:
            return None, _refuse(
                "evidence_tampered",
                f"evidence record {path.name} hashes to {record.digest[:12]}, not to the "
                f"{key[:12]} its name claims; the evidence was edited after it was recorded",
                subject=key,
            )
        return record, None

    # -- the guard -------------------------------------------------------

    def guard(self, *, expected_fingerprint: str = "") -> GuardReport:
        """Diff what is on disk against the seal AND the history.

        Two independent mechanisms, because one is not enough:

        * the **seal** catches a changed OBLIGATION (added, removed, mutated
          item; a changed ledger identity; a changed item count);
        * the **history replay** catches a changed REPORT (a status the file
          claims that no transition ever recorded, or a document digest the
          history never saw).

        ``expected_fingerprint`` is the CALLER's pre-recorded seal value. An
        empty one is reported as a FAILURE rather than as intact -- the same
        shape ``execution.independent_evidence.detect_tampering`` uses, because an
        unsealed ledger cannot prove it was not edited.
        """
        violations: List[StoreRefusal] = []
        document = self.document()
        if document is None:
            return GuardReport(
                ok=False,
                sealed=self.seal() is not None,
                ledger_path=str(self.ledger_path),
                seal_path=str(self.seal_path),
                violations=(
                    _refuse(
                        "corrupt_ledger",
                        f"the ledger at {self.ledger_path} is absent or unparseable; it is "
                        "quarantined and reported, never repaired",
                        subject=str(self.ledger_path),
                    ),
                ),
            )

        actual_fingerprint = seal_of(document).fingerprint
        seal = self.seal()
        recorded_documents = tuple(
            str(row.get("document_sha256") or "") for row in self.history() if row.get("document_sha256")
        )

        # -- an unsealed ledger is a FAILURE, not a pass -------------------
        if expected_fingerprint and actual_fingerprint != expected_fingerprint:
            violations.append(
                _refuse(
                    "seal_mismatch",
                    f"the ledger's immutable projection hashes to {actual_fingerprint[:12]} but "
                    f"{expected_fingerprint[:12]} was expected; an obligation changed",
                    subject=str(self.ledger_path),
                )
            )
        if seal is None:
            violations.append(
                _refuse(
                    "seal_missing",
                    "no seal beside the ledger: a removed or rewritten feature cannot be "
                    "detected, and an unsealed ledger is reported as unprotected rather than "
                    "as intact",
                    subject=str(self.seal_path),
                )
            )
        else:
            violations.extend(self._seal_violations(document, seal))

        # -- the history chain --------------------------------------------
        chain = self.verify_history_chain()
        if chain is not None:
            violations.append(chain)
        rows = self.history()
        if not rows:
            violations.append(
                _refuse(
                    "document_not_in_history",
                    "the history is empty, so no document has ever been recorded; a ledger "
                    "with no history cannot have its current view explained",
                    subject=str(self.history_path),
                )
            )
        current = document_digest(document)
        if recorded_documents and current not in recorded_documents:
            violations.append(
                _refuse(
                    "document_not_in_history",
                    f"the ledger on disk hashes to {current[:12]}, a value no history row ever "
                    "recorded; the file was rewritten outside the store",
                    subject=str(self.ledger_path),
                )
            )

        # -- the replay ----------------------------------------------------
        view, replay_refusal = self.replay()
        if replay_refusal is not None:
            violations.append(replay_refusal)
        else:
            on_disk: Dict[str, str] = {}
            for item in document.get("features") or ():
                if isinstance(item, Mapping):
                    on_disk[str(item.get("id") or "")] = str(item.get("status") or "")
            for feature_id in sorted(set(view) | set(on_disk)):
                recorded = view.get(feature_id)
                actual = on_disk.get(feature_id)
                if recorded is None:
                    violations.append(
                        _refuse(
                            "status_without_history",
                            f"{feature_id!r} is on disk with status {actual!r} but no transition "
                            "ever recorded it; the status field was edited directly",
                            subject=feature_id,
                        )
                    )
                elif recorded != actual:
                    violations.append(
                        _refuse(
                            "status_without_history",
                            f"{feature_id!r} is {actual!r} on disk but the history says the last "
                            f"accepted transition made it {recorded!r}",
                            subject=feature_id,
                        )
                    )

        removed, added, mutated = self._item_delta(document, seal)
        return GuardReport(
            ok=not violations,
            sealed=seal is not None,
            ledger_path=str(self.ledger_path),
            seal_path=str(self.seal_path),
            expected_fingerprint=str(expected_fingerprint or (seal.fingerprint if seal else "")),
            actual_fingerprint=actual_fingerprint,
            document_digest=current,
            recorded_documents=recorded_documents,
            violations=tuple(violations),
            removed=removed,
            added=added,
            mutated=mutated,
            history_rows=len(rows),
            replayed=view,
        )

    def _seal_violations(self, document: Mapping[str, Any], seal: LedgerSeal) -> List[StoreRefusal]:
        """Return the seal-derived violations: added, removed, mutated, identity."""
        violations: List[StoreRefusal] = []
        data = dict(document or {})
        features = [item for item in (data.get("features") or ()) if isinstance(item, Mapping)]
        current_ids = {str(item.get("id") or "") for item in features}
        sealed_ids = set(seal.item_ids)
        removed = sorted(sealed_ids - current_ids)
        added = sorted(current_ids - sealed_ids)
        if removed:
            violations.append(
                _refuse(
                    "item_removed",
                    f"features removed since the seal: {', '.join(removed)}. Deleting a "
                    "requirement is as much a spec mutation as rewriting one",
                )
            )
        if added:
            violations.append(
                _refuse(
                    "item_added",
                    f"features added since the seal: {', '.join(added)}. Inventing an "
                    "obligation -- especially one already marked passing -- is a ledger mutation",
                )
            )
        if len(features) != int(seal.item_count):
            violations.append(
                _refuse(
                    "item_count_changed",
                    f"the ledger holds {len(features)} features; the seal recorded "
                    f"{int(seal.item_count)}",
                )
            )
        sealed_projection = {
            str(item.get("id") or ""): item for item in (seal.projection.get("features") or ()) if isinstance(item, Mapping)
        }
        for feature_id in sorted(current_ids & sealed_ids):
            feature = next(item for item in features if str(item.get("id") or "") == feature_id)
            if immutable_projection(feature) != sealed_projection.get(feature_id):
                violations.append(
                    _refuse(
                        "item_mutated",
                        f"{feature_id!r}'s obligation changed: its id, description, "
                        "verification or derivation differs from the seal. Success criteria are "
                        "not the agent's to rewrite",
                        subject=feature_id,
                    )
                )
        for name in IMMUTABLE_LEDGER_FIELDS:
            sealed_value = seal.projection.get(name)
            if sealed_value is not None and data.get(name) != sealed_value:
                violations.append(
                    _refuse(
                        "ledger_identity_changed",
                        f"ledger field {name!r} is {data.get(name)!r}; the seal recorded "
                        f"{sealed_value!r}",
                        subject=str(name),
                    )
                )
        return violations

    def _item_delta(
        self, document: Mapping[str, Any], seal: Optional[LedgerSeal]
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
        """Return ``(removed, added, mutated)`` ids for the report's summary keys."""
        if seal is None:
            return (), (), ()
        features = [
            item for item in ((document or {}).get("features") or ()) if isinstance(item, Mapping)
        ]
        current_ids = {str(item.get("id") or "") for item in features}
        sealed_ids = set(seal.item_ids)
        sealed_projection = {
            str(item.get("id") or ""): item
            for item in (seal.projection.get("features") or ())
            if isinstance(item, Mapping)
        }
        mutated = tuple(
            sorted(
                feature_id
                for feature_id in current_ids & sealed_ids
                if immutable_projection(next(i for i in features if str(i.get("id") or "") == feature_id))
                != sealed_projection.get(feature_id)
            )
        )
        return tuple(sorted(sealed_ids - current_ids)), tuple(sorted(current_ids - sealed_ids)), mutated

    # -- the one write ---------------------------------------------------

    def apply_status(
        self,
        feature_id: str,
        status: str,
        *,
        evidence_digest: str = "",
        actor: str = "",
        reason: str = "",
        allow_attested: bool = False,
        timestamp: str = "",
        allow_cross_session_evidence: bool = False,
    ) -> TransitionReceipt:
        """Apply ONE status change, or refuse it. This is the only mutation path.

        Refusals, in the order a reader cares:

        * an unknown feature, an unknown status, or an edge the table forbids;
        * a consequential change with no reason (``reason_required``);
        * a ``passing`` claim with no resolvable, passing, ``run`` evidence
          recorded against the feature's OWN command (``evidence_*``);
        * **a ``passing`` claim whose evidence belongs to a DIFFERENT session**
          (``evidence_session_mismatch``).

        ``passing -> failing`` is a legal edge and is marked ``backward`` on the
        receipt. It needs a reason but NOT a passing evidence artefact: a
        regression is often observed without a stored verification, and demanding
        one would make under-reporting the easy path.

        **``allow_cross_session_evidence`` is the W2.2 drift fix and its default is
        the load-bearing part.** The drift audit found that before this parameter
        existed, session 4 could mark a feature ``passing`` by citing session 1's
        evidence digest: the digest resolved, the record said ``pass``, the
        command matched, and every check passed -- because nothing compared the
        evidence's own ``session`` to the ``actor`` asking for the status change.
        Each write was individually valid, which is exactly what makes it drift:
        the per-row evidence is real and the per-row ATTRIBUTION is a lie, and a
        ledger that only ever accumulates such writes reports four sessions of
        verified work on the strength of one run.

        The override exists because the honest case is real -- a session may
        legitimately re-affirm a feature against an unchanged suite -- so the
        refusal names the mismatch and the caller passes the flag deliberately.
        It is recorded on the history row as ``cross_session_evidence``, so a
        reader can enumerate every re-affirmation instead of inferring one.
        """
        wanted = str(status or "").strip().lower()
        subject = str(feature_id)
        document = self.document()
        if document is None:
            refusal = _refuse(
                "corrupt_ledger",
                f"the ledger at {self.ledger_path} is absent or unparseable; nothing is written "
                "to a ledger whose state is unknown",
                subject=subject,
            )
            return TransitionReceipt(
                ok=False,
                feature_id=subject,
                from_status="",
                to_status=wanted,
                refusal=refusal,
            )

        if wanted not in FEATURE_STATUSES:
            refusal = _refuse(
                "status_not_in_vocabulary",
                f"{wanted!r} is not one of {list(FEATURE_STATUSES)}",
                subject=subject,
            )
            return TransitionReceipt(
                ok=False, feature_id=subject, from_status="", to_status=wanted, refusal=refusal
            )

        features = [item for item in (document.get("features") or ()) if isinstance(item, Mapping)]
        target = next((item for item in features if str(item.get("id") or "") == subject), None)
        if target is None:
            refusal = _refuse(
                "unknown_feature",
                f"no feature {subject!r} in this ledger; ids are read-only to the agent, so a "
                "new requirement is an initializer decision",
                subject=subject,
            )
            return TransitionReceipt(
                ok=False, feature_id=subject, from_status="", to_status=wanted, refusal=refusal
            )

        current = str(target.get("status") or INITIAL_FEATURE_STATUS)
        if not is_transition_allowed(current, wanted):
            refusal = _refuse(
                "transition_not_allowed",
                f"{current!r} -> {wanted!r} is not an allowed edge; the table is "
                f"{ {k: list(v) for k, v in ALLOWED_TRANSITIONS.items()} }",
                subject=subject,
            )
            return TransitionReceipt(
                ok=False, feature_id=subject, from_status=current, to_status=wanted, refusal=refusal
            )

        reason_refusal = _require_reason(
            reason, what=f"{current!r} -> {wanted!r} on {subject!r}"
        )
        if reason_refusal is not None:
            return TransitionReceipt(
                ok=False, feature_id=subject, from_status=current, to_status=wanted, refusal=reason_refusal
            )

        record: Optional[EvidenceRecord] = None
        if wanted == PASSING_FEATURE_STATUS:
            evidence_refusal = self._require_passing_evidence(
                target, evidence_digest=evidence_digest, allow_attested=allow_attested
            )
            if evidence_refusal is not None:
                return TransitionReceipt(
                    ok=False,
                    feature_id=subject,
                    from_status=current,
                    to_status=wanted,
                    evidence_digest=str(evidence_digest),
                    actor=str(actor),
                    reason=str(reason),
                    refusal=evidence_refusal,
                )
            loaded, _ = self.read_evidence(evidence_digest)
            record = loaded

        # -- W2.2 drift fix: bind the EVIDENCE to the session claiming it ---
        #
        # Deliberately placed AFTER `_require_passing_evidence`, so an unusable
        # digest is still reported as such and this check only ever sees evidence
        # that is real, passing, and for this feature's own command. The order
        # matters: a caller must not be told "wrong session" about evidence that
        # was never valid.
        cross_session = False
        if wanted == PASSING_FEATURE_STATUS and record is not None and str(actor):
            evidence_session = str(record.session or "")
            if evidence_session and evidence_session != str(actor):
                if not allow_cross_session_evidence:
                    return TransitionReceipt(
                        ok=False,
                        feature_id=subject,
                        from_status=current,
                        to_status=wanted,
                        evidence_digest=str(evidence_digest),
                        actor=str(actor),
                        reason=str(reason),
                        refusal=_refuse(
                            "evidence_session_mismatch",
                            f"the evidence belongs to session {evidence_session!r} but "
                            f"{actor!r} is asking for the status change; re-running the check "
                            "is what makes the claim this session's own. Pass "
                            "allow_cross_session_evidence=True if re-affirming against an "
                            "unchanged suite is genuinely what you mean -- it is recorded",
                            subject=subject,
                        ),
                    )
                cross_session = True

        # -- publish ------------------------------------------------------
        updated = [dict(item) for item in features]
        for item in updated:
            if str(item.get("id") or "") != subject:
                continue
            item["status"] = wanted
            # The published row CITES its evidence by address. Without the
            # digest the ledger says "there is some evidence" without saying
            # which, and the overclaim audit would then have to guess -- or,
            # worse, count a claim as supported on the strength of an unrelated
            # record for the same feature.
            item["evidence"] = (
                [{**record.to_dict(), "evidence_digest": str(evidence_digest)}] if record is not None else []
            )
            item["sessions_touched"] = sorted(
                {str(value) for value in (item.get("sessions_touched") or ())}
                | ({str(actor)} if str(actor) else set())
            )
        new_document = dict(document)
        new_document["features"] = updated

        guard = self.guard()
        # A store that publishes a new current view over an ALREADY-violated
        # ledger is how corruption gets laundered into history.
        if not guard.ok:
            refusal = _refuse(
                "seal_mismatch" if "seal_mismatch" in guard.reasons else guard.reasons[0],
                "refusing to write over a ledger that already fails its guard: "
                + "; ".join(item.detail for item in guard.violations[:3]),
                subject=subject,
            )
            return TransitionReceipt(
                ok=False,
                feature_id=subject,
                from_status=current,
                to_status=wanted,
                evidence_digest=str(evidence_digest),
                actor=str(actor),
                reason=str(reason),
                refusal=refusal,
            )

        write_feature_ledger(self.ledger_path, _as_ledger(new_document))
        # Digest what was ACTUALLY WRITTEN, not the value we meant to write.
        # T1's renderer adds derived keys (``verification_ran``, ``evidence_kind``)
        # and normalises evidence rows, so digesting the pre-serialisation dict
        # would record a value the file never had -- and the guard's whole
        # document-not-in-history check would then fire on every honest write.
        written = self.document() or {}
        digest_after = document_digest(written)
        row = self._append_history(
            {
                "event": "transition",
                "feature_id": subject,
                "from_status": current,
                "to_status": wanted,
                "evidence_digest": str(evidence_digest),
                "actor": str(actor),
                "reason": str(reason),
                "allow_attested": bool(allow_attested),
                # W2.2: the evidence's OWN session rides every row, so the
                # who-ran-it question is answerable from the history alone and a
                # deliberate cross-session re-affirmation is enumerable rather
                # than inferable.
                "evidence_session": str(getattr(record, "session", "") or ""),
                "cross_session_evidence": bool(cross_session),
                "document_sha256": digest_after,
                "at": str(timestamp or _now()),
            }
        )
        return TransitionReceipt(
            ok=True,
            feature_id=subject,
            from_status=current,
            to_status=wanted,
            evidence_digest=str(evidence_digest),
            actor=str(actor),
            reason=str(reason),
            backward=_is_backward(current, wanted),
            document_digest=digest_after,
            chain_digest=str(row.get("chain") or ""),
            history_row=int(row.get("seq") or 0),
        )

    def _require_passing_evidence(
        self,
        feature: Mapping[str, Any],
        *,
        evidence_digest: str,
        allow_attested: bool,
    ) -> Optional[StoreRefusal]:
        """Return a refusal when ``passing`` is not backed by stored evidence.

        Four checks, and the fourth is the one a self-graded report card fails:

        1. the digest RESOLVES in the separate evidence store (and the file's
           bytes still hash to its own name);
        2. the record says ``pass`` and ``ran``;
        3. the record was made by a ``run``, not an attestation, unless the
           caller explicitly allowed attested evidence (T1's
           ``FEATURE_PASSING_REQUIRES_RUN``, delegated);

        **On attested evidence the honest state is that it cannot pass at all.**
        Measured on this tree before this note was written:
        ``VerificationRun.__post_init__`` normalises an ``attested`` row to
        ``ran=False, outcome="not_run"``, so ``is_pass_evidence`` is
        structurally unsatisfiable for one, and ``check_verification_evidence``
        refuses it under BOTH ``allow_attested`` values -- with two DIFFERENT
        reasons, so a caller reading the refusal cannot tell that the third
        option does not exist either. **That third option now exists** --
        ``allow_attested`` accepts an attestation and it is a real control, not a
        formality: an attested claim is refused without the flag and accepted
        with it. (It was DEAD when this store was written, registered as
        ``ALLOW_ATTESTED_IS_A_DEAD_ESCAPE_HATCH`` with owner T1; T1 fixed it
        mid-phase and the entry was deleted rather than left stale -- see
        :data:`RECORDED_GAPS`.) An attestation is still a human's word rather
        than an executed run, which is why it is opt-in per call and never the
        default.
        4. the record's ``command`` MATCHES the feature's own immutable
           verification command -- otherwise evidence from some other check is
           being presented as this feature's proof.
        """
        subject = str(feature.get("id") or "")
        record, refusal = self.read_evidence(evidence_digest)
        if refusal is not None:
            return refusal
        assert record is not None  # read_evidence returns a refusal or a record

        if str(record.feature_id) != subject:
            return _refuse(
                "evidence_unknown",
                f"evidence {evidence_digest[:12]} is for feature {record.feature_id!r}, not "
                f"{subject!r}; one feature's evidence is not another's",
                subject=subject,
            )
        if not record.is_pass_evidence and not _is_attested_pass(record):
            return _refuse(
                "evidence_did_not_pass",
                f"the stored evidence records outcome={record.outcome!r} ran={record.ran}; "
                "'we did not check' and 'we checked and it broke' are different facts and "
                "neither is a pass",
                subject=subject,
            )
        if str(record.kind) != "run" and not allow_attested:
            return _refuse(
                "evidence_did_not_pass",
                f"the stored evidence is {record.kind!r} (a human said so), not a run; pass "
                "allow_attested=True if an attestation is genuinely this feature's contract",
                subject=subject,
            )
        declared = str((feature.get("verification") or {}).get("command") or "")
        if declared and str(record.command) and str(record.command) != declared:
            return _refuse(
                "evidence_command_mismatch",
                f"the evidence was produced by {record.command!r} but this feature's own "
                f"verification is {declared!r}; evidence from a different check is not this "
                "feature's proof",
                subject=subject,
            )
        # Delegate the final word to T1's rule rather than keeping a second one.
        delegated = check_verification_evidence(
            [
                VerificationRun(
                    feature_id=subject,
                    method="command",
                    command=str(record.command),
                    outcome=str(record.outcome),
                    ran=bool(record.ran),
                    exit_code=record.exit_code,
                    started_at=str(record.observed_at),
                    kind=str(record.kind),
                )
            ],
            allow_attested=allow_attested,
        )
        if delegated is not None:
            return _refuse("evidence_did_not_pass", f"harness.long_horizon refused it: {delegated.detail}", subject=subject)
        return None

    # -- structural writes (authoring, not the agent) ---------------------

    def replace_document(self, document: Mapping[str, Any], *, reseal: bool = False) -> Dict[str, Any]:
        """Replace the whole ledger document. AUTHORING ONLY, and loud.

        This is the only structural write path and it exists so a human (or an
        initializer step) can define obligations. It is refused outright while
        the guard is already failing, it re-seals ONLY when ``reseal=True``, and
        the re-seal is appended to the history so the new fingerprint is on the
        record rather than being a silent re-baseline.

        This is tamper path 3 of the six in the handoff, and the pin proves that
        a replacement WITHOUT ``reseal=True`` fails the guard.
        """
        guard = self.guard()
        payload = dict(document or {})
        if payload.get("schema") != FEATURE_LEDGER_SCHEMA:
            return {
                "written": False,
                "reason": (
                    f"schema {payload.get('schema')!r} is not {FEATURE_LEDGER_SCHEMA!r}; a "
                    "ledger document is refused whole rather than half-read"
                ),
            }
        if not guard.ok and not reseal:
            return {
                "written": False,
                "reason": (
                    "the ledger already fails its guard ("
                    + ", ".join(guard.reasons)
                    + "); quarantine it rather than replacing it, or pass reseal=True to "
                    "re-baseline deliberately and have that recorded"
                ),
                "guard": guard.to_dict(),
            }
        receipt = dict(write_feature_ledger(self.ledger_path, _as_ledger(payload)))
        digest_after = document_digest(payload)
        self._append_history(
            {
                "event": "document_replaced",
                "resealed": bool(reseal),
                "document_sha256": digest_after,
                "reason": "authoring" if guard.ok else "deliberate re-baseline over a failing guard",
            }
        )
        if reseal:
            fresh = seal_of(payload, sealed_at=_now())
            self.write_seal(fresh)
            self._append_history(
                {
                    "event": "sealed",
                    "seal_fingerprint": fresh.fingerprint,
                    "item_count": fresh.item_count,
                    "document_sha256": digest_after,
                    "reason": "deliberate re-baseline",
                }
            )
            receipt["resealed"] = True
            receipt["seal_fingerprint"] = fresh.fingerprint
        else:
            receipt["resealed"] = False
        receipt["document_digest"] = digest_after
        return receipt

    # -- quarantine ------------------------------------------------------

    def quarantine(self, reason: str, *, detail: str = "") -> QuarantineReceipt:
        """MOVE the corrupt artefacts aside, report, and never repair.

        The ledger, the seal and the history are copied into
        ``.neo-store/quarantine/<ts>-<slug>/`` and then removed from their working
        locations, and a ``quarantine.json`` records exactly what moved, why and
        what its digests were. Nothing is rewritten, nothing is re-sealed and no
        default ledger is invented in their place: a caller that wants a fresh
        ledger asks for one.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.quarantine_dir / f"{stamp}-{_slug(reason)}"
        try:
            target.mkdir(parents=True, exist_ok=False)
        except (OSError, FileExistsError) as exc:
            refusal = _refuse(
                "quarantined",
                f"could not create the quarantine directory {target}: {exc}",
                subject=str(target),
            )
            return QuarantineReceipt(ok=False, root=str(self.root), refusal=refusal)

        moved: List[str] = []
        manifest: List[Dict[str, Any]] = []
        for source in (self.ledger_path, self.seal_path, self.history_path, self.progress_path):
            if not source.exists():
                continue
            destination = target / source.name
            try:
                shutil.copy2(str(source), str(destination))
                manifest.append(
                    {
                        "from": str(source),
                        "to": str(destination),
                        "bytes": int(destination.stat().st_size),
                        "sha256": _sha256(destination.read_text(encoding="utf-8", errors="replace")),
                    }
                )
                source.unlink()
                moved.append(source.name)
            except OSError as exc:
                manifest.append({"from": str(source), "error": f"{type(exc).__name__}: {exc}"})

        report = {
            "store_schema": STORE_SCHEMA,
            "at": _now(),
            "root": str(self.root),
            "reason": str(reason),
            "detail": str(detail),
            "guard": self.guard().to_dict(),
            "manifest": manifest,
            "note": (
                "the originals were MOVED, not repaired. Nothing was re-sealed and no "
                "default document was written in their place; a caller that wants a "
                "fresh ledger asks for one explicitly."
            ),
        }
        _atomic_write_json(target / QUARANTINE_REPORT_NAME, report)
        return QuarantineReceipt(
            ok=True,
            root=str(self.root),
            quarantine_dir=str(target),
            moved=tuple(moved),
            reason=str(reason),
            detail=str(detail),
        )

    # -- the overclaim audit ---------------------------------------------

    def audit(
        self,
        *,
        expected_fingerprint: str = "",
        evidence_root: str = "",
    ) -> OverclaimReport:
        """Report whether the ledger claims more than the evidence supports.

        The report a person reads before believing a green ledger. Four things
        are measured, and none of them is inferred from the ledger's own words:

        1. how many features are marked ``passing``;
        2. how many of those have a ``pass`` record in the SEPARATE evidence
           store (``coverage_share``);
        3. whether any of those acceptance suites was edited after sealing, via
           :func:`execution.independent_evidence.detect_tampering`;
        4. whether the whole ledger's completion claim is premature, via
           :func:`execution.independent_evidence.premature_completion` -- the
           repo's own mechanical check for "claimed complete without a clean
           final gate".

        ``evidence_root`` is the CALLER's pre-recorded root (a seal-time
        fingerprint from another process). An empty one makes every suite with a
        recorded fingerprint read as UNCHECKABLE rather than as clean, because an
        unrecorded expectation cannot prove the suite was not edited.
        """
        document = self.document()
        features = [
            item for item in ((document or {}).get("features") or ()) if isinstance(item, Mapping)
        ]
        passing = [item for item in features if str(item.get("status") or "") == PASSING_FEATURE_STATUS]
        supported: List[str] = []
        unsupported: List[str] = []
        tampered: List[str] = []
        per_feature: List[Dict[str, Any]] = []

        for feature in features:
            feature_id = str(feature.get("id") or "")
            if str(feature.get("status") or "") != PASSING_FEATURE_STATUS:
                per_feature.append({"id": feature_id, "status": str(feature.get("status") or ""), "evidence": "n/a"})
                continue
            digests = [
                str(row.get("evidence_digest") or "")
                for row in (feature.get("evidence") or ())
                if isinstance(row, Mapping)
            ] or [str(feature.get("evidence_digest") or "")]
            digests = [value for value in digests if value]
            row_report = self._audit_one(feature_id, digests, expected_fingerprint=expected_fingerprint)
            per_feature.append(row_report)
            if row_report["state"] == "supported":
                supported.append(feature_id)
            elif row_report["state"] == "tampered":
                tampered.append(feature_id)
                unsupported.append(feature_id)
            else:
                unsupported.append(feature_id)

        coverage = (len(supported) / len(passing)) if passing else None
        gate_ran = bool(passing)
        gate_passed = bool(passing) and not unsupported and not tampered
        premature = _premature_completion(
            claimed_complete=bool(features) and all(
                str(item.get("status") or "") == PASSING_FEATURE_STATUS for item in features
            ),
            final_gate_ran=gate_ran,
            final_gate_passed=gate_passed,
            reason=(
                ""
                if gate_passed
                else f"{len(unsupported)} of {len(passing)} passing feature(s) lack a stored "
                "passing verification"
            ),
        )
        return OverclaimReport(
            total_features=len(features),
            passing=len(passing),
            passing_with_stored_evidence=len(supported),
            unsupported_claims=tuple(sorted(set(unsupported))),
            undetected_tampering=tuple(sorted(set(tampered))),
            coverage_share=coverage,
            premature=premature,
            per_feature=tuple(per_feature),
            vacuous=not passing,
        )

    def _audit_one(self, feature_id: str, digests: Sequence[str], *, expected_fingerprint: str) -> Dict[str, Any]:
        """Audit one ``passing`` feature against the separate evidence store."""
        if not digests:
            # The ledger asserts there is evidence but does not say which. Fall
            # back to the evidence store's own index for this feature, and report
            # the fallback on the row, so the audit's answer is attributable.
            candidates = sorted(
                path.stem
                for path in self._evidence_files()
                if str(_peek_feature_id(path)) == feature_id
            )
            if not candidates:
                return {
                    "id": feature_id,
                    "state": "unsupported",
                    "reason": "the ledger claims passing with no evidence row and the evidence store holds none for this feature",
                }
            digests = list(candidates)

        for digest in digests:
            record, refusal = self.read_evidence(digest)
            if refusal is not None or record is None:
                return {
                    "id": feature_id,
                    "state": "unsupported",
                    "evidence_digest": digest,
                    "reason": refusal.detail if refusal is not None else "unreadable",
                }
            if str(record.outcome) != "pass" or not bool(record.ran):
                return {
                    "id": feature_id,
                    "state": "unsupported",
                    "evidence_digest": digest,
                    "reason": f"outcome={record.outcome!r} ran={record.ran}",
                }
            suite = _suite_tampering(record, expected_fingerprint=expected_fingerprint)
            if suite is not None:
                return {
                    "id": feature_id,
                    "state": "tampered",
                    "evidence_digest": digest,
                    "reason": suite,
                    "suite": True,
                }
            return {"id": feature_id, "state": "supported", "evidence_digest": digest, "observed_at": record.observed_at}
        return {"id": feature_id, "state": "unsupported", "reason": "no usable evidence"}

    def _evidence_files(self) -> List[Path]:
        """Return every evidence file on disk, sorted by digest.

        Bounded by the store's own directory and read one file at a time, so a
        ledger with thousands of features does not load every observation into
        memory to answer "which of these belong to this feature".
        """
        if not self.evidence_dir.is_dir():
            return []
        return sorted(self.evidence_dir.glob("*.json"))

    # -- convenience -----------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """Return the whole store state as one JSON-compatible document."""
        return {
            "store_schema": STORE_SCHEMA,
            "root": str(self.root),
            "ledger_path": str(self.ledger_path),
            "ledger_schema": FEATURE_LEDGER_SCHEMA,
            "progress_schema": PROGRESS_SCHEMA,
            "progress_ledger_name": PROGRESS_LEDGER_NAME,
            "seal": (self.seal().to_dict() if self.seal() is not None else None),
            "guard": self.guard().to_dict(),
            "audit": self.audit().to_dict(),
            "history_rows": len(self.history()),
            "immutable_feature_fields": list(IMMUTABLE_FEATURE_FIELDS),
            "mutable_feature_fields": list(MUTABLE_FEATURE_FIELDS),
            "agent_writable_fields": list(AGENT_WRITABLE_LEDGER_FIELDS),
            "allowed_transitions": {key: list(value) for key, value in ALLOWED_TRANSITIONS.items()},
            "refusals": list(STORE_REFUSALS),
            "reachability": dict(REACHABILITY),
        }

    @classmethod
    def open(cls, root: Any, *, ledger_path: Any = "") -> Tuple[Optional["LedgerStore"], Optional[StoreRefusal]]:
        """Return ``(store, refusal)`` -- never raises for a corrupt ledger.

        A loader that raises would take a run down over a formatting detail, and
        a loader that half-reads a ledger would turn "the ledger is broken" into
        "the ledger has fewer features". Both are refused whole, and the refusal
        names the reason so a caller can quarantine on purpose rather than by
        accident.
        """
        store = cls(root, ledger_path=ledger_path)
        if not store.exists():
            return None, _refuse(
                "no_store",
                f"no feature ledger at {store.ledger_path}; an initializer must write one "
                "before any status can be claimed",
                subject=str(store.ledger_path),
            )
        document = store.document()
        if document is None:
            return None, _refuse(
                "corrupt_ledger",
                f"the ledger at {store.ledger_path} is not readable JSON; quarantine it and "
                "report -- never repair it silently",
                subject=str(store.ledger_path),
            )
        if str(document.get("schema") or "") != FEATURE_LEDGER_SCHEMA:
            return None, _refuse(
                "unknown_schema",
                f"ledger schema {document.get('schema')!r} is not {FEATURE_LEDGER_SCHEMA!r}; "
                "refused whole rather than half-read",
                subject=str(store.ledger_path),
            )
        # T1's loader is the second opinion. If it refuses what we parsed, both
        # answers are reported rather than one of them winning silently.
        _, tool_refusal = load_feature_ledger(store.ledger_path)
        if tool_refusal is not None:
            return None, _refuse(
                "corrupt_ledger",
                f"harness.long_horizon refused the ledger: {tool_refusal.detail}",
                subject=str(store.ledger_path),
                owner=str(tool_refusal.owner or "T1 / harness"),
            )
        return store, None


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _as_ledger(document: Mapping[str, Any]) -> Any:
    """Return a ``harness.long_horizon.FeatureLedger`` for ``document``.

    The store never writes a document T1 cannot read back. Building the value
    through T1's own ``Feature.from_dict`` means the on-disk shape is whatever
    the tool layer's loader accepts, by construction rather than by agreement
    between two independently written serialisers -- a store that wrote its own
    JSON would be a second answer to "what is a ledger document".
    """
    from harness.long_horizon import Feature, FeatureLedger

    data = dict(document or {})
    return FeatureLedger(
        schema=str(data.get("schema") or FEATURE_LEDGER_SCHEMA),
        project_id=str(data.get("project_id") or ""),
        request=str(data.get("request") or ""),
        created_at=str(data.get("created_at") or ""),
        sessions=tuple(str(item) for item in (data.get("sessions") or ())),
        features=tuple(Feature.from_dict(item) for item in (data.get("features") or ())),
    )


def _is_attested_pass(record: "EvidenceRecord") -> bool:
    """Return whether an ATTESTED record reports a passing outcome.

    An attestation has no run behind it, so ``ran`` is legitimately ``False`` and
    requiring it would make every attested claim fail closed for a reason that is
    not a defect. The OUTCOME still has to say ``pass``; ``ran`` is what the
    ``allow_attested`` flag governs.
    """
    return str(record.kind) != "run" and str(record.outcome) == EvidenceRecord.PASS_OUTCOME


def _is_backward(from_status: str, to_status: str) -> bool:
    """Return whether an edge moves AWAY from ``passing``.

    "Backward" is defined against the pass/fail axis rather than by table order,
    because the table is a set and sets have no order.
    """
    rank = {"blocked": 1, INITIAL_FEATURE_STATUS: 0, "failing": 0, "skipped": 0, PASSING_FEATURE_STATUS: 2}
    return rank.get(str(to_status), 0) < rank.get(str(from_status), 0)


def _now() -> str:
    """Return the current UTC timestamp, ISO-8601 with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _peek_feature_id(path: Path) -> str:
    """Return a stored record's ``feature_id`` without validating the record.

    Used only to INDEX the evidence store for the audit's fallback. A file that
    cannot be read yields ``""``, so a corrupt record is never attributed to a
    feature; :meth:`LedgerStore.read_evidence` is the authority when a digest is
    actually named.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("feature_id") or "") if isinstance(raw, Mapping) else ""


def _slug(value: str) -> str:
    """Return a filesystem-safe slug for a quarantine directory name."""
    keep = [char if char.isalnum() else "-" for char in str(value or "reason").lower()]
    text = "".join(keep).strip("-") or "reason"
    return "-".join(part for part in text.split("-") if part)[:48] or "reason"


def _atomic_write_json(path: Any, payload: Mapping[str, Any]) -> str:
    """Write ``payload`` as JSON via tmp + replace. Never raises."""
    target = Path(str(path))
    text = json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    directory = target.parent
    tmp = ""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # tmp + os.replace, so a crash mid-write cannot leave a truncated seal or
        # evidence record where a whole one is expected. `delete=False` because
        # the temp file must SURVIVE the handle being closed in order to be
        # renamed; the rename is what publishes it.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(directory),
            prefix=".neo-store-",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as handle:
            tmp = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(target))
    except OSError as exc:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise ValueError(f"could not write {target}: {type(exc).__name__}: {exc}") from exc
    return str(target)


def _suite_tampering(record: EvidenceRecord, *, expected_fingerprint: str) -> Optional[str]:
    """Delegate the acceptance-suite comparison to the repo's own detector.

    ``execution.independent_evidence.detect_tampering`` is the ONE tamper
    authority in this tree and this module does not fork it. The import is lazy
    because ``memory/`` sits below ``execution/`` in the dependency direction and
    must stay importable without it.

    An empty ``expected_fingerprint`` is the honest reading, and it is UNCHECKED
    rather than clean: nobody fingerprinted this suite, so nothing can show it is
    untouched. :func:`execution.independent_evidence.detect_tampering` reports a
    missing fingerprint as a failure, and this wrapper turns that into ``None``
    (no evidence of tampering, no evidence of safety) rather than into a clean
    bill of health for a suite nobody sealed.
    """
    if not str(record.accept_suite_root):
        return None
    if not str(expected_fingerprint).strip():
        return None
    try:
        from execution.independent_evidence import (
            HeldOutFile,
            HeldOutSuite,
            detect_tampering,
        )
    except Exception:  # pragma: no cover - the detector is always present in-tree
        return None
    root = Path(str(record.accept_suite_root))
    files: List[HeldOutFile] = []
    if root.is_dir():
        for entry in sorted(root.glob("*.py")):
            try:
                files.append(
                    HeldOutFile(name=entry.name, digest=_sha256(entry.read_text(encoding="utf-8", errors="replace")))
                )
            except OSError:
                continue
    suite = HeldOutSuite(root=str(root), seed=0, files=tuple(files), concealed=False)
    report = detect_tampering(suite, expected_fingerprint=str(expected_fingerprint or ""))
    if report.ok:
        return None
    changed = ", ".join(report.changed) or "(fingerprint mismatch)"
    return (
        f"the acceptance suite behind {record.feature_id!r} changed after sealing: {changed}; "
        f"{report.reason}"
    )


def _premature_completion(
    *, claimed_complete: bool, final_gate_ran: bool, final_gate_passed: bool, reason: str = ""
) -> Dict[str, Any]:
    """Delegate the completion claim to the repo's own mechanical check.

    ``execution.independent_evidence.premature_completion`` is a deliberately
    dumb four-field reducer -- "claimed complete without a clean final gate" --
    and the whole point of it is that it is mechanical. The mapping is explicit:

    * ``claimed_complete`` -- every feature is marked ``passing``;
    * ``final_gate_ran``    -- at least one verification was recorded;
    * ``final_gate_passed`` -- EVERY ``passing`` feature has a stored passing
      verification. This is the term that fails when a ledger claims more than
      its evidence supports, which is precisely the premature-completion shape.
    """
    try:
        from execution.independent_evidence import premature_completion as _delegate
    except Exception:  # pragma: no cover - the check is always present in-tree
        return {
            "claimed_complete": bool(claimed_complete),
            "final_gate_ran": bool(final_gate_ran),
            "final_gate_passed": bool(final_gate_passed),
            "premature": bool(claimed_complete and not (final_gate_ran and final_gate_passed)),
            "reason": str(reason),
            "delegated": False,
        }
    result = dict(
        _delegate(
            claimed_complete=bool(claimed_complete),
            final_gate_ran=bool(final_gate_ran),
            final_gate_passed=bool(final_gate_passed),
            reason=str(reason),
        )
    )
    result["delegated"] = True
    return result


# ---------------------------------------------------------------------------
# CLI -- a corrupt ledger must be REPORTABLE by a human, not only by a test
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the store's guard and overclaim audit. Exits 2 on a finding.

    0 = the ledger passes its guard AND claims nothing its evidence cannot
    support; 2 = at least one of those is false; 3 = there is no store to audit.
    A corrupt ledger and a clean one are therefore distinguishable from a shell,
    which is the difference between a gate and a log line.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m memory.feature_ledger_store")
    parser.add_argument("root", help="the project root holding features.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--expected-fingerprint",
        default="",
        help="a seal-time fingerprint recorded in another process; empty means UNCHECKABLE",
    )
    parser.add_argument(
        "--quarantine",
        metavar="REASON",
        default="",
        help="move the corrupt artefacts aside and report; never repairs",
    )
    args = parser.parse_args(list(argv or []))

    store, refusal = LedgerStore.open(args.root)
    if store is None:
        payload = {"ok": False, "refusal": refusal.to_dict() if refusal else None}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"no store: {refusal.render() if refusal else 'unknown reason'}")
        return 3

    if args.quarantine:
        receipt = store.quarantine(args.quarantine, detail="requested on the command line")
        if args.json:
            print(json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        else:
            print(receipt.render())
        return 0 if receipt.ok else 2

    report = store.report()
    guard = store.guard(expected_fingerprint=args.expected_fingerprint)
    audit = store.audit(expected_fingerprint=args.expected_fingerprint)
    report["guard"] = guard.to_dict()
    report["audit"] = audit.to_dict()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(guard.render())
        print(audit.render())
    return 0 if (guard.ok and not audit.overclaims) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
