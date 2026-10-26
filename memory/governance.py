"""Memory governance: provenance completeness, untrusted-source marking, export.

WHY THIS IS A SEPARATE MODULE FROM THE STORE
--------------------------------------------
``memory.decision_store`` STORES. This module answers the four questions a
person asks about a memory they did not write:

1. **Where did this come from?** (:func:`provenance_receipt`)
2. **Can I see everything this project remembers?** (:func:`list_memories`)
3. **Can I take it away?** -- delegated to
   :meth:`memory.decision_store.DecisionStore.revoke`, because the store owns
   the tombstone and this module must not be able to bypass it
4. **Can I leave with it all?** (:func:`export_memories`)

Question 4 has a short answer that is worth stating plainly: **memory you cannot
export is memory you do not own.** So the export is total -- every row,
including revoked tombstones, with full provenance, in both JSON and Markdown,
and it never mutates the store.

THE FOUR PROVENANCE FACTS, AND WHAT IS MISSING WHEN THEY ARE
------------------------------------------------------------
The phase requires every memory to carry: which session, which turn, which
source, which model, when. The store's provenance block already carries most of
that when a caller supplies it -- and "when a caller supplies it" is the gap.
This module's contribution is :func:`provenance_receipt`, which reports each of
the four as PRESENT or ABSENT with the reason, rather than rendering an absent
provenance as an empty dict. An empty dict and "we never recorded a session" are
different facts and only the second is an admission.

THE UNTRUSTED-SOURCE RULE, RE-PINNED
-------------------------------------
Phase 2 added two new untrusted inputs (issue text, repository instruction
files) and four pre-existing ones were already scanned. The rules that must
survive, all re-pinned here as TESTS rather than as prose:

* a row claiming system authority is QUARANTINED **for every actor**, including
  an operator -- enforced by
  :func:`shared.security.authorize_memory_write`;
* an unverified outcome claim is DOWNGRADED to ``category="observation"`` with
  ``claim_downgraded`` set;
* a memory from an untrusted source is MARKED as such, and marking is not
  optional: a consumer that wants to know must not have to infer it from the
  source string.

Promotion of an untrusted-sourced memory is allowed only with an EXPLICIT
operator marker (:func:`promote_untrusted`). That is the "cannot be promoted
silently" requirement: silent promotion is the failure, and the fix is that
promotion requires a named human decision rather than merely being possible.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from shared.security import UNTRUSTED_SOURCES

__all__ = [
    "EXPORT_FORMATS",
    "TRUST_TIERS",
    "MemoryExport",
    "ProvenanceReceipt",
    "export_memories",
    "list_memories",
    "mark_untrusted",
    "promote_untrusted",
    "provenance_receipt",
    "trust_tier",
]

#: Trust tiers, ordered strongest-first. A closed vocabulary: "how much do I
#: believe this" must not be a free-text field, because a free-text trust field
#: is one nobody filters on.
TRUST_TIERS: Tuple[str, ...] = ("stated", "observed", "untrusted")

#: The four provenance facts the phase requires per memory, and the provenance
#: keys that satisfy each. A fact with an empty key list is UNSATISFIABLE, which
#: is reported as such rather than as a permanently-absent field.
_REQUIRED_PROVENANCE: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("session_id", ("session_id", "session", "conversation_id")),
    ("turn", ("turn", "turn_index", "step")),
    ("source", ("source", "kind", "provenance_source")),
    ("model", ("model", "model_name")),
)

#: The export formats this module writes. JSON is the machine-complete one and
#: Markdown is the human-readable one; "full export to JSON/Markdown" in the
#: phase brief is satisfied by exactly these two.
EXPORT_FORMATS: Tuple[str, ...] = ("json", "markdown")


@dataclass(frozen=True)
class ProvenanceReceipt:
    """Whether one memory's provenance is complete, and what is missing.

    ``complete`` is the field to branch on. ``missing`` names the gaps so a
    caller can be told WHICH fact was not recorded rather than receiving a bare
    ``False``.
    """

    memory_id: Any
    complete: bool
    present: Tuple[str, ...]
    missing: Tuple[str, ...]
    reasons: Mapping[str, str]
    provenance: Mapping[str, Any]
    source: str
    created_at: str
    revoked: bool
    untrusted_source: bool
    trust_tier: str

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "memory_id": self.memory_id,
            "complete": self.complete,
            "present": list(self.present),
            "missing": list(self.missing),
            "reasons": dict(self.reasons),
            "provenance": dict(self.provenance),
            "source": self.source,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "untrusted_source": self.untrusted_source,
            "trust_tier": self.trust_tier,
        }


def _provenance_of(row: Any) -> Dict[str, Any]:
    if isinstance(row, MappingABC):
        raw = row.get("provenance")
    else:
        raw = getattr(row, "provenance", None)
    if isinstance(raw, MappingABC):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, MappingABC) else {}
    return {}


def _attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, MappingABC):
        return row.get(name, default)
    return getattr(row, name, default)


def mark_untrusted(row: Any, *, untrusted: Optional[bool] = None) -> bool:
    """Return whether a memory's source is untrusted, honouring an override.

    The three untrusted-source signals, in precedence order:

    1. an explicit ``untrusted_source=True`` in the provenance;
    2. an explicit ``trusted_source=True`` in the provenance, which WINS -- the
       operator assertion is the promotion path, so it must beat the channel;
    3. the ``source`` string being one of
       :data:`shared.security.UNTRUSTED_SOURCES`.

    ``untrusted=`` overrides the computed answer, for a caller that already
    established the trust tier some other way. An override of ``False`` is
    honoured: refusing to let a human mark a memory as trusted would make
    ``trusted_source`` in the provenance meaningless.
    """
    if untrusted is not None:
        return bool(untrusted)
    provenance = _provenance_of(row)
    if provenance.get("trusted_source") is True:
        return False
    if provenance.get("untrusted_source") is True:
        return True
    if _attr(row, "trusted_source") is True:
        return False
    if _attr(row, "untrusted_source") is True:
        return True
    source = str(_attr(row, "source") or provenance.get("source") or "")
    return source.strip().casefold() in set(UNTRUSTED_SOURCES)


def trust_tier(row: Any, *, untrusted: Optional[bool] = None) -> str:
    """Return one of :data:`TRUST_TIERS` for a memory.

    ``stated`` means the user said it (``shared.security``'s operator-asserted
    provenance, or one of the manual/operator/user channels); ``untrusted``
    means it came from a channel an attacker could write; ``observed`` is
    everything in between, which is the honest default for an agent's own
    recorded observation.
    """
    if mark_untrusted(row, untrusted=untrusted):
        return "untrusted"
    provenance = _provenance_of(row)
    if provenance.get("trusted_source") is True:
        return "stated"
    source = str(_attr(row, "source") or provenance.get("source") or "")
    if source.strip().casefold() in {"user", "operator", "manual", "human"}:
        return "stated"
    return "observed"


def provenance_receipt(row: Any, *, untrusted: Optional[bool] = None) -> ProvenanceReceipt:
    """Report whether one memory carries the four required provenance facts.

    Never raises. A row with no provenance at all is reported as missing ALL
    FOUR with the reason, which is the honest answer -- an absent provenance
    block is not a provenance block of empty strings.
    """
    provenance = _provenance_of(row)
    present: List[str] = []
    missing: List[str] = []
    reasons: Dict[str, str] = {}
    if not provenance and not _attr(row, "source"):
        reasons["_all"] = (
            "this row carries no provenance block and no source, so which "
            "session, turn, source and model produced it were never recorded"
        )
    for fact, keys in _REQUIRED_PROVENANCE:
        value = None
        for key in keys:
            if key in provenance and provenance.get(key) not in (None, "", []):
                value = provenance[key]
                break
        if value in (None, "", []):
            missing.append(fact)
            if not reasons:
                reasons[fact] = (
                    f"no provenance key in {list(keys)} carries a value; an "
                    "absent fact is reported as absent rather than as an empty "
                    "string"
                )
        else:
            present.append(fact)
    return ProvenanceReceipt(
        memory_id=_attr(row, "id", _attr(row, "memory_id")),
        complete=not missing,
        present=tuple(present),
        missing=tuple(missing),
        reasons=reasons,
        provenance=provenance,
        source=str(_attr(row, "source") or ""),
        created_at=str(_attr(row, "created_at") or ""),
        revoked=bool(_attr(row, "revoked", False)),
        untrusted_source=mark_untrusted(row, untrusted=untrusted),
        trust_tier=trust_tier(row, untrusted=untrusted),
    )


def promote_untrusted(
    store: Any,
    decision_id: Any,
    *,
    actor: Any = "operator",
    note: Any = "",
) -> Dict[str, Any]:
    """Promote an untrusted-sourced memory, WITH a named human decision.

    The requirement is "cannot be promoted SILENTLY", and the fix is not to
    forbid promotion -- a fact from an issue tracker the user has checked is
    exactly what promotion is for -- but to make it require an explicit actor
    and leave a marker. ``actor`` must be an operator class; anything else is
    refused, so no automated path can promote an untrusted memory.

    The promotion is recorded as a NEW row carrying ``trusted_source: True``
    plus the note, and the original is left alone. A mutation in place would
    destroy the evidence that the memory was ever untrusted, which is the fact
    an auditor most needs.
    """
    actor_key = str(actor or "").strip().casefold()
    if actor_key not in {"operator", "human", "user-confirmed"}:
        return {
            "ok": False,
            "promoted": False,
            "decision_id": decision_id,
            "reason": (
                f"actor {actor!r} is not an operator class; promoting an "
                "untrusted memory is a human decision and must be recorded as "
                "one of 'operator', 'human', 'user-confirmed'"
            ),
        }
    row = store.get(decision_id, include_undurable=True)
    if row is None:
        return {
            "ok": False,
            "promoted": False,
            "decision_id": decision_id,
            "reason": f"no memory with id {decision_id!r}",
        }
    base = _provenance_of(row)
    base.update(
        {
            "trusted_source": True,
            "promoted_from_untrusted": True,
            "promoted_by": actor_key,
            "promoted_note": str(note or "")[:500],
            "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    base.pop("untrusted_source", None)
    try:
        new_id = store.record(
            _attr(row, "text"),
            category=_attr(row, "category") or "general",
            source=str(actor_key),
            task_id=_attr(row, "task_id"),
            repo_path=_attr(row, "repo_path"),
            provenance=base,
            metadata={"promoted_from": _attr(row, "id")},
        )
    except TypeError as exc:
        return {
            "ok": False,
            "promoted": False,
            "decision_id": decision_id,
            "reason": f"the store refused the promotion: {exc}"[:200],
        }
    except Exception as exc:
        return {
            "ok": False,
            "promoted": False,
            "decision_id": decision_id,
            "reason": f"the store raised on promotion: {type(exc).__name__}",
        }
    if new_id is None:
        return {
            "ok": False,
            "promoted": False,
            "decision_id": decision_id,
            "reason": (
                "the store returned no id for the promoted row; its text was "
                "refused as unsafe or malformed"
            ),
        }
    return {
        "ok": True,
        "promoted": True,
        "decision_id": decision_id,
        "promoted_id": new_id,
        "actor": actor_key,
        "note": str(note or ""),
        "original_retained": True,
        "reason": "",
    }


def list_memories(
    store: Any,
    repo_path: Optional[Any] = None,
    *,
    include_revoked: bool = False,
    limit: int = 500,
) -> Dict[str, Any]:
    """The per-project LISTING surface, with a completeness receipt.

    Returns the rows plus the four things a listing surface must never omit:
    how many rows there were, how many were REVOKED (so a short list is
    explainable), how many have INCOMPLETE provenance (so "this project knows N
    things" does not quietly include N things nobody can trace), and how many
    are UNTRUSTED-sourced.
    """
    rows: List[Any] = []
    error = ""
    try:
        rows = list(store.list_for_repo(repo_path, include_revoked=include_revoked, limit=limit))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
    receipts = [provenance_receipt(row) for row in rows]
    live = [row for row in rows if not bool(_attr(row, "revoked", False))]
    return {
        "repo_path": str(repo_path or ""),
        "rows": [row.as_dict() if hasattr(row, "as_dict") else dict(row) for row in rows],
        "count": len(rows),
        "live": len(live),
        "revoked": len(rows) - len(live),
        "untrusted": sum(1 for item in receipts if item.untrusted_source),
        "provenance_complete": sum(1 for item in receipts if item.complete),
        "provenance_incomplete": sum(1 for item in receipts if not item.complete),
        "trust_tiers": {
            tier: sum(1 for item in receipts if item.trust_tier == tier)
            for tier in TRUST_TIERS
        },
        "error": error,
    }


@dataclass(frozen=True)
class MemoryExport:
    """A total export of one repository's memory, in both formats."""

    format: str
    text: str
    row_count: int
    revoked_count: int
    untrusted_count: int
    incomplete_provenance: int
    repo_path: str
    digest: str
    at: str

    @property
    def empty(self) -> bool:
        """True when there was nothing to export."""
        return self.row_count == 0

    def as_dict(self) -> Dict[str, Any]:
        """Return a receipt (without the payload, which is in ``text``)."""
        return {
            "format": self.format,
            "bytes": len(self.text),
            "row_count": self.row_count,
            "revoked_count": self.revoked_count,
            "untrusted_count": self.untrusted_count,
            "incomplete_provenance": self.incomplete_provenance,
            "repo_path": self.repo_path,
            "digest": self.digest,
            "at": self.at,
            "empty": self.empty,
            "total": True,
        }


def _render_markdown(
    rows: Sequence[Any], receipts: Sequence[ProvenanceReceipt], repo_path: str, at: str
) -> str:
    lines = [
        "# Project memory export",
        "",
        f"- repository: `{repo_path or '(unscoped)'}`",
        f"- exported at: {at}",
        f"- memories: {len(rows)}",
        f"- revoked tombstones: {sum(1 for item in receipts if item.revoked)}",
        f"- untrusted-sourced: {sum(1 for item in receipts if item.untrusted_source)}",
        f"- incomplete provenance: {sum(1 for item in receipts if not item.complete)}",
        "",
        "A revoked memory is listed here because an export you can audit "
        "includes what was withdrawn; it is NOT live and is not injected.",
        "",
    ]
    for row, receipt in zip(rows, receipts, strict=True):
        flags = []
        if receipt.revoked:
            flags.append("REVOKED")
        if receipt.untrusted_source:
            flags.append("UNTRUSTED SOURCE")
        if not receipt.complete:
            flags.append("PROVENANCE INCOMPLETE: missing " + ", ".join(receipt.missing))
        suffix = f"  [{'; '.join(flags)}]" if flags else ""
        lines.append(f"## #{receipt.memory_id} ({_attr(row, 'category') or 'general'})")
        lines.append("")
        lines.append(str(_attr(row, "text") or ""))
        lines.append("")
        lines.append(f"- source: `{receipt.source or '(none)'}`")
        lines.append(f"- recorded: {receipt.created_at or '(none)'}")
        lines.append(f"- trust tier: {receipt.trust_tier}")
        if receipt.revoked:
            lines.append(
                f"- revoked at: {_attr(row, 'revoked_at')} by "
                f"{_attr(row, 'revoked_by') or '(unknown)'} — "
                f"{_attr(row, 'revoked_reason') or 'no reason recorded'}"
            )
        if receipt.provenance:
            rendered = ", ".join(
                f"{key}={value}" for key, value in sorted(receipt.provenance.items())
            )
            lines.append(f"- provenance: {rendered}")
        lines.append("")
        lines.append(suffix or "")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_memories(
    store: Any,
    repo_path: Optional[Any] = None,
    *,
    fmt: str = "json",
    include_revoked: bool = True,
    limit: int = 100_000,
    destination: Optional[Any] = None,
) -> MemoryExport:
    """Export EVERY memory for a repository, in JSON or Markdown.

    Total by contract and by default: revoked tombstones ARE included. A user
    leaving with an export should be able to see what the system believed and
    what it withdrew -- an export that silently drops the tombstones cannot
    answer "did it ever hold anything I need to know about?".

    ``destination`` writes the file atomically and is the only side effect;
    without it the payload is returned in :attr:`MemoryExport.text` and nothing
    is touched. The store is never mutated.
    """
    if fmt not in EXPORT_FORMATS:
        raise ValueError(
            f"unknown export format {fmt!r}; this module writes {EXPORT_FORMATS!r}"
        )
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = list(store.list_for_repo(repo_path, include_revoked=include_revoked, limit=limit))
    receipts = [provenance_receipt(row) for row in rows]

    if fmt == "json":
        payload = {
            "schema": "neo.memory.export/1",
            "repo_path": str(repo_path or ""),
            "exported_at": at,
            "count": len(rows),
            "revoked": sum(1 for item in receipts if item.revoked),
            "untrusted": sum(1 for item in receipts if item.untrusted_source),
            "incomplete_provenance": sum(1 for item in receipts if not item.complete),
            "total": True,
            "note": (
                "revoked memories are included as tombstones: they are NOT live "
                "and are NOT injected into any prompt"
            ),
            "memories": [
                {
                    **(row.as_dict() if hasattr(row, "as_dict") else dict(row)),
                    "provenance_complete": item.complete,
                    "provenance_missing": list(item.missing),
                    "trust_tier": item.trust_tier,
                    "untrusted_source": item.untrusted_source,
                }
                for row, item in zip(rows, receipts, strict=True)
            ],
        }
        text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    else:
        text = _render_markdown(rows, receipts, str(repo_path or ""), at)

    import hashlib

    export = MemoryExport(
        format=fmt,
        text=text,
        row_count=len(rows),
        revoked_count=sum(1 for item in receipts if item.revoked),
        untrusted_count=sum(1 for item in receipts if item.untrusted_source),
        incomplete_provenance=sum(1 for item in receipts if not item.complete),
        repo_path=str(repo_path or ""),
        digest=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        at=at,
    )
    if destination not in (None, ""):
        target = Path(str(destination)).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".partial")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(target)
        except OSError as exc:
            # A failed export is a failed export. Returning an object whose
            # ``text`` is populated while the file was never written is how a
            # user believes they have a backup they do not have.
            raise OSError(
                f"the export could not be written to {target}: {type(exc).__name__}"
            ) from exc
    return export
