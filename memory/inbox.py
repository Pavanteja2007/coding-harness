"""The per-project ``.inbox/``: extracted memories that a HUMAN must apply.

THE NON-NEGOTIABLE THIS MODULE EXISTS TO ENFORCE
------------------------------------------------
**Nothing is persisted until a human applies it.** Not by the extractor, not by
a retry, not by a consolidation pass, not by a later ``poll()``, not by the
MCP server. :meth:`MemoryInbox.propose` is the ONLY method that creates
something, and what it creates is a patch FILE. The decision store is reachable
from exactly one method, :meth:`MemoryInbox.apply`, and that method requires an
explicit human actor.

This is not a policy statement, it is a structural one. There is no code path
from "extraction ran" to "the store has a new row". A test asserts the store's
row count is unchanged across a full propose-and-reject cycle, and a second
test asserts that after applying, the row count moved -- because a gate that
can only prove the negative is a gate nobody can trust.

WHY A FILE INBOX AND NOT A DATABASE FLAG
----------------------------------------
The corpus design writes canonical patch files into a per-project ``.inbox/``
and a user reviews and applies them. The file is the point: it is reviewable
with ``cat``, diffable with ``git diff``, and checkable into version control.
A ``pending`` column in the store is none of those, and it makes the trust
boundary invisible to the one party the boundary exists for.

WHERE THE INBOX LIVES
---------------------
The corpus layout is ``<project>/.inbox/``. This module's DEFAULT is
``<memory_root>/inbox/<repo_key>/.inbox/`` -- outside the user's working tree,
because a default that writes into a checkout turns every extraction run into an
untracked-directory surprise in ``git status``. A caller that wants the corpus
layout exactly passes ``root=`` explicitly. See :func:`memory.paths.inbox_root`.

THE TWO STATES, DEFINED
-----------------------
The inbox is why a proposed memory and a committed memory are different things,
and the two are represented by different TYPES so they cannot be confused:

:class:`ProposedMemory`
    Extracted, gated, redacted, and sitting in a patch file. Has a
    ``proposal_id``. Has NOT been stored. Reading it back is a file read.

:class:`memory.decision_store.Decision`
    Committed. Has a store row id. Survives the inbox being deleted.

:meth:`MemoryInbox.status` returns both ids for one proposal so a caller can
ask "did this land?" without inferring it from a count.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.security import authorize_memory_write, redact_secrets, redact_text

__all__ = [
    "INBOX_DIRNAME",
    "InboxProposal",
    "MemoryInbox",
    "ProposalState",
    "ProposedMemory",
]

#: The directory name, matching the corpus design, so the two layouts are
#: recognisably the same idea whichever root it is given.
INBOX_DIRNAME = ".inbox"

#: Where applied proposals go. They are KEPT rather than deleted: "who applied
#: this, and when" is the audit trail, and an inbox whose applied entries vanish
#: cannot answer that. A user who wants it gone can remove the directory, which
#: is a decision they are entitled to make and this module does not make for
#: them.
APPLIED_DIRNAME = "applied"
REJECTED_DIRNAME = "rejected"

#: Extension for a canonical patch file. ``.json`` because the canonical form
#: has to be machine-readable for a human-gated pipeline to be automatable at
#: all, and JSON is the only format whose round-trip this repository already
#: depends on.
PATCH_SUFFIX = ".patch.json"

_SCHEMA_VERSION = 1

#: Bounded proposal text. Matches the decision store's own per-row storage cap
#: (``_MAX_TEXT_CHARS`` = 16,384) so a proposal that could never be committed is
#: never accepted in the first place.
MAX_PROPOSAL_TEXT_CHARS = 16_384

#: Bound on one proposal file's bytes. A patch file is a human review artifact;
#: an unbounded one is a way to fill a disk by asking the extractor to try
#: harder.
MAX_PATCH_BYTES = 1_048_576

#: Bound on proposals per patch file. Extraction over a long session can
#: produce a lot; a reviewable inbox is chunked rather than unbounded.
MAX_ITEMS_PER_PATCH = 50

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProposalState(str, Enum):
    """The lifecycle of a proposal. ``proposed`` is the ONLY live state.

    There is no ``committing`` or ``applying`` intermediate: applying is a single
    store call under a human actor, and an interrupted apply leaves the patch
    file in ``applied/`` with the ids that landed recorded in it. A state machine
    with an unreachable half-state is worse than a small honest one.
    """

    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"

    def __str__(self) -> str:
        """Return the serialized state."""
        return self.value


@dataclass(frozen=True)
class ProposedMemory:
    """One extracted memory awaiting a human decision.

    Carries its own gate receipt. The gate runs at PROPOSAL time, so a reviewer
    can see WHY a proposed memory looks the way it does -- redacted, or
    claim-downgraded, or quarantined-with-a-reason -- without having to
    reproduce the decision.
    """

    text: str
    category: str = "general"
    rationale: str = ""
    evidence: Tuple[str, ...] = ()
    source: str = "extraction"
    session_id: str = ""
    run_id: str = ""
    turn: Optional[int] = None
    model: str = ""
    provider: str = ""
    confidence: Optional[float] = None
    untrusted_source: bool = False
    redacted: bool = False
    claim_downgraded: bool = False
    quarantined: bool = False
    gate_reason: str = ""
    gate_severity: str = ""
    gate_findings: Tuple[Dict[str, Any], ...] = ()
    index: int = 0

    @property
    def digest(self) -> str:
        """A content address for this proposal, for dedupe and idempotence."""
        payload = "\x1f".join(
            (self.text, self.category, self.source, self.session_id)
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()

    @property
    def applicable(self) -> bool:
        """Whether a human may apply this item.

        A quarantined item is NOT applicable. It stays in the patch file as a
        record that the extractor tried, because "the pipeline attempted an
        authority-claim and the gate stopped it" is evidence worth keeping --
        but it can never reach the store through this module.
        """
        return not self.quarantined and bool(self.text)

    @property
    def content_digest(self) -> str:
        """A content address that does NOT include the session.

        :attr:`digest` deliberately includes ``session_id``: it is the identity
        of a proposal WITHIN a session, which is what proposal-level dedupe
        needs. Suppressing a REJECTION across sessions needs the other half —
        the same sentence extracted by a later session must produce the same
        key, or a human's "no" expires the moment the next run starts and the
        gate becomes decorative.

        Category is excluded too, deliberately: a reviewer rejected a memory,
        not a memory filed under one category instead of another. ``source`` IS
        included, because "the user's own stated convention" and "something an
        issue body claimed" are different claims and a human may well have
        rejected only the second.
        """
        payload = "\x1f".join((self.text, self.source))
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-redacted projection."""
        return {
            "index": self.index,
            "text": redact_secrets(self.text),
            "digest": self.digest,
            "content_digest": self.content_digest,
            "category": self.category,
            "rationale": self.rationale[:1_000],
            "evidence": list(self.evidence)[:20],
            "source": self.source,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn": self.turn,
            "model": self.model,
            "provider": self.provider,
            "confidence": self.confidence,
            "untrusted_source": self.untrusted_source,
            "redacted": self.redacted,
            "claim_downgraded": self.claim_downgraded,
            "quarantined": self.quarantined,
            "gate_reason": self.gate_reason,
            "gate_severity": self.gate_severity,
            "gate_findings": [dict(item) for item in self.gate_findings],
            "applicable": self.applicable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProposedMemory":
        """Rebuild a proposal from a patch file.

        Defensive on every field: a patch file is a review artifact a human may
        have edited, so a malformed one degrades to a refused read rather than
        raising into the caller's face.
        """
        if not isinstance(data, Mapping):
            raise ValueError("a proposed memory must be a JSON object")
        text = str(data.get("text") or "")
        turn = data.get("turn")
        try:
            safe_turn = int(turn) if turn is not None else None
        except (TypeError, ValueError):
            safe_turn = None
        confidence = data.get("confidence")
        try:
            safe_confidence = (
                float(confidence) if confidence is not None else None
            )
        except (TypeError, ValueError):
            safe_confidence = None
        evidence = data.get("evidence")
        findings = data.get("gate_findings")
        return cls(
            text=text,
            category=str(data.get("category") or "general"),
            rationale=str(data.get("rationale") or ""),
            evidence=tuple(
                str(item) for item in (evidence if isinstance(evidence, list) else [])
            ),
            source=str(data.get("source") or "extraction"),
            session_id=str(data.get("session_id") or ""),
            run_id=str(data.get("run_id") or ""),
            turn=safe_turn,
            model=str(data.get("model") or ""),
            provider=str(data.get("provider") or ""),
            confidence=safe_confidence,
            untrusted_source=bool(data.get("untrusted_source")),
            redacted=bool(data.get("redacted")),
            claim_downgraded=bool(data.get("claim_downgraded")),
            quarantined=bool(data.get("quarantined")),
            gate_reason=str(data.get("gate_reason") or ""),
            gate_severity=str(data.get("gate_severity") or ""),
            gate_findings=tuple(
                dict(item)
                for item in (findings if isinstance(findings, list) else [])
                if isinstance(item, Mapping)
            ),
            index=int(data.get("index") or 0),
        )


@dataclass(frozen=True)
class InboxProposal:
    """A patch file's worth of proposals, with its review state."""

    proposal_id: str
    state: ProposalState
    items: Tuple[ProposedMemory, ...]
    repo_path: str
    created_at: str
    path: str
    applied_ids: Tuple[Any, ...] = ()
    applied_item_indexes: Tuple[int, ...] = ()
    applied_at: str = ""
    applied_by: str = ""
    rejected_reason: str = ""
    digest: str = ""
    byte_size: int = 0

    @property
    def applicable_items(self) -> Tuple[ProposedMemory, ...]:
        """Items a human may apply (quarantined ones excluded)."""
        return tuple(item for item in self.items if item.applicable)

    @property
    def quarantined_items(self) -> Tuple[ProposedMemory, ...]:
        """Items the gate quarantined; visible, never appliable."""
        return tuple(item for item in self.items if item.quarantined)

    @property
    def committed(self) -> bool:
        """Whether this proposal has been applied."""
        return self.state is ProposalState.APPLIED

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible review projection."""
        return {
            "proposal_id": self.proposal_id,
            "state": self.state.value,
            "repo_path": self.repo_path,
            "created_at": self.created_at,
            "path": self.path,
            "digest": self.digest,
            "byte_size": self.byte_size,
            "items": [item.as_dict() for item in self.items],
            "applicable_items": len(self.applicable_items),
            "quarantined_items": len(self.quarantined_items),
            "untrusted_items": sum(
                1 for item in self.items if item.untrusted_source
            ),
            "applied_ids": list(self.applied_ids),
            "applied_item_indexes": list(self.applied_item_indexes),
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
            "rejected_reason": self.rejected_reason,
            "persisted": self.committed,
        }

    def render(self) -> str:
        """Render the human review view.

        This is the surface the whole trust boundary exists for, so it states
        every gate fact per item rather than only the ones that blocked it: a
        reviewer who cannot see that item 3 was REDACTED cannot judge whether
        to apply it.
        """
        lines = [
            f"proposal {self.proposal_id} [{self.state.value}] "
            f"{len(self.items)} item(s) -> {self.path}"
        ]
        for item in self.items:
            flags = []
            if item.untrusted_source:
                flags.append("UNTRUSTED SOURCE")
            if item.quarantined:
                flags.append(f"QUARANTINED: {item.gate_reason or 'no reason given'}")
            if item.redacted:
                flags.append("REDACTED")
            if item.claim_downgraded:
                flags.append("CLAIM DOWNGRADED")
            marker = (" [" + "; ".join(flags) + "]") if flags else ""
            lines.append(f"  {item.index}. ({item.category}){marker} {item.text}")
            if item.rationale:
                lines.append(f"       why: {item.rationale}")
        if self.state is ProposalState.APPLIED:
            lines.append(
                f"  APPLIED at {self.applied_at} by {self.applied_by} -> "
                f"store ids {list(self.applied_ids)}"
            )
        elif self.state is ProposalState.REJECTED:
            lines.append(f"  REJECTED: {self.rejected_reason}")
        else:
            lines.append(
                "  NOTHING IS STORED YET. Apply with "
                "MemoryInbox.apply(proposal_id, actor='operator') or discard "
                "with reject()."
            )
        return "\n".join(lines)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialise deterministically.

    Canonical means ``sort_keys`` + fixed separators + ``ensure_ascii=False``,
    so the same proposal bytes the same way twice and ``git diff`` on the inbox
    shows a real change rather than key reordering.
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _redact_finding(finding: Any) -> Dict[str, Any]:
    """Redact a security finding's free-text fields before it is written.

    ``shared.security.detect_prompt_injection`` returns an ``evidence`` field
    holding the MATCHED TEXT. That is the right thing for an in-memory receipt
    a human is actively reading and the wrong thing for a durable review
    artifact, because the artifact outlives the reading. Credentials in it are
    redacted here; the instruction text itself is preserved because a reviewer
    cannot classify an injection attempt they cannot see, and the artifact is
    quarantined content by construction.
    """
    row = dict(finding) if isinstance(finding, Mapping) else {"value": str(finding)}
    for key in ("evidence", "excerpt", "match", "text", "snippet"):
        value = row.get(key)
        if isinstance(value, str) and value:
            row[key] = redact_text(value)
    return row


class MemoryInbox:
    """A per-project ``.inbox/`` of proposals awaiting a human.

    Assumes the parent of ``root`` is writable. Never raises for an I/O or
    content problem: every method returns a receipt carrying ``ok`` and
    ``reason``, because a review surface that crashes on a malformed patch file
    is a review surface nobody trusts.
    """

    def __init__(self, root: Any, *, repo_path: Any = None) -> None:
        """Point at one project's inbox.

        ``root`` is the directory that will CONTAIN the ``.inbox`` directory, so
        both the corpus layout (``root=<repo>``) and the default layout
        (``root=<memory_root>/inbox/<repo-key>``) produce a ``.inbox`` child.
        """
        base = Path(str(root)).expanduser()
        self.repo_path = str(repo_path) if repo_path else ""
        self.root = base / INBOX_DIRNAME
        self._pending = self.root
        self._applied = self.root / APPLIED_DIRNAME
        self._rejected = self.root / REJECTED_DIRNAME

    # -- layout ---------------------------------------------------------

    def ensure(self) -> Dict[str, Any]:
        """Create the inbox directories. Reports rather than raises."""
        created: List[str] = []
        for path in (self.root, self._applied, self._rejected):
            try:
                if not path.exists():
                    path.mkdir(parents=True, exist_ok=True)
                    created.append(path.name)
            except OSError as exc:
                return {
                    "ok": False,
                    "root": str(self.root),
                    "created": created,
                    "reason": f"could not create {path}: {type(exc).__name__}",
                }
        return {"ok": True, "root": str(self.root), "created": created, "reason": ""}

    def _path_for(self, proposal_id: str, state: ProposalState) -> Path:
        directory = {
            ProposalState.PROPOSED: self._pending,
            ProposalState.APPLIED: self._applied,
            ProposalState.REJECTED: self._rejected,
        }[state]
        return directory / f"{proposal_id}{PATCH_SUFFIX}"

    # -- write ----------------------------------------------------------

    def propose(
        self,
        memories: Sequence[Any],
        *,
        session_id: Any = "",
        run_id: Any = "",
        model: Any = "",
        provider: Any = "",
        proposal_id: Any = "",
        actor: str = "agent",
        at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Write proposals to a canonical patch file. PERSISTS NOTHING.

        Each memory may be a :class:`ProposedMemory`, a mapping, or a bare
        string. Everything is passed through
        :func:`shared.security.authorize_memory_write` HERE, before the bytes
        reach the disk -- which is the "redaction before write" the phase
        requires, and it means even the PROPOSAL FILE cannot hold a credential.

        A quarantined item is written into the patch file marked
        ``quarantined: true`` rather than dropped. The attempt is evidence; the
        drop would hide it.

        Returns a receipt with the proposal id and the path. ``persisted`` is
        always ``False`` here and is present so a caller does not have to know
        the rule to check it.
        """
        layout = self.ensure()
        if not layout["ok"]:
            return {
                "ok": False,
                "proposal_id": "",
                "path": "",
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "reason": layout["reason"],
            }

        prepared: List[ProposedMemory] = []
        seen: set[str] = set()
        rejected_empty = 0
        suppressed = self.rejected_digests()
        suppressed_hits = 0
        for position, raw in enumerate(memories or ()):
            candidate = self._prepare_one(
                raw,
                position=position,
                session_id=session_id,
                run_id=run_id,
                model=model,
                provider=provider,
                actor=actor,
            )
            if candidate is None:
                rejected_empty += 1
                continue
            if candidate.content_digest in suppressed:
                # A human REJECTED this exact memory. Re-proposing it on the
                # next extraction run would make the gate decorative: the
                # reviewer's decision would be undone by the next session, and
                # the queue would refill with something already answered.
                # Suppression is by CONTENT DIGEST, so a re-worded variant is
                # still proposed -- the human rejected a memory, not a topic.
                suppressed_hits += 1
                continue
            if candidate.digest in seen:
                # Proposal-level dedupe: the same extracted sentence twice in one
                # session is one proposal, and a reviewer asked to approve the
                # same line twice is how a review queue gets rubber-stamped.
                continue
            seen.add(candidate.digest)
            prepared.append(candidate)

        if not prepared:
            return {
                "ok": True,
                "proposal_id": "",
                "path": "",
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "skipped_empty": rejected_empty,
                "suppressed_by_rejection": suppressed_hits,
                # The digest-set size and the suppressed-ITEM count are
                # different numbers and this receipt used to publish the first
                # under the second's name: a one-item proposal whose memory had
                # been rejected reported "2 suppressed", because one rejected
                # item contributes both its content digest and its session
                # digest. A count of what was suppressed that is really a count
                # of what is known cannot be read by anyone downstream.
                "rejected_digests_known": len(suppressed),
                "reason": (
                    "every item was suppressed by an earlier human rejection"
                    if suppressed_hits
                    else "nothing proposable was supplied"
                ),
            }

        stamp = _utc_now() if at is None else time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(float(at))
        ) + "Z"
        pid = str(proposal_id or "").strip()
        if not pid or not _ID_RE.match(pid):
            seed = hashlib.sha256(
                "\x1f".join(item.digest for item in prepared).encode("utf-8", "replace")
            ).hexdigest()[:16]
            pid = f"prop-{seed}"
        if not _ID_RE.match(pid):
            return {
                "ok": False,
                "proposal_id": "",
                "path": "",
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "reason": f"unusable proposal id {pid!r}",
            }

        payload: Dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "proposal_id": pid,
            "state": ProposalState.PROPOSED.value,
            "repo_path": self.repo_path,
            "created_at": stamp,
            "session_id": str(session_id or ""),
            "run_id": str(run_id or ""),
            "model": str(model or ""),
            "provider": str(provider or ""),
            "actor": str(actor or ""),
            "persisted": False,
            "applied_ids": [],
            "applied_at": "",
            "applied_by": "",
            "rejected_reason": "",
            "items": [item.as_dict() for item in prepared],
        }
        body = _canonical_json(payload)
        if len(body) > MAX_PATCH_BYTES:
            return {
                "ok": False,
                "proposal_id": pid,
                "path": "",
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "reason": (
                    f"the proposal is {len(body)} bytes, over the "
                    f"{MAX_PATCH_BYTES}-byte patch-file bound; it was NOT written "
                    "in truncated form, because a review artifact that silently "
                    "dropped half its items is not reviewable"
                ),
            }
        target = self._path_for(pid, ProposalState.PROPOSED)
        if target.exists():
            return {
                "ok": False,
                "proposal_id": pid,
                "path": str(target),
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "reason": (
                    f"a proposal with id {pid!r} already exists at {target}; "
                    "propose() does not overwrite a pending review"
                ),
            }
        try:
            target.write_text(body + "\n", encoding="utf-8")
        except OSError as exc:
            return {
                "ok": False,
                "proposal_id": pid,
                "path": str(target),
                "persisted": False,
                "items": 0,
                "quarantined": 0,
                "reason": f"could not write the patch file: {type(exc).__name__}",
            }
        return {
            "ok": True,
            "proposal_id": pid,
            "path": str(target),
            "persisted": False,
            "items": len(prepared),
            "quarantined": sum(1 for item in prepared if item.quarantined),
            "untrusted": sum(1 for item in prepared if item.untrusted_source),
            "skipped_empty": rejected_empty,
            "suppressed_by_rejection": suppressed_hits,
            "rejected_digests_known": len(suppressed),
            "bytes": len(body),
            "reason": "",
        }

    def _prepare_one(
        self,
        raw: Any,
        *,
        position: int,
        session_id: Any,
        run_id: Any,
        model: Any,
        provider: Any,
        actor: str,
    ) -> Optional[ProposedMemory]:
        """Normalise one input into a gated, redacted :class:`ProposedMemory`.

        The gate call is the reason this method exists separately: it must run
        before anything is serialised, so neither the patch file nor an
        exception message can carry a credential.
        """
        if isinstance(raw, ProposedMemory):
            text = raw.text
            category = raw.category
            rationale = raw.rationale
            evidence = raw.evidence
            source = raw.source
            session = raw.session_id or str(session_id or "")
            run = raw.run_id or str(run_id or "")
            turn = raw.turn
            row_model = raw.model or str(model or "")
            row_provider = raw.provider or str(provider or "")
            confidence = raw.confidence
            untrusted = raw.untrusted_source
        elif isinstance(raw, Mapping):
            text = str(raw.get("text") or "")
            category = str(raw.get("category") or "general")
            rationale = str(raw.get("rationale") or "")
            raw_evidence = raw.get("evidence")
            evidence = tuple(
                str(item)
                for item in (
                    raw_evidence if isinstance(raw_evidence, list) else []
                )
            )
            source = str(raw.get("source") or "extraction")
            session = str(raw.get("session_id") or session_id or "")
            run = str(raw.get("run_id") or run_id or "")
            turn = raw.get("turn")
            try:
                turn = int(turn) if turn is not None else None
            except (TypeError, ValueError):
                turn = None
            row_model = str(raw.get("model") or model or "")
            row_provider = str(raw.get("provider") or provider or "")
            confidence_raw = raw.get("confidence")
            try:
                confidence = (
                    float(confidence_raw) if confidence_raw is not None else None
                )
            except (TypeError, ValueError):
                confidence = None
            untrusted = bool(raw.get("untrusted_source"))
        else:
            text = str(raw or "")
            category, rationale, evidence = "general", "", ()
            source, session, run, turn = "extraction", str(session_id or ""), str(
                run_id or ""
            ), None
            row_model, row_provider, confidence, untrusted = (
                str(model or ""),
                str(provider or ""),
                None,
                False,
            )

        stripped = text.strip()
        if not stripped:
            return None
        if len(stripped) > MAX_PROPOSAL_TEXT_CHARS:
            return None

        decision = authorize_memory_write(
            stripped,
            source=source,
            actor=actor,
            provenance={
                "kind": "extraction",
                "source": source,
                "session_id": session,
                "run_id": run,
                "model": row_model,
                "provider": row_provider,
                "turn": turn,
                "explicit": True,
            },
        )
        gate_text = str(decision.text or "")
        redacted = gate_text != stripped and bool(gate_text)
        quarantined = bool(decision.quarantined) or not decision.allowed
        # The findings carry an `evidence` field holding the MATCHED TEXT of the
        # injection attempt. Serialising that verbatim puts the raw hostile
        # payload into the patch file even though `text` is empty — so the
        # review artifact would carry the very bytes the redaction removed from
        # the memory. Redaction before write applies to the WHOLE record, not
        # only to its text field.
        gate_findings = tuple(
            _redact_finding(dict(item)) for item in decision.findings
        )
        downgraded, final_category = self._downgrade_claim(
            gate_text or stripped, category
        )
        return ProposedMemory(
            text=gate_text,
            category=final_category,
            rationale=rationale,
            evidence=evidence,
            source=source,
            session_id=session,
            run_id=run,
            turn=turn,
            model=row_model,
            provider=row_provider,
            confidence=confidence,
            untrusted_source=untrusted,
            redacted=redacted,
            claim_downgraded=downgraded,
            quarantined=quarantined,
            gate_reason="" if not quarantined else str(decision.reason),
            gate_severity=str(decision.severity or ""),
            gate_findings=tuple(gate_findings),
            index=position,
        )

    @staticmethod
    def _downgrade_claim(text: str, category: str) -> Tuple[bool, str]:
        """Downgrade an unverified CLAIM to ``category="observation"``.

        The rule is re-pinned here rather than imported from
        ``harness.knowledge`` (``harness/**`` is not this terminal's file, and
        ``memory`` may not depend on ``harness`` -- the dependency direction is
        cli -> memory). The patterns are the historical six; the shared
        constants this duplicates are recorded as a cross-terminal request in
        ``INTERFACES.md`` so ``harness.knowledge`` can delegate here and the two
        copies can become one.

        Text that REPORTS an outcome keeps its category -- a report of a failing
        command is evidence, and evidence is not a claim.
        """
        normalized = text.casefold()
        for pattern in _UNVERIFIED_CLAIM_PATTERNS:
            if pattern.search(normalized):
                return True, "observation"
        return False, (category or "general")

    # -- read -----------------------------------------------------------

    def list_proposals(
        self, *, state: Optional[ProposalState] = None
    ) -> List[InboxProposal]:
        """List proposals, newest first, optionally filtered by state."""
        results: List[InboxProposal] = []
        states = [state] if state is not None else list(ProposalState)
        for one in states:
            directory = self._path_for("x", one).parent
            try:
                entries = sorted(directory.glob(f"*{PATCH_SUFFIX}"))
            except OSError:
                continue
            for entry in entries:
                loaded = self._load(entry, expected_state=one)
                if loaded is not None:
                    results.append(loaded)
        results.sort(key=lambda item: (item.created_at, item.proposal_id), reverse=True)
        return results

    def pending(self) -> List[InboxProposal]:
        """List only the proposals awaiting a human decision."""
        return self.list_proposals(state=ProposalState.PROPOSED)

    def _load(
        self, path: Path, *, expected_state: Optional[ProposalState] = None
    ) -> Optional[InboxProposal]:
        """Read one patch file. Returns ``None`` for anything unusable."""
        try:
            if path.is_symlink() or not path.is_file():
                return None
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if len(raw) > MAX_PATCH_BYTES:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, Mapping):
            return None
        state_raw = str(data.get("state") or "")
        try:
            state = ProposalState(state_raw)
        except ValueError:
            state = expected_state or ProposalState.PROPOSED
        items: List[ProposedMemory] = []
        for entry in data.get("items") or []:
            try:
                items.append(ProposedMemory.from_dict(entry))
            except (ValueError, TypeError):
                continue
        pid = str(data.get("proposal_id") or path.stem)
        return InboxProposal(
            proposal_id=pid,
            state=state,
            items=tuple(items),
            repo_path=str(data.get("repo_path") or ""),
            created_at=str(data.get("created_at") or ""),
            path=str(path),
            applied_ids=tuple(data.get("applied_ids") or []),
            applied_item_indexes=tuple(
                int(value)
                for value in (data.get("applied_item_indexes") or [])
                if isinstance(value, (int, float))
            ),
            applied_at=str(data.get("applied_at") or ""),
            applied_by=str(data.get("applied_by") or ""),
            rejected_reason=str(data.get("rejected_reason") or ""),
            digest=str(data.get("digest") or ""),
            byte_size=len(raw),
        )

    def get(self, proposal_id: Any) -> Optional[InboxProposal]:
        """Return one proposal by id, searched across all three states."""
        pid = str(proposal_id or "").strip()
        if not pid or not _ID_RE.match(pid):
            return None
        for state in ProposalState:
            loaded = self._load(self._path_for(pid, state), expected_state=state)
            if loaded is not None:
                return loaded
        return None

    def status(self, proposal_id: Any) -> Dict[str, Any]:
        """Report whether a proposal is still pending, applied, or rejected.

        The "did this land?" question, answered without inferring it from a
        row count. ``committed`` is the field that matters: it is ``True`` only
        when this module actually wrote rows.
        """
        proposal = self.get(proposal_id)
        if proposal is None:
            return {
                "found": False,
                "committed": False,
                "state": "",
                "reason": f"no proposal with id {proposal_id!r}",
            }
        return {
            "found": True,
            "committed": proposal.committed,
            "state": proposal.state.value,
            "proposal_id": proposal.proposal_id,
            "applied_ids": list(proposal.applied_ids),
            "applied_at": proposal.applied_at,
            "applied_by": proposal.applied_by,
            "items": len(proposal.items),
            "path": proposal.path,
            "reason": "",
        }

    # -- the ONE path to the store --------------------------------------

    def apply(
        self,
        proposal_id: Any,
        store: Any,
        *,
        actor: Any = "operator",
        only: Optional[Sequence[int]] = None,
        reason: Any = "",
    ) -> Dict[str, Any]:
        """Commit a human-approved proposal into the decision store.

        **This is the only method in this module that can write a memory.**
        ``store`` is required and is not defaulted, so a caller cannot apply
        "by accident" against an implicit global.

        ``actor`` must be a recognised operator class -- ``operator``, ``human``
        or ``user-confirmed``. The gate in
        :func:`shared.security.authorize_memory_write` already treats those as
        operator actors; this module requires the caller to SAY which one, so
        the audit trail names a human role rather than a generic string.

        ``only`` restricts application to a subset of item indices, which is how
        a reviewer approves two of five. Every item is re-gated on the way in:
        the gate ran at proposal time, possibly days earlier, and a proposal
        file is an editable artifact.
        """
        proposal = self.get(proposal_id)
        if proposal is None:
            return {
                "ok": False,
                "committed": 0,
                "applied_ids": [],
                "reason": f"no proposal with id {proposal_id!r}",
            }
        if proposal.committed:
            return {
                "ok": True,
                "committed": 0,
                "applied_ids": list(proposal.applied_ids),
                "reason": (
                    f"proposal {proposal.proposal_id} was already applied at "
                    f"{proposal.applied_at}; apply() is idempotent and wrote "
                    "nothing this time"
                ),
                "already_applied": True,
            }
        actor_key = str(actor or "").strip().casefold()
        if actor_key not in {"operator", "human", "user-confirmed"}:
            return {
                "ok": False,
                "committed": 0,
                "applied_ids": [],
                "reason": (
                    f"actor {actor!r} is not an operator class; applying a "
                    "proposal is a human decision and must be recorded as one "
                    "of 'operator', 'human', 'user-confirmed'"
                ),
            }

        wanted = None
        if only is not None:
            wanted = set()
            for value in only:
                try:
                    wanted.add(int(value))
                except (TypeError, ValueError):
                    continue

        applied_ids: List[Any] = []
        refused: List[Dict[str, Any]] = []
        quarantined: List[Dict[str, Any]] = []
        applied_indices: List[int] = []
        already_done = set(proposal.applied_item_indexes)
        for item in proposal.items:
            if item.index in already_done:
                # An item already committed by an earlier partial apply is
                # skipped silently rather than re-reported as a fresh success:
                # otherwise the second call's `committed` count would include
                # rows that were written the first time.
                continue
            if wanted is not None and item.index not in wanted:
                continue
            if item.quarantined:
                # Reported in its OWN list, not in `refused`. A quarantined item
                # is permanently unapplicable, so counting it as a refusal
                # would leave `complete` false forever and the proposal would
                # stay pending for ever over an item no human can ever act on.
                quarantined.append(
                    {
                        "index": item.index,
                        "reason": (
                            "the security gate quarantined this item at proposal "
                            f"time and a quarantined item is never appliable: "
                            f"{item.gate_reason}"
                        ),
                    }
                )
                continue
            gate = authorize_memory_write(
                item.text,
                source=item.source,
                actor=actor_key,
                provenance={
                    "kind": "inbox-applied",
                    "source": item.source,
                    "session_id": item.session_id,
                    "run_id": item.run_id,
                    "turn": item.turn,
                    "model": item.model,
                    "provider": item.provider,
                    "explicit": True,
                    "proposal_id": proposal.proposal_id,
                },
            )
            if gate.quarantined or not gate.allowed:
                refused.append(
                    {
                        "index": item.index,
                        "reason": (
                            "the item was quarantined when re-gated on the way "
                            f"into the store: {gate.reason}"
                        ),
                    }
                )
                continue
            try:
                row_id = store.record(
                    str(gate.text),
                    category=item.category,
                    source=item.source,
                    task_id=item.run_id or None,
                    repo_path=proposal.repo_path or None,
                    provenance={
                        "kind": "inbox-applied",
                        "source": item.source,
                        "session_id": item.session_id,
                        "run_id": item.run_id,
                        "turn": item.turn,
                        "model": item.model,
                        "provider": item.provider,
                        "explicit": True,
                        "proposal_id": proposal.proposal_id,
                        "untrusted_source": item.untrusted_source,
                        "claim_downgraded": item.claim_downgraded,
                    },
                    metadata={
                        "proposal_id": proposal.proposal_id,
                        "rationale": item.rationale[:512] or None,
                    },
                )
            except Exception as exc:  # a store failure must not lose the patch
                refused.append(
                    {
                        "index": item.index,
                        "reason": f"the store refused the write: {type(exc).__name__}",
                    }
                )
                continue
            if row_id is None:
                refused.append(
                    {
                        "index": item.index,
                        "reason": (
                            "the store returned no id; the row's text was "
                            "rejected as unsafe or malformed"
                        ),
                    }
                )
                continue
            applied_ids.append(row_id)
            applied_indices.append(item.index)

        # A PARTIAL approval must leave the remainder PENDING. Retiring a
        # proposal whose applicable items were merely not selected would drop
        # them on the floor with no record, which is the exact outcome a human
        # reviewer is entitled to assume did not happen.
        quarantined_indices = {entry["index"] for entry in quarantined}
        redacted_indices = {
            item.index for item in proposal.items if item.redacted
        }
        # A REDACTED, credential-shaped item is permanently unapplicable too:
        # the decision store's own `contains_secret` gate refuses
        # `api_key = [REDACTED_SECRET]` for the same reason it refuses the raw
        # form, so retrying it can never succeed. Leaving it in `remaining`
        # would keep the proposal pending for ever over an item no human can
        # ever act on -- which is the same defect the quarantine rule fixes.
        resolved = (
            already_done
            | set(applied_indices)
            | quarantined_indices
            | redacted_indices
        )
        remaining = [
            item.index
            for item in proposal.applicable_items
            if item.index not in resolved
        ]
        stamp = _utc_now()
        # A refusal only keeps the proposal OPEN if retrying it could ever work.
        # A permanently-unapplicable item (quarantined by the gate, or a
        # redacted credential the store refuses by construction) is reported but
        # does not block completion -- otherwise the queue never drains.
        blocking_refusals = [
            entry for entry in refused if entry["index"] not in resolved
        ]
        complete = not blocking_refusals and not remaining
        if complete:
            self._retire(
                proposal,
                state=ProposalState.APPLIED,
                applied_ids=applied_ids,
                applied_indices=applied_indices,
                applied_at=stamp,
                applied_by=actor_key,
            )
        else:
            self._annotate(
                proposal,
                applied_ids=applied_ids,
                applied_indices=applied_indices,
                applied_at=stamp,
                applied_by=actor_key,
            )
        if blocking_refusals:
            reason = (
                f"{len(blocking_refusals)} item(s) were refused and could be "
                "retried; the proposal stays pending"
            )
        elif remaining:
            reason = (
                f"{len(remaining)} applicable item(s) were not approved and "
                "remain pending; the proposal stays pending"
            )
        elif refused or quarantined:
            reason = (
                f"committed; {len(quarantined)} quarantined and {len(refused)} "
                "store-refused item(s) were reported and are permanently "
                "unapplicable"
            )
        else:
            reason = ""
        return {
            "ok": bool(applied_ids) or not refused,
            "committed": len(applied_ids),
            "applied_ids": applied_ids,
            "applied_indices": applied_indices,
            "remaining_indices": remaining,
            "refused": refused,
            "quarantined": quarantined,
            "permanently_unapplicable": sorted(quarantined_indices | redacted_indices),
            "proposal_id": proposal.proposal_id,
            "complete": complete,
            "reason": reason,
        }

    def _annotate(
        self,
        proposal: InboxProposal,
        *,
        applied_ids: Sequence[Any],
        applied_indices: Sequence[int],
        applied_at: str,
        applied_by: str,
    ) -> bool:
        """Record a PARTIAL application in place, leaving the state ``proposed``.

        The already-applied ids are written back into the patch file so a second
        ``apply()`` call with a different ``only`` set does not re-commit them.
        Idempotence at the item level, not just the proposal level.
        """
        target = Path(proposal.path)
        already = list(proposal.applied_ids) + [
            value for value in applied_ids if value not in proposal.applied_ids
        ]
        already_indices = list(proposal.applied_item_indexes) + [
            value
            for value in applied_indices
            if value not in proposal.applied_item_indexes
        ]
        payload: Dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "proposal_id": proposal.proposal_id,
            "state": ProposalState.PROPOSED.value,
            "repo_path": proposal.repo_path,
            "created_at": proposal.created_at,
            "applied_ids": already,
            "applied_item_indexes": already_indices,
            "applied_at": applied_at,
            "applied_by": applied_by,
            "rejected_reason": "",
            "persisted": bool(already),
            "partial": True,
            "items": [item.as_dict() for item in proposal.items],
        }
        try:
            target.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
        except OSError:
            return False
        return True

    def reject(
        self, proposal_id: Any, *, reason: Any = "", actor: Any = "operator"
    ) -> Dict[str, Any]:
        """Discard a proposal without committing anything.

        The patch file is MOVED to ``rejected/`` rather than deleted, so a later
        question -- "did the extractor propose this and someone throw it away?"
        -- has an answer.
        """
        proposal = self.get(proposal_id)
        if proposal is None:
            return {
                "ok": False,
                "reason": f"no proposal with id {proposal_id!r}",
                "state": "",
            }
        if proposal.committed:
            return {
                "ok": False,
                "reason": (
                    f"proposal {proposal.proposal_id} is already applied; use "
                    "revoke on the committed rows rather than rejecting the "
                    "proposal that produced them"
                ),
                "state": proposal.state.value,
            }
        self._retire(
            proposal,
            state=ProposalState.REJECTED,
            rejected_reason=str(reason or "no reason given"),
            applied_by=str(actor or ""),
        )
        return {
            "ok": True,
            "reason": "",
            "state": ProposalState.REJECTED.value,
            "proposal_id": proposal.proposal_id,
        }

    def _retire(
        self,
        proposal: InboxProposal,
        *,
        state: ProposalState,
        applied_ids: Sequence[Any] = (),
        applied_indices: Sequence[int] = (),
        applied_at: str = "",
        applied_by: str = "",
        rejected_reason: str = "",
    ) -> bool:
        """Move a patch file into its terminal directory, updating its state."""
        source = Path(proposal.path)
        target = self._path_for(proposal.proposal_id, state)
        payload: Dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "proposal_id": proposal.proposal_id,
            "state": state.value,
            "repo_path": proposal.repo_path,
            "created_at": proposal.created_at,
            "retired_at": _utc_now(),
            "applied_ids": list(applied_ids),
            "applied_item_indexes": list(applied_indices),
            "applied_at": applied_at,
            "applied_by": applied_by,
            "rejected_reason": rejected_reason,
            "persisted": state is ProposalState.APPLIED,
            "items": [item.as_dict() for item in proposal.items],
        }
        try:
            self.ensure()
            target.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
        except OSError:
            return False
        try:
            source.unlink()
        except OSError:
            return False
        return True

    # -- introspection --------------------------------------------------

    def rejected_digests(self) -> set[str]:
        """Content digests a human has REJECTED, read from ``rejected/``.

        This is what makes a rejection STICKY. Without it the next extraction
        run re-proposes the identical memory, the reviewer answers the same
        question twice, and a gate that a queue can refill is not a gate.

        The suppression key is the memory's own :attr:`ProposedMemory.digest`,
        which is computed from the normalised text AFTER redaction. That has two
        consequences worth stating: a redaction that changes the text changes
        the digest (so a credential-bearing rejection suppresses the same
        credential-bearing memory), and a RE-WORDED variant is still proposed,
        because a human rejected a memory rather than a topic.

        An unreadable or corrupt retired file is skipped, never fatal: a
        proposal must not fail to be written because a rejection record is
        damaged. The count of skipped files is published in
        :meth:`rejection_backlog`.
        """
        digests: set[str] = set()
        directory = self._rejected
        try:
            entries = sorted(directory.glob(f"*{PATCH_SUFFIX}"))
        except OSError:
            return digests
        for entry in entries:
            try:
                payload = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            if str(payload.get("state") or "") != ProposalState.REJECTED.value:
                continue
            for item in payload.get("items") or ():
                if not isinstance(item, Mapping):
                    continue
                # content_digest is the cross-session key; digest is the
                # within-session one and is read too so a patch file written
                # before this field existed still suppresses its own content.
                for key in ("content_digest", "digest"):
                    digest = str(item.get(key) or "").strip()
                    if digest:
                        digests.add(digest)
        return digests

    def rejection_backlog(self) -> Dict[str, Any]:
        """How many rejections are on record, and the oldest one's age.

        A rejection list nobody reads is not a decision, it is a growing
        directory. The backlog is reported so the gate's own retention has a
        number attached to it.
        """
        skipped = 0
        rejected_files = 0
        oldest: str = ""
        oldest_epoch = 0.0
        try:
            entries = sorted(self._rejected.glob(f"*{PATCH_SUFFIX}"))
        except OSError:
            entries = []
        import time as _time

        for entry in entries:
            try:
                payload = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                skipped += 1
                continue
            if not isinstance(payload, Mapping):
                skipped += 1
                continue
            rejected_files += 1
            stamp = str(payload.get("retired_at") or payload.get("created_at") or "")
            if not stamp:
                continue
            try:
                epoch = _time.mktime(_time.strptime(stamp.rstrip("Z"), "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, OverflowError):
                continue
            if not oldest_epoch or epoch < oldest_epoch:
                oldest_epoch = epoch
                oldest = stamp
        age_days = (
            round((_time.time() - oldest_epoch) / 86400.0, 3) if oldest_epoch else None
        )
        return {
            "rejected_files": rejected_files,
            "unreadable_files": skipped,
            "oldest_rejected_at": oldest,
            "oldest_age_days": age_days,
            "note": (
                "the backlog is the number of memories a human has already "
                "answered NO to; it is not a queue anybody is waiting on"
            ),
        }

    def audit(self) -> Dict[str, Any]:
        """Return counts per state, for a status surface.

        ``pending_items`` is the number the operator actually cares about, and
        it is separated from ``pending`` (the number of FILES) because "12
        memories await review" and "12 files await review" are different
        statements about the same queue.
        """
        counts: Dict[str, int] = {state.value: 0 for state in ProposalState}
        item_counts: Dict[str, int] = {state.value: 0 for state in ProposalState}
        quarantined = 0
        untrusted = 0
        try:
            for proposal in self.list_proposals():
                counts[proposal.state.value] = counts.get(proposal.state.value, 0) + 1
                item_counts[proposal.state.value] += len(proposal.items)
                quarantined += len(proposal.quarantined_items)
                untrusted += sum(1 for item in proposal.items if item.untrusted_source)
        except OSError:
            pass
        return {
            "root": str(self.root),
            "exists": self.root.is_dir(),
            "counts": counts,
            "item_counts": item_counts,
            "pending": counts.get(ProposalState.PROPOSED.value, 0),
            "pending_items": item_counts.get(ProposalState.PROPOSED.value, 0),
            "quarantined_items": quarantined,
            "untrusted_items": untrusted,
            "persisted_by_this_module_only": True,
        }


#: The six unverified-claim patterns, re-pinned here so the store-facing half of
#: the memory layer does not depend on ``harness``. See
#: :meth:`MemoryInbox._downgrade_claim` for why they are duplicated rather than
#: imported, and ``INTERFACES.md`` for the de-duplication request.
_UNVERIFIED_CLAIM_PATTERNS = (
    re.compile(
        r"\b(?:verified|confirmed|proved|proven|guaranteed)\b[^.]{0,40}\btests?\b"
    ),
    re.compile(r"\b(?:tests?|suite|build|ci)\b[^.]{0,30}\b(?:passes|passed|passing)\b"),
    re.compile(r"\b(?:the fix|this fix|the change|this change|the patch|this patch)\b"
               r"[^.]{0,30}\b(?:is|was)\b[^.]{0,20}\b(?:correct|right|good)\b"),
    re.compile(r"\b(?:definitely|certainly|100%\s*sure|guaranteed to)\b"),
    re.compile(r"\b(?:should always|will never|never work|always works)\b"),
    re.compile(r"\b(?:no longer broken|no longer needed|is no longer required)\b"),
)
