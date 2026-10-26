"""The memory inbox: nothing applies silently, and nothing shows without provenance.

T4.W1.1. The contract T5 owns is *"a human-gated review inbox between extraction
and persistence - the only such design in the corpus."* This module is the
review side of it, and it is the ONLY memory renderer in ``cli/`` so the
invariants below cannot be satisfied on one surface and broken on another.

The four things it guarantees, each with the mechanism that enforces it:

1. **A pending proposal is visible with a count AND an age.** A proposal
   sitting for a week is a proposal the user forgot, and the system is
   silently diverging from what they approved. :func:`inbox_status` reports
   both, and the rail reads it.
2. **Nothing is persisted without a human act.** :func:`review_command` has no
   verb that applies memory as a side effect of reading it. ``status``,
   ``show`` and ``diff`` cannot write. Only ``accept``/``reject`` write, and
   both refuse without an explicit ``--confirm``-shaped second token.
3. **Every proposal carries its provenance.** Source session, source TYPE
   (user-stated vs extracted from untrusted content), and confidence. A memory
   mined from an issue body is not the same kind of claim as something the user
   said, and :func:`provenance_lines` renders that difference as a word.
4. **Rejection is permanent.** :func:`rejection_ledger` is the CLI's own
   append-only record of what a human refused, keyed by a content digest so a
   re-extraction of the same text is recognisable. :func:`rejected_digests`
   exists for the extractor to consult; until T5 wires it, the ledger is what
   makes the promise auditable rather than aspirational.

**A rejected memory never silently comes back, and a rejected memory is never
deleted.** The inbox directory is the user's; this module only ever APPENDS to
its ledger and marks the proposal file as decided. That is deliberate: a tool
that deletes a user's memory proposal on their behalf is a tool that has taken
an action they did not ask for.

Everything here is plain text on the way out. Memory text is attacker-influenced
(an issue body, a retrieved file), so every rendered line crosses
``cli.ui.sanitize_text`` exactly once, at the boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "DECIDED_SUFFIX",
    "INBOX_DIR_CANDIDATES",
    "MEMORY_LIST_CAP",
    "MEMORY_VERBS",
    "REJECTED_LEDGER_NAME",
    "SOURCE_TYPES",
    "UNTRUSTED_SOURCE_TYPES",
    "applied_memories",
    "assert_never_applies_without_a_human_act",
    "export_memories",
    "inbox_available",
    "inbox_root",
    "inbox_status",
    "load_proposal",
    "memory_command",
    "memory_detail_lines",
    "memory_diff_lines",
    "memory_export_lines",
    "pending_items",
    "proposal_paths",
    "provenance_lines",
    "provenance_of",
    "ranked_memories",
    "ranked_memory_lines",
    "rejected_digests",
    "rejection_ledger",
    "rejection_report",
    "review_lines",
    "status_lines",
]

#: Where a per-project inbox may live, in resolution order. T5's contract says
#: "a per-project ``.inbox/``"; both spellings are probed because a terminal
#: that picked the other one should not make this surface say "unavailable"
#: while a directory full of proposals sits right there.
INBOX_DIR_CANDIDATES: Tuple[str, ...] = (
    ".inbox",
    ".neo/inbox",
)

#: The CLI's own append-only record of human rejections. Named so it is obvious
#: at a glance in a directory listing that this file is the review ledger and
#: not extracted content.
REJECTED_LEDGER_NAME = "rejected.jsonl"

#: Appended to a proposal file once it has been accepted or rejected. The file
#: is never deleted; a reader can always tell a pending proposal from a decided
#: one by its name alone.
DECIDED_SUFFIX = ".decided"

#: The closed source-type vocabulary. A value outside it renders as
#: ``unrecognised`` rather than being guessed into one of these, because
#: "we did not recognise where this came from" and "this came from the user"
#: are very different things to a reader.
SOURCE_TYPES: Tuple[str, ...] = (
    "user_stated",
    "extracted_session",
    "extracted_untrusted",
    "operator_confirmed",
)

#: The subset that means "this did not come from the user saying it". Every one
#: of these renders with the untrusted marker, and the marker is a WORD in the
#: output, not only a colour.
UNTRUSTED_SOURCE_TYPES = frozenset({"extracted_untrusted"})

#: The closed verb vocabulary. Adding a word is a data edit and both the
#: registry hint and ``neo run`` teach it from here, so a verb a person can
#: type and a verb the dispatcher answers cannot drift.
MEMORY_VERBS: Tuple[str, ...] = (
    "status",
    "list",
    "show",
    "diff",
    "accept",
    "reject",
    "applied",
    "export",
)

#: Verbs that CANNOT write. Asserted by
#: :func:`assert_never_applies_without_a_human_act`, which is the gate rather
#: than a comment: a future round that adds a verb to this table and forgets
#: the gate fails there.
_READ_ONLY_VERBS = frozenset({"status", "list", "show", "diff", "applied", "export"})

#: How many ranked memories ``/memory applied`` prints. A bound that is not
#: stated reads as a complete list, so the remainder is reported as a count.
MEMORY_LIST_CAP = 25

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A human-readable age, deliberately coarse. "3d" is the fact a person
#: acting on a stale proposal needs; "259200 seconds" is not.
def _age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown age"
    value = max(0.0, float(seconds))
    if value < 90:
        return f"{int(value)}s ago"
    if value < 5400:
        return f"{int(value // 60)}m ago"
    if value < 172800:
        return f"{int(value // 3600)}h ago"
    return f"{int(value // 86400)}d ago"


def _sanitize(value: Any, width: int = 0) -> str:
    """One sanitise at the render boundary, exactly once.

    Memory text is untrusted by construction (an issue body, a retrieved file,
    a model reply), and every rendered line passes through here. The sanitiser
    itself FAILS CLOSED, so a raising sanitiser withholds the line rather than
    leaking it - which is why this wrapper has no ``except``.
    """
    try:
        from cli import ui as _ui

        text = str(_ui.sanitize_text(str(value if value is not None else "")))
    except Exception:  # the sanitiser is unavailable: withhold, never pass through
        return "(detail withheld: sanitiser unavailable)"
    text = " ".join(text.split())
    if width and len(text) > max(4, int(width)):
        text = text[: max(1, int(width) - 3)] + "..."
    return text


def _plain(value: Any, width: int = 0) -> str:
    """Bound a product-authored string. NOT a substitute for :func:`_sanitize`.

    Used for words this module wrote (a verb, a reason, a file name) so a
    sanitiser's withholding marker can never appear on a line the reader needs
    to trust for navigation.
    """
    text = " ".join(str(value if value is not None else "").split())
    if width and len(text) > max(4, int(width)):
        text = text[: max(1, int(width) - 3)] + "..."
    return text


def _now() -> float:
    return time.time()


def digest_of(text: Any) -> str:
    """The stable identity of a memory's CONTENT, for rejection matching.

    Normalised so that the differences between two extractions of the same fact
    are recognisably noise rather than a new memory: case, whitespace, and
    TRAILING SENTENCE PUNCTUATION. The punctuation rule is the one this round
    added, and it is the one that decides whether the promise holds - an
    extractor that returns ``"the project uses pytest"`` on one run and
    ``"the project uses pytest."`` on the next produces two digests, so a
    refusal recorded against the first is invisible to the second and the
    rejected memory re-proposes itself with a full stop added.

    ``rejected_digests`` keys on this value, so a ledger written before the
    punctuation rule will not match a digest computed after it. That is stated
    rather than papered over: the ledger row carries ``version``, and a
    mismatch degrades to "this refusal is not recognised", which is the safe
    direction - a stale ledger can re-propose, a wrong one cannot silently
    refuse something the user never refused.
    """
    normalized = " ".join(str(text if text is not None else "").split()).lower()
    normalized = normalized.strip(" \t\r\n.,;:!?-")
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Locating the inbox. Tolerant by design, honest when it finds nothing.
# ---------------------------------------------------------------------------


def inbox_root(repo: Any = None, log_root: Any = None) -> Optional[Path]:
    """The per-project inbox directory, or ``None`` when there is not one.

    Resolution order: ``<repo>/.inbox``, ``<repo>/.neo/inbox``, then
    ``<log_root>/_memory-inbox``. The log-root fallback exists because a
    harness-owned artifact root is outside the user's repository by design
    (VEX-CEILING-03), so an inbox written there would be invisible to a
    repository-relative read.

    ``None`` is the honest answer when no inbox exists. It is NOT an empty
    inbox: "there is no inbox here" and "the inbox is empty" are different
    facts and the status surface renders them differently.
    """
    roots: List[Path] = []
    if repo:
        base = Path(str(repo))
        for candidate in INBOX_DIR_CANDIDATES:
            roots.append(base / candidate)
    if log_root:
        roots.append(Path(str(log_root)) / "_memory-inbox")
    for candidate in roots:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def inbox_available(repo: Any = None, log_root: Any = None) -> Dict[str, Any]:
    """Whether an inbox exists here, and if not, why not."""
    root = inbox_root(repo, log_root)
    return {
        "available": root is not None,
        "root": str(root) if root is not None else "",
        "reason": "" if root is not None else (
            "no .inbox/ directory exists for this repository; memory extraction "
            "has either never run here or writes somewhere this surface does "
            "not know about"
        ),
        "searched": [
            str(Path(str(repo)) / candidate) if repo else ""
            for candidate in INBOX_DIR_CANDIDATES
        ]
        + ([str(Path(str(log_root)) / "_memory-inbox")] if log_root else []),
    }


def proposal_paths(root: Any) -> List[Path]:
    """Every PENDING proposal file under an inbox root.

    A file whose name ends with :data:`DECIDED_SUFFIX` is excluded, and so is
    the rejection ledger. Sorting is by name so the review order is stable
    between two reads of the same inbox - a review list that reorders itself
    is a review list nobody can work through.
    """
    try:
        base = Path(str(root))
    except (TypeError, ValueError):
        return []
    if not base.is_dir():
        return []
    out: List[Path] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda item: item.name):
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith(".") or name == REJECTED_LEDGER_NAME:
                continue
            if name.endswith(DECIDED_SUFFIX):
                continue
            out.append(entry)
    except OSError:
        return []
    return out


def load_proposal(path: Any) -> Dict[str, Any]:
    """Read one proposal file into a record, whatever shape it is in.

    Accepts a canonical patch file (JSON, JSON-lines, or a plain text/markdown
    memory body) because T5's exact serialisation is landing in the same wave
    this surface is built in, and a renderer that only understood one of them
    would report "no proposals" over a full inbox.

    A file that cannot be parsed is returned as a record with ``readable:
    False`` and the parse error as its text - never dropped, because a proposal
    nobody can read is still a proposal a human has to decide about.
    """
    file = Path(str(path)) if path else None
    record: Dict[str, Any] = {
        "id": file.stem if file is not None else "",
        "path": str(file) if file is not None else "",
        "readable": False,
        "text": "",
        "payload": {},
        "reason": "",
        "mtime": None,
    }
    if file is None:
        record["reason"] = "no proposal file"
        return record
    try:
        raw = file.read_text(encoding="utf-8", errors="replace")
        record["mtime"] = file.stat().st_mtime
    except OSError as exc:
        record["reason"] = f"proposal could not be read: {exc}"
        return record

    payload = _parse_proposal(raw)
    if isinstance(payload, Mapping):
        record["readable"] = True
        record["payload"] = dict(payload)
        record["text"] = str(
            payload.get("text")
            or payload.get("memory")
            or payload.get("content")
            or payload.get("body")
            or ""
        )
        record["id"] = str(payload.get("id") or record["id"])
    else:
        # A plain body is a legitimate canonical form: the whole file IS the
        # memory. Treating that as unreadable would hide a proposal.
        record["readable"] = True
        record["payload"] = {}
        record["text"] = str(payload or raw)
    if not record["text"].strip():
        record["reason"] = "the proposal carries no memory text"
    return record


def _parse_proposal(raw: str) -> Any:
    """Parse a proposal body: JSON object, JSON array, JSON-lines, or text."""
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass
    if text.startswith("["):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list) and loaded:
                first = loaded[0]
                if isinstance(first, Mapping):
                    return dict(first)
        except ValueError:
            pass
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1 and all(line.strip().startswith("{") for line in lines):
        records = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                records = []
                break
            if isinstance(item, Mapping):
                records.append(dict(item))
        if records:
            return records[0]
    return text


# ---------------------------------------------------------------------------
# Provenance. The field that makes an untrusted memory visibly different.
# ---------------------------------------------------------------------------


def provenance_of(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The provenance of one proposal, from wherever the producer recorded it.

    Six keys, each with its OWN absent value, because provenance is the thing
    that tells a reader whether to trust a claim:

    ==================  ==================================================
    key                 absent rule
    ==================  ==================================================
    ``source_type``     ``unrecognised`` - never defaulted to
                        ``user_stated``. A proposal whose origin the
                        producer did not record is NOT a user statement.
    ``source_session``  ``""`` plus ``origin_recorded: False``
    ``confidence``      ``None`` (rendered ``unavailable``), never 0.0
    ``extracted_from``  ``""`` - the untrusted locator, when there is one
    ``created_at``      ``None``, never the file mtime as if it were a fact
    ``digest``          always computed from the text
    ==================  ==================================================

    ``origin_recorded`` is the load-bearing one: a proposal with no recorded
    provenance renders under an UNTRUSTED marker even when its source type is
    unknown, because silence from a producer is not consent.
    """
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    source = payload if isinstance(payload, Mapping) else {}
    provenance = source.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}

    def pick(*keys: str) -> Any:
        for key in keys:
            for holder in (provenance, source):
                if key in holder and holder[key] is not None:
                    return holder[key]
        return None

    raw_type = str(pick("source_type", "origin", "kind", "source") or "").strip().lower()
    known = raw_type in SOURCE_TYPES
    source_type = raw_type if known else "unrecognised"
    if not raw_type:
        source_type = "unrecognised"

    session = str(pick("source_session", "session_id", "session") or "").strip()
    extracted = str(
        pick("extracted_from", "source_ref", "source_path", "locator") or ""
    ).strip()

    confidence = pick("confidence", "score", "confidence_score")
    try:
        confidence_value = (
            None if confidence is None else float(confidence)
        )
    except (TypeError, ValueError):
        confidence_value = None

    created = pick("created_at", "proposed_at", "ts", "timestamp")
    try:
        created_value = None if created is None else float(created)
    except (TypeError, ValueError):
        created_value = None

    origin_recorded = bool(raw_type or session or extracted)
    return {
        "id": str(record.get("id") or ""),
        "source_type": source_type,
        "source_type_declared": raw_type,
        "source_type_recognised": known,
        "source_session": session,
        "origin_recorded": origin_recorded,
        "extracted_from": extracted,
        "confidence": confidence_value,
        "confidence_recorded": confidence_value is not None,
        "created_at": created_value,
        "created_at_recorded": created_value is not None,
        "digest": digest_of(record.get("text") or ""),
        "category": str(pick("category") or ""),
        # An UNRECOGNISED type is untrusted, and that was a real defect: the
        # first version computed `untrusted` from the COERCED type, so a
        # producer that wrote `source_type: "something_new"` produced
        # `unrecognised` in the row and `untrusted: False` beside it - a memory
        # this build cannot explain, rendered as though it were safe. "We do
        # not know what produced this" and "this is safe" are different claims,
        # and only the first one is true.
        "untrusted": (
            (source_type in UNTRUSTED_SOURCE_TYPES)
            or (not known)
            or (not origin_recorded)
        ),
    }


def provenance_lines(provenance: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render provenance, with the untrusted distinction as a WORD.

    The distinction is a word rather than a colour because this line renders
    into a markup sink on two shells and into plain text on a third, and a
    colour is the one channel that does not survive all three.
    """
    provenance = provenance if isinstance(provenance, Mapping) else {}
    lines = [
        "provenance  source type: {}".format(
            _plain(provenance.get("source_type") or "unrecognised")
        )
    ]
    if not provenance.get("source_type_recognised"):
        lines.append(
            "provenance  the producer recorded no source type this surface "
            "recognises; treat it as UNTRUSTED"
        )
    lines.append(
        "provenance  source session: {}".format(
            _plain(provenance.get("source_session") or "not recorded")
        )
    )
    confidence = provenance.get("confidence")
    lines.append(
        "provenance  confidence: {}".format(
            "not recorded (unavailable)"
            if confidence is None
            else f"{float(confidence):.2f}"
        )
    )
    extracted = str(provenance.get("extracted_from") or "")
    if extracted:
        lines.append(f"provenance  extracted from: {_sanitize(extracted, width)}")
    if provenance.get("untrusted"):
        lines.append(
            "provenance  UNTRUSTED SOURCE - this was extracted from content, "
            "not stated by you"
        )
    return lines


# ---------------------------------------------------------------------------
# The rejection ledger. Permanent, append-only, and the user's to keep.
# ---------------------------------------------------------------------------


def rejection_ledger(root: Any) -> Path:
    """The path of the CLI's rejection ledger inside an inbox."""
    base = Path(str(root)) if root else Path(".")
    return base / REJECTED_LEDGER_NAME


def rejected_digests(root: Any) -> Dict[str, Dict[str, Any]]:
    """Every rejection recorded in an inbox, keyed by content digest.

    This is the function T5's extractor consults so a refused memory does not
    re-propose itself. It is deliberately the SIMPLEST shape that works -
    ``digest -> {reason, at, id, text_digest_only}`` - so an extractor can
    honour it without importing a rendering module.
    """
    path = rejection_ledger(root)
    out: Dict[str, Dict[str, Any]] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("digest") or "")
        if not key:
            continue
        out[key] = {
            "digest": key,
            "id": str(row.get("id") or ""),
            "reason": str(row.get("reason") or ""),
            "at": row.get("at"),
            "text_digest": str(row.get("text_digest") or ""),
            "source_type": str(row.get("source_type") or ""),
        }
    return out


def _append_rejection(root: Any, record: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    """Append one rejection row. The ONLY write this module performs."""
    provenance = provenance_of(record)
    row = {
        "digest": provenance["digest"],
        "id": provenance["id"],
        "reason": _plain(reason),
        "at": _now(),
        "source_type": provenance["source_type"],
        "text_digest": digest_of(record.get("text") or ""),
        "version": 1,
    }
    path = rejection_ledger(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        return {"recorded": False, "reason": f"the rejection could not be recorded: {exc}"}
    return {"recorded": True, "reason": "", "row": row}


def _mark_decided(root: Any, proposal_id: str) -> Dict[str, Any]:
    """Mark a proposal file decided by appending the suffix, never deleting it."""
    if not _SAFE_ID.match(str(proposal_id or "")):
        return {"marked": False, "reason": "the proposal id is not a safe filename"}
    for candidate in proposal_paths(root):
        if candidate.stem != str(proposal_id):
            continue
        target = candidate.with_name(candidate.name + DECIDED_SUFFIX)
        try:
            if target.exists():
                return {"marked": True, "reason": ""}
            candidate.rename(target)
        except OSError as exc:
            return {"marked": False, "reason": f"could not mark it decided: {exc}"}
        return {"marked": True, "reason": ""}
    return {"marked": False, "reason": "no pending proposal with that id"}


def rejection_report(root: Any) -> Dict[str, Any]:
    """What has been refused, for display and for an audit."""
    rows = rejected_digests(root)
    return {
        "count": len(rows),
        "path": str(rejection_ledger(root)),
        "entries": sorted(
            rows.values(), key=lambda item: str(item.get("at") or 0), reverse=True
        ),
    }


# ---------------------------------------------------------------------------
# The status surface. Count AND age, which is what the rail reads.
# ---------------------------------------------------------------------------


def pending_items(
    repo: Any = None,
    log_root: Any = None,
    *,
    root: Any = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Every pending proposal, with its provenance, oldest first.

    Oldest first is deliberate. A review queue ordered by age puts the
    proposal the user has been ignoring longest at the top, which is the one
    most likely to be silently shaping behaviour.
    """
    base = Path(str(root)) if root is not None else inbox_root(repo, log_root)
    if base is None:
        return []
    stamp = float(now) if now is not None else _now()
    rejected = rejected_digests(base)
    items: List[Dict[str, Any]] = []
    for path in proposal_paths(base):
        record = load_proposal(path)
        provenance = provenance_of(record)
        created = provenance.get("created_at") or record.get("mtime")
        age = (stamp - float(created)) if isinstance(created, (int, float)) else None
        item = dict(record)
        item["provenance"] = provenance
        item["age_seconds"] = age
        item["age"] = _age(age)
        prior = rejected.get(provenance["digest"])
        item["previously_rejected"] = prior is not None
        # W2.1 item 5: a rejection is DURABLE at the UI layer, which is the only
        # layer this module owns. `rejected_digests` already existed and was
        # only ever REPORTED - `pending_items` set a flag and moved on - so a
        # re-extraction of the same text produced a fresh pending proposal that
        # the surface cheerfully offered for acceptance again. The promise in
        # Wave 1's docstring ("a rejected memory never silently comes back")
        # was therefore not kept by the code.
        #
        # A suppressed row stays VISIBLE with its refusal attached rather than
        # being hidden: a proposal the user cannot see is indistinguishable from
        # one that was never made, and "nothing was proposed" is a different
        # fact from "what was proposed was refused and here is the refusal".
        item["suppressed"] = prior is not None
        item["suppression_reason"] = str((prior or {}).get("reason") or "")
        item["suppressed_at"] = (prior or {}).get("at")
        items.append(item)
    items.sort(key=lambda item: -(float(item.get("age_seconds") or 0.0)))
    return items


def inbox_status(
    repo: Any = None,
    log_root: Any = None,
    *,
    root: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The pending-inbox status the rail and ``/memory status`` both read.

    Every field carries its own absent rule. ``count`` is ``None`` - not zero -
    when there is no inbox, because "no inbox exists" and "the inbox is empty"
    are different facts and a rail that renders both as ``0 pending`` is
    telling a user their memory is under control when nothing has said so.
    """
    available = inbox_available(repo, log_root)
    base = Path(str(root)) if root is not None else inbox_root(repo, log_root)
    if base is None:
        return {
            "available": False,
            "root": "",
            "count": None,
            "age": "",
            "age_seconds": None,
            "oldest_id": "",
            "untrusted": None,
            "unrecognised": None,
            "previously_rejected": 0,
            "reason": available["reason"],
            "searched": available["searched"],
        }

    items = pending_items(root=base, now=now)
    stamp = float(now) if now is not None else _now()
    ages = [
        item["age_seconds"]
        for item in items
        if isinstance(item.get("age_seconds"), (int, float))
    ]
    oldest = max(ages) if ages else None
    untrusted = sum(
        1 for item in items if (item.get("provenance") or {}).get("untrusted")
    )
    unrecognised = sum(
        1
        for item in items
        if (item.get("provenance") or {}).get("source_type") == "unrecognised"
    )
    return {
        "available": True,
        "root": str(base),
        "count": len(items),
        "age": _age(oldest),
        "age_seconds": oldest,
        "oldest_id": str(items[0].get("id") or "") if items else "",
        "untrusted": untrusted,
        "unrecognised": unrecognised,
        "previously_rejected": sum(
            1 for item in items if item.get("previously_rejected")
        ),
        "reason": "",
        "searched": available["searched"],
        "as_of": stamp,
    }


def status_lines(status: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """Render the status, count AND age, or say honestly that there is none."""
    status = status if isinstance(status, Mapping) else {}
    lines: List[str] = []
    if not status.get("available"):
        lines = ["memory inbox  UNAVAILABLE - there is no inbox for this repository"]
        # The reason goes on its OWN line and is NOT truncated. Wave 1 put it on
        # the headline and bounded it to the terminal width, which cut the
        # sentence at exactly the load-bearing half: "memory extraction has
        # either..." with the "never run" clause - the one fact that separates
        # "extraction has not happened" from "extraction writes somewhere this
        # surface does not know about" - clipped off the end. A disclosure that
        # survives only in the source is a disclosure nobody receives, and this
        # file's own §2 records that lesson from a different surface.
        reason = str(status.get("reason") or "no inbox directory was found")
        for row in reason.split(". "):
            text = row.strip().rstrip(".")
            if text:
                lines.append("  " + text + ".")
        searched = [str(item) for item in (status.get("searched") or []) if item]
        if searched:
            lines.append("  looked in: " + ", ".join(searched[:4]))
        lines.append(
            "  a proposal that is not shown here is a behaviour change nobody "
            "applied deliberately"
        )
        lines.append(
            "  NOTE: an empty inbox is a DIFFERENT fact from no inbox - this is "
            "the second"
        )
        return lines

    count = status.get("count")
    if count is None:
        count = 0
    headline = f"memory inbox  {int(count)} proposal(s) pending"
    if int(count) > 0 and status.get("age"):
        headline += f" · oldest {status['age']}"
    lines.append(headline)
    untrusted = status.get("untrusted")
    unrecognised = status.get("unrecognised")
    if untrusted or unrecognised:
        lines.append(
            "memory inbox  {u} of {n} came from untrusted or unrecorded "
            "sources - review those with the most care".format(
                u=int(untrusted or 0) + int(unrecognised or 0), n=int(count)
            )
        )
    if status.get("previously_rejected"):
        lines.append(
            "memory inbox  {n} proposal(s) were refused before and have been "
            "proposed again - the text is unchanged".format(
                n=int(status["previously_rejected"])
            )
        )
    lines.append("memory inbox  /memory  to review · /memory export to take them all")
    return [_plain(line, width + 8) for line in lines]


# ---------------------------------------------------------------------------
# The applied set: inspectable and exportable at all times.
# ---------------------------------------------------------------------------


def _open_store() -> Any:
    """The decision store, or ``None``.

    Imported lazily and defensively: this surface must render an honest
    "unavailable" when the memory layer cannot load, and a renderer that
    raised would take a session down over a read.
    """
    try:
        from memory.decision_store import open_default_store

        return open_default_store()
    except Exception:
        return None


def applied_memories(
    repo: Any = None, *, store: Any = None, limit: int = 200
) -> Dict[str, Any]:
    """Every APPLIED memory for a repository, with its provenance AND its trust.

    W2.1 change, and it is a re-point rather than an addition. Wave 1 read the
    store through ``handle.search`` and projected six fields out of it, which
    meant the row it rendered carried no trust field at all: an untrusted
    extraction and something the user typed came back as the same dict. The
    read now goes through ``memory.governance.list_memories`` - the authority
    that already reduces a row to a trust tier - and every row carries the
    marker a reader needs before they read the text.

    ``store`` is accepted and ignored by the governance path, which opens its
    own store. It is kept because callers pass it and because a caller that
    wants a specific store should say so; the honest answer is that the
    governance reader owns the open, so a passed store cannot narrow it.

    A store that cannot be opened reports ``available: False`` with a reason;
    it never reports an empty set, because "the memory store is unreachable"
    and "this project has no memories" are different facts.
    """
    if store is not None:
        # An injected store was accepted by Wave 1 and read directly. Reading
        # it directly again would reinstate the second reader this round
        # removes, so the injected handle is honoured only as an availability
        # probe and the projection comes from the authority.
        try:
            store.search("", limit=1)
        except Exception as exc:
            return {
                "available": False,
                "count": None,
                "rows": [],
                "reason": f"the memory store could not be read: {exc}",
                "repo": str(repo) if repo else "",
            }
    try:
        from memory import governance as _governance
    except Exception as exc:
        return {
            "available": False,
            "count": None,
            "rows": [],
            "reason": f"the memory layer could not be loaded: {exc}",
            "repo": str(repo) if repo else "",
        }
    handle = _open_store()
    if handle is None:
        return {
            "available": False,
            "count": None,
            "rows": [],
            "reason": "the memory store could not be opened",
            "repo": str(repo) if repo else "",
        }
    try:
        report = _governance.list_memories(
            handle, repo_path=str(repo) if repo else None, limit=int(limit)
        )
    except Exception as exc:
        return {
            "available": False,
            "count": None,
            "rows": [],
            "reason": f"the memory store could not be read: {exc}",
            "repo": str(repo) if repo else "",
        }
    if not isinstance(report, Mapping):
        return {
            "available": False,
            "count": None,
            "rows": [],
            "reason": "the memory authority returned something that is not a report",
            "repo": str(repo) if repo else "",
        }

    from cli import memory_provenance as _prov

    raw = report.get("rows")
    raw = raw if isinstance(raw, list) else []
    rows: List[Dict[str, Any]] = []
    for item in raw:
        as_dict = getattr(item, "as_dict", None)
        try:
            payload = as_dict() if callable(as_dict) else dict(item)
        except (TypeError, ValueError):
            continue
        trust = _prov.trust_class_of(payload)
        rows.append(
            {
                "id": payload.get("id"),
                "text": str(payload.get("text") or ""),
                "category": str(payload.get("category") or ""),
                "source": str(payload.get("source") or ""),
                "task_id": str(payload.get("task_id") or ""),
                "created_at": payload.get("created_at"),
                "provenance": payload.get("provenance"),
                "provenance_kind": str(payload.get("provenance_kind") or ""),
                "durable": payload.get("durable"),
                "revoked": bool(payload.get("revoked")),
                "digest": digest_of(payload.get("text") or ""),
                # W2.1: the four facts, the trust class, and the marker. The
                # marker is what a reader sees first, and an untrusted memory
                # that renders with the same shape as a stated one is the
                # defect item 2 of this round names.
                "trust": trust,
                "marker": trust["marker"],
                "untrusted": bool(trust["untrusted"]),
                "facts": _prov.memory_facts(payload),
            }
        )
    # The authority's own aggregates are carried through rather than recomputed:
    # a second count of the same rows is a second answer to "how many memories
    # are untrusted", and this module already has one recorded instance of
    # exactly that (see `cli/context_budget.py`'s applied count).
    return {
        "available": True,
        "count": len(rows),
        "rows": rows,
        "reason": "",
        "repo": str(repo) if repo else "",
        "live": report.get("live"),
        "revoked": report.get("revoked"),
        "untrusted": report.get("untrusted"),
        "trust_tiers": report.get("trust_tiers"),
        "provenance_complete": report.get("provenance_complete"),
        "provenance_incomplete": report.get("provenance_incomplete"),
        "authority_error": report.get("error") or "",
    }


def ranked_memories(
    repo: Any = None,
    *,
    store: Any = None,
    limit: int = 50,
    log_root: Any = None,
) -> Dict[str, Any]:
    """The applied memories RANKED, with every score's explanation attached.

    W2.1 item 4. ``memory.ranking.rank_memories`` already computes
    citation-count plus usage decay and already writes a sentence per scored
    term; Wave 1 never called it, so ``/memory applied`` listed memories in
    store order and a reader had no way to learn why one was above another -
    or that the ordering was not a ranking at all.

    The receipt is returned whole, including ``usage_applied``: when it is
    ``False`` the ordering in play is lexical plus recency, and saying "top
    memory" over that set without the caveat would be claiming a usage-based
    ranking that did not happen.
    """
    applied = applied_memories(repo, store=store, limit=max(int(limit), 1) * 4)
    if not applied["available"]:
        return {
            "available": False,
            "rows": [],
            "count": None,
            "receipt": None,
            "reason": applied["reason"],
        }
    ranking = None
    try:
        from memory import ranking as _ranking

        ranking = _ranking
    except Exception as exc:
        return {
            "available": False,
            "rows": applied["rows"],
            "count": applied["count"],
            "receipt": None,
            "reason": f"the ranking module could not be loaded: {exc}",
        }
    usage = None
    try:
        usage = ranking.open_default_usage_index()
    except Exception:
        usage = None
    try:
        receipt = ranking.rank_memories(
            [{"id": row["id"], "text": row["text"], "source": row["source"]} for row in applied["rows"]],
            usage=usage,
            limit=int(limit),
        )
    except Exception as exc:
        return {
            "available": False,
            "rows": applied["rows"],
            "count": applied["count"],
            "receipt": None,
            "reason": f"the memories could not be ranked: {exc}",
        }

    from cli import memory_provenance as _prov

    ordered: List[Dict[str, Any]] = []
    for candidate in getattr(receipt, "ranked", ()) or ():
        source_row = next(
            (row for row in applied["rows"] if row["id"] == candidate.memory_id), None
        )
        if source_row is None:
            continue
        merged = dict(source_row)
        merged["ranking"] = _prov.ranking_why(candidate.memory_id, ranked=receipt)
        ordered.append(merged)
    for row in applied["rows"]:
        if not any(item["id"] == row["id"] for item in ordered):
            merged = dict(row)
            merged["ranking"] = _prov.ranking_why(row["id"], ranked=receipt)
            ordered.append(merged)
    return {
        "available": True,
        "rows": ordered,
        "count": len(ordered),
        "receipt": receipt,
        "usage_applied": bool(getattr(receipt, "usage_applied", False)),
        "usage_reason": str(getattr(receipt, "usage_reason", "") or ""),
        "dropped_below_floor": int(getattr(receipt, "dropped_below_floor", 0) or 0),
        "considered": int(getattr(receipt, "considered", 0) or 0),
        "reason": "",
        "repo": str(repo) if repo else "",
    }


def memory_detail_lines(
    row: Mapping[str, Any], *, log_root: Any = None, width: int = 78
) -> List[str]:
    """One APPLIED memory, with resolved provenance and its ranking explained.

    This is the memory-detail surface. It delegates to
    ``cli.memory_provenance`` rather than rendering provenance itself, because a
    second provenance renderer is a second answer to "where did this come from"
    and the two would drift on the first producer change.
    """
    from cli import memory_provenance as _prov

    report = _prov.provenance_report(row, memory_id=row.get("id"), log_root=log_root)
    # W2.1 item 4: reuse the ranking the LIST already computed rather than
    # re-ranking one row in isolation. A single-row ranking normalises every
    # component against itself, so its scores and reasons would not be the ones
    # the reader saw in the list - and "why is this #1" answered with a
    # different set of numbers than "why is this #1" showed two lines earlier is
    # a second answer to one question.
    if isinstance(row.get("ranking"), Mapping):
        report["ranking"] = dict(row["ranking"])
    lines = _prov.provenance_report_lines(report, width=width)
    ok, why = _prov.assert_no_dangling_citation_renders_as_verifiable(report)
    if not ok:
        # A renderer that discovers it produced a dead link says so rather than
        # letting it stand: the receipt contract in this repo treats a
        # self-detected lie as a defect worth surfacing, not a detail to hide.
        lines.append("  PROVENANCE CHECK FAILED: " + _plain(why, max(20, width - 28)))
    return lines


def ranked_memory_lines(
    ranked: Mapping[str, Any], *, log_root: Any = None, width: int = 78
) -> List[str]:
    """The ranked memory list, each row led by its trust MARKER.

    The order is the ranking's, and the marker is first on every row: a reader
    scanning for "which of these did I not say" must be able to find it without
    reading a single word of prose.
    """
    ranked = ranked if isinstance(ranked, Mapping) else {}
    if not ranked.get("available"):
        return [
            "memory  unavailable: " + _plain(ranked.get("reason") or "", max(20, width - 22))
        ]
    rows = list(ranked.get("rows") or ())
    lines = [
        "memory  {n} applied, ranked ({considered} considered)".format(
            n=len(rows), considered=int(ranked.get("considered") or len(rows))
        )
    ]
    # The caveat renders BEFORE the empty check. The first version returned
    # early on an empty set, so a repository with no memories showed "no memory
    # has been applied" and nothing else - which silently drops the one fact
    # that stays true either way: the ordering in play is not a usage ranking.
    if not ranked.get("usage_applied", False):
        # UNBOUNDED, on its own line. The caveat's load-bearing clause is its
        # last one - "no memory in this set has ever been cited ... this is not
        # a usage-based ranking" - and bounding it to the rail width cut it at
        # exactly the word that makes the statement true. That is the third
        # time this round a width bound ate a disclosure, which is why
        # `cli/AGENTS.md` records the pattern rather than the individual bug.
        lines.append("  NOT a usage ranking:")
        for row in str(
            ranked.get("usage_reason") or "no usage telemetry was available"
        ).split(". "):
            text = row.strip().rstrip(".")
            if text:
                lines.append("    " + text + ".")
    dropped = int(ranked.get("dropped_below_floor") or 0)
    if dropped:
        lines.append(f"  {dropped} row(s) scored below the floor and were dropped")
    if not rows:
        lines.append("memory  no memory has been applied to this repository")
        return lines
    for row in rows[:MEMORY_LIST_CAP]:
        why = row.get("ranking") if isinstance(row.get("ranking"), Mapping) else {}
        rank = why.get("rank")
        score = why.get("score")
        prefix = f"  {row.get('marker') or '?'} #{row.get('id')}"
        if rank is not None:
            prefix += f" rank {rank}"
        if score is not None:
            try:
                prefix += f" · score {float(score):.4f}"
            except (TypeError, ValueError):
                pass
        citations = why.get("citations")
        if citations is None:
            prefix += " · recalled UNKNOWN (no usage row)"
        else:
            prefix += f" · recalled {int(citations)}x"
        lines.append(prefix)
        body = str(row.get("text") or "").splitlines()
        lines.append("      " + _sanitize(body[0] if body else "(no text)", max(20, width - 8)))
        if str((row.get("trust") or {}).get("class") or "") in (
            "untrusted",
            "unattributed",
        ):
            lines.append(
                "      "
                + _plain(
                    "from an untrusted or unattributed source - "
                    + str((row.get("trust") or {}).get("reason") or "no reason recorded"),
                    max(20, width - 8),
                )
            )
    if len(rows) > MEMORY_LIST_CAP:
        # A bound that is not STATED reads as a complete list. This repo has
        # recorded that failure twice (`cli/runview.py`'s capped changed-file
        # set and the memory module's own file listing), so the omission is
        # reported as a count rather than absorbed.
        lines.append(
            "  (+{n} more - /memory export takes all of them)".format(
                n=len(rows) - MEMORY_LIST_CAP
            )
        )
    untrusted = sum(1 for row in rows if row.get("untrusted"))
    if untrusted:
        lines.append(
            "  {n} of {total} came from an untrusted or unattributed source".format(
                n=untrusted, total=len(rows)
            )
        )
    lines.append("  why a memory ranks where it does: /memory show <id>")
    return lines


def export_memories(repo: Any = None, *, store: Any = None) -> Dict[str, Any]:
    """The full export document: applied memories AND the rejection ledger.

    A memory you cannot export is memory you do not own - so the export
    includes the REJECTIONS too. A user leaving needs to know what the agent
    was told not to remember as much as what it was told to remember.
    """
    applied = applied_memories(repo, store=store)
    rejections = rejection_report(inbox_root(repo))
    return {
        "schema_version": 1,
        "repo": str(repo) if repo else "",
        "applied": applied["rows"],
        "applied_count": applied["count"],
        "applied_available": applied["available"],
        "rejected": rejections["entries"],
        "rejected_count": rejections["count"],
        "notes": [
            "proposed (unapplied) memories are in the .inbox/ directory, not here",
            "nothing in this document was written by extraction: every row is "
            "either applied by a human or refused by one",
        ],
    }


def memory_export_lines(
    document: Mapping[str, Any], *, fmt: str = "markdown", width: int = 78
) -> List[str]:
    """Render an export as markdown or json. Both are complete documents."""
    document = document if isinstance(document, Mapping) else {}
    if str(fmt).lower() == "json":
        return json.dumps(document, indent=2, sort_keys=True, default=str).splitlines()
    lines = [
        "# Neo memory",
        "",
        f"- repository: {_plain(document.get('repo') or '(unset)')}",
        f"- applied: {document.get('applied_count')}",
        f"- rejected: {document.get('rejected_count')}",
        "",
    ]
    for note in document.get("notes") or ():
        lines.append(f"> {_plain(note)}")
        lines.append("")
    lines.append("## Applied")
    lines.append("")
    rows = document.get("applied") or []
    if not rows:
        lines.append("_none recorded_")
    for row in rows:
        provenance = row.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        lines.append(
            "- {text}\n  - category: {category}\n  - source: {source}\n"
            "  - provenance: {kind}\n  - digest: {digest}".format(
                text=_sanitize(row.get("text"), width),
                category=_plain(row.get("category") or "(none)"),
                source=_plain(row.get("source") or "(none)"),
                kind=_plain(
                    provenance.get("source")
                    or provenance.get("source_type")
                    or "not recorded"
                ),
                digest=_plain(row.get("digest") or ""),
            )
        )
    lines.extend(["", "## Rejected", ""])
    rejected = document.get("rejected") or []
    if not rejected:
        lines.append("_none recorded_")
    for row in rejected:
        lines.append(
            "- digest `{digest}` - {reason}".format(
                digest=_plain(row.get("digest") or ""),
                reason=_plain(row.get("reason") or "no reason recorded"),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# The per-item review. Full diff, per-item accept/reject, provenance.
# ---------------------------------------------------------------------------


def memory_diff_lines(record: Mapping[str, Any], *, width: int = 78) -> List[str]:
    """The FULL diff of what applying this proposal would do.

    "Full" means: the memory text as it would be written, the category, and the
    provenance, and nothing omitted. An all-or-nothing review is not a review,
    and a truncated diff is a diff that hides the part the user needed.

    There is no applied-set comparison here because a PROPOSAL has no
    "before" - it is new text. What a reviewer needs is the whole text plus
    its origin, and that is what this renders.
    """
    record = record if isinstance(record, Mapping) else {}
    provenance = (
        record.get("provenance")
        if isinstance(record.get("provenance"), Mapping)
        else provenance_of(record)
    )
    text = str(record.get("text") or "")
    lines = [
        f"proposal    {provenance.get('id') or record.get('id') or '(unnamed)'}",
        f"  would add {len(text)} char(s) as a memory, verbatim:",
    ]
    if text.strip():
        for row in text.splitlines() or [""]:
            lines.append("    + " + _sanitize(row, max(20, width - 6)))
    else:
        lines.append("    + (the proposal carries no text)")
    lines.append(f"  category:  {_plain(provenance.get('category') or '(none)')}")
    lines.append(f"  digest:    {_plain(provenance.get('digest') or '')}")
    lines.extend(provenance_lines(provenance, width=width))
    return lines


def review_lines(
    item: Mapping[str, Any], *, width: int = 78, diff: bool = True
) -> List[str]:
    """Render one proposal for review: identity, age, and the full diff."""
    item = item if isinstance(item, Mapping) else {}
    provenance = (
        item.get("provenance")
        if isinstance(item.get("provenance"), Mapping)
        else provenance_of(item)
    )
    header = "proposal {id} · {age}".format(
        id=provenance.get("id") or item.get("id") or "(unnamed)",
        age=_plain(item.get("age") or "unknown age"),
    )
    lines = [header]
    if item.get("suppressed"):
        # W2.1 item 5, on the surface a reader actually reaches.
        lines.append(
            "  YOU REFUSED THIS and it stays refused - the content digest is "
            "unchanged, so accepting it again is refused too"
        )
        if item.get("suppression_reason"):
            lines.append("  your reason was: " + _plain(item["suppression_reason"], 60))
        if item.get("suppressed_at"):
            lines.append(
                "  refused at: " + _plain(item["suppressed_at"], 40)
            )
    elif item.get("previously_rejected"):
        lines.append(
            "  you refused this exact text before and it has been proposed "
            "again - the content is unchanged"
        )
    if diff:
        lines.extend(memory_diff_lines({**item, "provenance": provenance}, width=width))
    if item.get("suppressed"):
        lines.append("  this proposal cannot be accepted; it is already refused")
        return lines
    lines.append(
        "  accept with:  /memory accept {id} --confirm".format(
            id=provenance.get("id") or item.get("id") or ""
        )
    )
    lines.append(
        "  refuse with:  /memory reject {id} --confirm --reason \"...\"".format(
            id=provenance.get("id") or item.get("id") or ""
        )
    )
    return lines


# ---------------------------------------------------------------------------
# The dispatcher. The ONE implementation, three surfaces.
# ---------------------------------------------------------------------------


def _fail(verb: str, message: str) -> Dict[str, Any]:
    return {"verb": verb, "ok": False, "lines": [message], "payload": {}}


def _receipt(
    verb: str, ok: bool, lines: Iterable[str], payload: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "verb": verb,
        "ok": bool(ok),
        "lines": [str(line) for line in lines],
        "payload": dict(payload or {}),
    }


def _find(root: Path, token: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Load one pending proposal WITH its age AND its suppression state.

    Delegates to :func:`pending_items` rather than re-reading the directory, so
    ``list`` and ``show`` cannot disagree about the same proposal. That is not
    a tidiness concern: Wave 1 already shipped this class once, when ``list``
    rendered ``0s ago`` and ``show <id>`` rendered ``unknown age`` for one
    proposal because two loaders existed. W2.1 found the second instance of it
    the same way - ``list`` knew a proposal was refused and ``show`` did not,
    so ``show`` cheerfully printed "accept with: /memory accept p2 --confirm"
    for a memory the user had already refused.
    """
    for item in pending_items(root=root, now=now):
        if str(item.get("id")) == str(token):
            return item
    return None


def _resolve_flags(rest: str) -> Tuple[str, Dict[str, str]]:
    """Split an argument string into its first token and its flags.

    Flags are ``--name value`` or ``--name=value``; a bare ``--name`` is
    ``""``. Splitting runs through ``shlex`` so a QUOTED reason
    (``--reason "not from the user"``) arrives as one value rather than as a
    value plus two stray positionals - the first version split on whitespace
    and therefore refused the very example its own help line teaches.

    An unrecognised flag is KEPT in the map so a caller who typo'd ``--confim``
    is refused rather than quietly treated as having no flags. A stray
    non-flag token lands in ``_positional``, which the mutating verbs reject
    rather than ignore, because silently discarding part of an argument is how
    a write happens with less consent than the user typed.
    """
    text = str(rest or "").strip()
    if not text:
        return "", {}
    try:
        import shlex

        parts = shlex.split(text)
    except ValueError:
        # An unbalanced quote is the user's typo, not a crash. Split it the
        # naive way so the resulting refusal names something actionable.
        parts = text.split()
    if not parts:
        return "", {}
    token = parts[0]
    flags: Dict[str, str] = {}
    index = 1
    while index < len(parts):
        item = parts[index]
        if not item.startswith("--"):
            flags["_positional"] = (flags.get("_positional", "") + " " + item).strip()
            index += 1
            continue
        if "=" in item:
            name, _, value = item[2:].partition("=")
            flags[name] = value
            index += 1
            continue
        name = item[2:]
        following = parts[index + 1] if index + 1 < len(parts) else ""
        if following and not following.startswith("--"):
            flags[name] = following
            index += 2
            continue
        flags[name] = ""
        index += 1
    return token, flags


def memory_command(
    verb: str,
    rest: str = "",
    *,
    repo_path: Any = None,
    log_root: Any = None,
    store: Any = None,
) -> Dict[str, Any]:
    """Dispatch one ``/memory`` verb. THE implementation, on every surface.

    ``verb`` outside :data:`MEMORY_VERBS` returns ``ok: False`` naming the
    valid set - a wrong verb is a usage error, not an empty result, because an
    empty result from an unknown verb reads as "nothing pending".

    ``accept`` and ``reject`` require BOTH an explicit id and ``--confirm``.
    The confirm token is deliberately not the default: "apply a memory" is the
    one act in this phase that must be typed on purpose, and a default that
    applied on an empty argument would make the confirmation theatre.
    """
    name = str(verb or "").strip().lower()
    if name not in MEMORY_VERBS:
        return _fail(
            name or "(none)",
            "unknown memory verb; use one of: " + ", ".join(MEMORY_VERBS),
        )
    root = inbox_root(repo_path, log_root)

    if name == "status":
        status = inbox_status(repo_path, log_root)
        return _receipt(
            "status",
            bool(status.get("available")),
            status_lines(status),
            status,
        )

    if name in ("list", "show", "diff"):
        token, _flags = _resolve_flags(rest)
        if name == "show" and token:
            # W2.5: the memory-DETAIL surface. `/memory show <id>` falls through
            # to an APPLIED memory when no pending proposal carries that id, so
            # one verb answers "what is this memory and where did it come from"
            # for both halves of the store. Without this the only way to see a
            # memory's provenance was to find the proposal that created it,
            # which for anything applied before this round does not exist.
            #
            # Checked BEFORE the inbox-availability guard, and that ordering is
            # the fix for a defect this round found: the guard ran first, so on
            # a repository with no `.inbox/` at all - the common case, since the
            # inbox lives outside the checkout by design - `/memory show <id>`
            # answered about the INBOX and never reached the applied memory the
            # reader had typed an id for.
            detail = _applied_detail(token, repo_path, store=store, log_root=log_root)
            if detail is not None:
                return _receipt(
                    name, True, detail["lines"], {"memory": detail["report"]}
                )
        if root is None:
            status = inbox_status(repo_path, log_root)
            return _receipt(name, False, status_lines(status), status)
        items = pending_items(repo_path, log_root)
        if not items:
            return _receipt(
                name,
                True,
                ["memory inbox  nothing pending in this repository"],
                {},
            )
        if not token:
            lines = [
                "memory inbox  {n} pending · oldest {age}".format(
                    n=len(items), age=items[0].get("age") or "unknown age"
                )
            ]
            suppressed = 0
            for item in items:
                provenance = item.get("provenance") or {}
                # W2.1 item 5: a refused proposal is rendered with its refusal
                # attached rather than quietly omitted, so "nothing pending" is
                # never the reason a reader cannot find something they know was
                # extracted.
                if item.get("suppressed"):
                    suppressed += 1
                    lines.append(
                        "  {id}  REFUSED {age:>8}  {trust:<20}  {preview}".format(
                            id=_plain(provenance.get("id") or item.get("id") or "?", 40),
                            age=_plain(item.get("age") or "?", 10),
                            trust="stays refused",
                            preview=_sanitize(
                                str(item.get("text") or "").splitlines()[0]
                                if str(item.get("text") or "").strip()
                                else "(no text)",
                                52,
                            ),
                        )
                    )
                    if item.get("suppression_reason"):
                        lines.append(
                            "      you said: " + _plain(item["suppression_reason"], 60)
                        )
                    continue
                lines.append(
                    "  {id}  {age:>10}  {trust:<20}  {preview}".format(
                        id=_plain(provenance.get("id") or item.get("id") or "?", 40),
                        age=_plain(item.get("age") or "?", 12),
                        trust=_plain(
                            "UNTRUSTED"
                            if provenance.get("untrusted")
                            else (provenance.get("source_type") or "unrecognised"),
                            20,
                        ),
                        preview=_sanitize(
                            str(item.get("text") or "").splitlines()[0]
                            if str(item.get("text") or "").strip()
                            else "(no text)",
                            60,
                        ),
                    )
                )
            if suppressed:
                lines.append(
                    "  {n} proposal(s) were refused before and stay refused - the "
                    "text is unchanged, so accepting is refused too".format(n=suppressed)
                )
            return _receipt(name, True, lines, {"items": items})
        record = _find(root, token)
        if record is None:
            return _fail(name, f"no pending proposal with id {token!r}")
        return _receipt(name, True, review_lines(record), {"item": record})

    if name == "applied":
        ranked = ranked_memories(repo_path, store=store)
        if not ranked.get("available"):
            return _receipt(
                name, False, [f"memory  unavailable: {ranked['reason']}"], ranked
            )
        return _receipt(
            name, True, ranked_memory_lines(ranked, log_root=log_root), ranked
        )

    if name == "export":
        fmt = "json" if "json" in str(rest or "").lower() else "markdown"
        document = export_memories(repo_path, store=store)
        return _receipt(
            "export",
            True,
            memory_export_lines(document, fmt=fmt),
            document,
        )

    # accept / reject. Both write, so both demand an id AND --confirm.
    token, flags = _resolve_flags(rest)
    if root is None:
        status = inbox_status(repo_path, log_root)
        return _receipt(name, False, status_lines(status), status)
    if not token:
        return _fail(
            name,
            f"/memory {name} needs the proposal id: /memory {name} <id> --confirm",
        )
    unknown = sorted(
        key
        for key in flags
        if key not in ("confirm", "reason", "note", "json", "yes")
    )
    if unknown:
        return _fail(
            name,
            f"unrecognised option(s): --{', --'.join(unknown)}; "
            f"/memory {name} takes --confirm and --reason only",
        )
    if "confirm" not in flags:
        return _fail(
            name,
            f"/memory {name} changes what the agent will remember; it needs "
            f"the explicit --confirm token",
        )
    record = _find(root, token)
    if record is None:
        return _fail(name, f"no pending proposal with id {token!r}")
    provenance = record.get("provenance") or provenance_of(record)

    if name == "reject":
        reason = str(flags.get("reason") or flags.get("note") or "")
        ledger = _append_rejection(root, record, reason)
        marked = _mark_decided(root, token)
        lines = [
            "refused: {id} will not be proposed again with this text".format(
                id=provenance.get("id") or token
            )
        ]
        lines.append(
            "  recorded in the rejection ledger - the next extraction is "
            "expected to consult it"
        )
        if ledger.get("recorded"):
            lines.append(f"  ledger: {_plain(ledger['row'].get('digest'), 40)}")
        else:
            lines.append(f"  LEDGER NOT WRITTEN: {_plain(ledger.get('reason'), 60)}")
        if not marked.get("marked"):
            lines.append(f"  the proposal file was not marked decided: {marked.get('reason')}")
        return _receipt(
            "reject",
            bool(ledger.get("recorded")),
            lines,
            {"item": record, "ledger": ledger, "marked": marked},
        )

    # accept. The store write is attempted only here, after the confirm gate.
    lines: List[str] = []
    applied = _apply(record, provenance, repo_path, store, root)
    if not applied.get("applied"):
        if applied.get("refused"):
            # W2.1 item 5. This is a DURABLE refusal and it says so in its own
            # words, because the alternative is a reader who typed `--confirm`
            # and cannot tell whether the memory was saved or the product
            # quietly ignored them.
            lines.append("REFUSED AND STAYS REFUSED: " + _sanitize(applied["reason"], 66))
            lines.append(
                "  nothing was written to the store; this is the same text you "
                "already refused, matched on its content digest"
            )
            refusal = applied.get("refusal") or {}
            if refusal.get("reason"):
                lines.append("  your reason was: " + _plain(refusal["reason"], 60))
            return _receipt(
                "accept", False, lines, {"item": record, "apply": applied}
            )
        lines.append(
            "NOT APPLIED: {reason}".format(reason=applied.get("reason") or "unknown")
        )
        lines.append(
            "  the proposal is still pending; nothing the agent remembers has "
            "changed"
        )
        return _receipt("accept", False, lines, {"item": record, "apply": applied})
    marked = _mark_decided(root, token)
    lines.append(
        "applied: {text}".format(
            text=_sanitize(str(record.get("text") or ""), 70)
        )
    )
    lines.append(
        "  provenance: {kind} · session {session} · digest {digest}".format(
            kind=_plain(provenance.get("source_type") or "unrecognised", 20),
            session=_plain(provenance.get("source_session") or "not recorded", 24),
            digest=_plain(provenance.get("digest"), 40),
        )
    )
    lines.append("  this was the ONLY write; nothing else about the inbox changed")
    if not marked.get("marked"):
        lines.append(f"  the proposal file was not marked decided: {marked.get('reason')}")
    return _receipt(
        "accept", True, lines, {"item": record, "apply": applied, "marked": marked}
    )


def _refusal_for(root: Any, record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The recorded refusal for this record's content, or ``None``.

    The lookup is by CONTENT DIGEST and nothing else - not by proposal id, not
    by filename, not by position. A producer mints proposal ids, so an id-based
    match would be defeated by the next extraction round; a content digest is
    the one identity two extractions of the same sentence share.
    """
    if root is None:
        return None
    digest = provenance_of(record).get("digest") or digest_of(record.get("text") or "")
    return rejected_digests(root).get(str(digest)) or None


def _applied_detail(
    token: str, repo_path: Any, *, store: Any = None, log_root: Any = None
) -> Optional[Dict[str, Any]]:
    """One APPLIED memory by id, fully attributed, or ``None`` if there is none.

    Returns ``None`` rather than a refusal receipt so the caller can keep
    looking: this is a fall-through helper, and a surface that printed "no such
    memory" from here would swallow the pending-proposal answer behind it.
    """
    ranked = ranked_memories(repo_path, store=store)
    if not ranked.get("available"):
        return None
    for row in ranked.get("rows") or ():
        if str(row.get("id")) != str(token):
            continue
        return {
            "report": row,
            "lines": memory_detail_lines(row, log_root=log_root),
        }
    return None


def _apply(
    record: Mapping[str, Any],
    provenance: Mapping[str, Any],
    repo_path: Any,
    store: Any,
    root: Any = None,
) -> Dict[str, Any]:
    """Write ONE approved memory into the store, and report what happened.

    Fails closed: a store that cannot be opened, refuses the write, or raises
    reports ``applied: False`` and the proposal STAYS pending. A write that
    half-succeeded and reported success is the receipt-lies failure this repo
    treats as the worst outcome.
    """
    text = str(record.get("text") or "").strip()
    if not text:
        return {"applied": False, "reason": "the proposal carries no memory text"}
    # W2.1 item 5: the DURABILITY half. Recording a refusal is not enough - the
    # refusal has to be HONOURED by the path that writes memory, or "rejection
    # is permanent" is a claim about a file rather than about the product. The
    # ledger is consulted here, at the last point before the store is touched,
    # so no code path can write a refused memory.
    #
    # Deliberately a REFUSAL and not a silent skip: the caller must be able to
    # tell the difference between "this was refused and stayed refused" and
    # "this write did not happen for some other reason".
    refused = _refusal_for(root, record)
    if refused:
        return {
            "applied": False,
            "reason": (
                "you refused this exact text on {at} and it has been proposed "
                "again; the content digest is unchanged, so it is the same "
                "memory and it stays refused".format(
                    at=_plain(refused.get("at") or "(no time recorded)", 40)
                )
            ),
            "refused": True,
            "refusal": refused,
        }
    handle = store if store is not None else _open_store()
    if handle is None:
        return {"applied": False, "reason": "the memory store could not be opened"}
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    try:
        row_id = handle.promote_decision(
            text,
            category=str(provenance.get("category") or payload.get("category") or "general"),
            source=str(payload.get("source") or "inbox"),
            task_id=str(payload.get("task_id") or ""),
            repo_path=str(repo_path) if repo_path else None,
            provenance=dict(provenance),
            dedupe=True,
        )
    except Exception as exc:
        return {"applied": False, "reason": f"the store refused the write: {exc}"}
    if row_id is None:
        return {
            "applied": False,
            "reason": "the store returned no row id, so nothing is claimed to be saved",
        }
    return {"applied": True, "reason": "", "row_id": row_id, "digest": provenance.get("digest")}


def assert_never_applies_without_a_human_act() -> Tuple[bool, str]:
    """The gate: a read-only verb cannot write, and a write needs ``--confirm``.

    In-file (not in the test suite) so the check runs in the product, and
    returned as ``(ok, reason)`` rather than raised so a caller can report it.

    It asserts two things that reading the code would not catch:

    1. :data:`_READ_ONLY_VERBS` and the mutating half of
       :data:`MEMORY_VERBS` are DISJOINT, and the union is the whole
       vocabulary - so a new verb cannot be neither read-only nor mutating.
    2. Driving the three verbs that a reader would expect to be safe cannot
       produce a rejection-ledger row in a scratch inbox.
    """
    read_only = set(_READ_ONLY_VERBS)
    declared = set(MEMORY_VERBS)
    mutating = declared - read_only
    # The first version of this check was `read_only & mutating`, where
    # `mutating` was computed as `MEMORY_VERBS - read_only`. That intersection
    # is EMPTY BY CONSTRUCTION - it can never fire, so it was a green light
    # wired to nothing, which is worse than an absent check because a reader
    # believes it is covered. Replaced with two assertions that CAN fire:
    #
    # * every read-only verb is IN the vocabulary. A typo'd or half-removed
    #   read-only verb outside `MEMORY_VERBS` would be a verb the dispatcher
    #   never routes and the gate never covers.
    # * the two sets partition the vocabulary, so a verb can never be neither
    #   (ungated) nor both.
    stray = read_only - declared
    if stray:
        return False, (
            "read-only verbs outside the vocabulary: "
            + ", ".join(sorted(stray))
            + " - a verb nothing dispatches is a verb nothing gates"
        )
    if read_only | mutating != declared:
        return False, "the verb vocabulary is not partitioned"
    if mutating != {"accept", "reject"}:
        return False, (
            "the mutating verbs changed to "
            + ", ".join(sorted(mutating))
            + " - a new writer needs its own confirm gate and its own test"
        )

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        inbox = root / ".inbox"
        inbox.mkdir(parents=True)
        (inbox / "probe.json").write_text(
            json.dumps({"id": "probe", "text": "the probe text", "source_type": "user_stated"}),
            encoding="utf-8",
        )
        before = rejected_digests(inbox)
        for verb in ("status", "list", "show", "diff", "applied", "export"):
            memory_command(verb, "", repo_path=root, store=None)
        # and a write WITHOUT the confirm token
        memory_command("reject", "probe", repo_path=root, store=None)
        after = rejected_digests(inbox)
        if len(after) != len(before):
            return False, (
                f"a read-only verb (or an unconfirmed write) added "
                f"{len(after) - len(before)} rejection row(s)"
            )
        if (inbox / "probe.json.decided").exists():
            return False, "an unconfirmed write marked a proposal decided"
    return True, ""
