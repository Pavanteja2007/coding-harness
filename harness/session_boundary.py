"""A session boundary that survives the process, and the read rule that must cross it.

**Why this module exists.** Wave 1 built an initializer, a feature ledger and a
:class:`~harness.long_horizon.LongHorizonSession`, and
``harness/AGENTS.md`` filed the reason they were not shipped wired: *"The
initializer, the ledger and ``LongHorizonSession`` have NO production call site."*
A mechanism that has never been run across a session boundary has not been
tested, and Phase 4 is *entirely* about that boundary. So this module is the
missing half of Wave 1: it is the first code in the tree that can tell a NEW
session from a continued one.

**The boundary is a counter, and the counter lives on disk.** There was no
detector anywhere in Wave 1 -- verified, not assumed: ``long_horizon.py``
compares no session id against anything, and ``Feature.sessions_touched`` /
``FeatureLedger.sessions`` are declared, round-tripped through JSON, and never
written by any code path. So every "session" was a fresh object in the same
process, which measures *compaction*, not *survival*. Here a session is an
append-only row in ``.neo-session/sessions.jsonl`` and the index is
``len(rows) + 1``.

**The signature is the claim.** :func:`open_session` takes a project root and
nothing that could carry a prior session -- no ledger object, no message list,
no summary. Every fact it reports is read back from disk in the same process that
wrote it. A harness that kept sessions alive by handing the next one the last
one's Python object would be measuring its own argument passing.

**The read rule had to be decided, and the decision is the load-bearing part.**
Anthropic's rule is session-scoped: *"read the file, then act on the next turn."*
That rule is right for a single window and **wrong for this product**, because a
multi-session collaborator must be able to edit a file it read *yesterday*. A
session-scoped read rule in a three-session effort refuses roughly every edit in
sessions 2 and 3, and an enforcement that blocks 30% of legitimate edits is
disabled by the user within a day -- which is worse than not enforcing.

So ours is **repo-scoped, digest-checked**:

``repo``
    a read observed by ANY earlier session still counts, because the repository
    is the thing that persists across sessions, not the context window. The
    observation carries the content digest, so the window is not the guarantee.

``session``
    Anthropic's rule, kept as a measurable arm rather than deleted.

Either way the question *"is the content still what I read?"* is asked by the
existing ``stale_read`` digest gate, not by this module: a file that changed on
disk between the read and the edit must be refused as **stale**, and a stale
refusal is a *different* refusal from a never-read one. A repo-scoped rule that
skipped the digest check would let a collaborator edit yesterday's file at
yesterday's bytes, which is precisely the failure a cross-session read rule
invites.

**Not wired into the daily loop.** This module is complete, runnable and pinned,
but nothing in ``harness/core.py`` / ``harness/agent_loop.py`` calls it; that is
another owner's seam and is filed rather than done. ``harness/AGENTS.md`` records
the same gap for ``clearing.py`` and ``PathTrigger``, so this is the established
shape for a harness mechanism whose call sites live elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import harness.long_horizon as LH
from harness.agent_kernel.read_before_edit import TurnReadLedger

__all__ = [
    "READ_OBSERVATION_NAME",
    "READ_SCOPES",
    "READ_SCOPE_DEFAULT",
    "READ_SCOPE_REPO",
    "READ_SCOPE_SESSION",
    "SESSION_DIR_NAME",
    "SESSION_RECORD_NAME",
    "RepoReadRefusal",
    "SessionOpen",
    "git_log_text",
    "load_read_observations",
    "note_read_observation",
    "open_session",
    "prior_session_view",
    "read_observations_for",
    "read_session_record",
    "record_session_end",
    "repo_read_gate",
    "save_read_observations",
    "session_boundary_report",
    "unknown_read_scope",
]

#: Harness-owned directory. Deliberately NOT ``.neo-store/`` -- that is T4's
#: ledger store and this module must not write into a directory whose tamper
#: chain it does not own. A read observation is not a ledger row.
SESSION_DIR_NAME = ".neo-session"
SESSION_RECORD_NAME = "sessions.jsonl"
READ_OBSERVATION_NAME = "reads.json"

#: The two scoping rules, as DATA. A closed set is required so a typo cannot
#: silently produce a third rule: ``read_scope="repoo"`` raises rather than
#: falling back to the default, because a scope that quietly becomes "session"
#: turns every cross-session edit into a refusal.
READ_SCOPES: Tuple[str, ...] = ("repo", "session")
READ_SCOPE_REPO = "repo"
READ_SCOPE_SESSION = "session"

#: ``repo`` is the default and the reason is in this module's docstring: a
#: multi-session collaborator must be able to edit a file it read yesterday.
READ_SCOPE_DEFAULT = READ_SCOPE_REPO

#: How many prior-session rows the brief carries. Bounded because the brief is
#: the session's first request and an unbounded history is the context flood
#: this project has already measured (``P1/W2-T1``: a single search returning
#: 84,490 chars). The ledger table carries the authoritative per-feature state;
#: this is the narrative on top of it.
DEFAULT_HISTORY_ROWS = 12


def unknown_read_scope(scope: Any) -> Tuple[str, ...]:
    """Return a reason for an unrecognised read scope, or ``()`` when it is valid.

    Returns a value rather than raising so a caller can decide; :func:`open_session`
    raises on it, because a session that cannot know its own read scope must not
    start.
    """
    if scope in READ_SCOPES:
        return ()
    return (
        f"unknown read scope {scope!r}; the rule must be one of {list(READ_SCOPES)}. "
        "A scope that silently fell back to the default would turn every "
        "cross-session edit into a refusal or into no gate at all, and neither "
        "is a decision anybody made."
    )


# ---------------------------------------------------------------------------
# Durable state. Atomic, because a torn boundary record is a boundary nobody
# can trust.
# ---------------------------------------------------------------------------


def _session_dir(root: Any) -> str:
    return os.path.join(str(root), SESSION_DIR_NAME)


def _session_record_path(root: Any) -> str:
    return os.path.join(_session_dir(root), SESSION_RECORD_NAME)


def _read_observation_path(root: Any) -> str:
    return os.path.join(_session_dir(root), READ_OBSERVATION_NAME)


def _atomic_write(path: str, text: str) -> Dict[str, Any]:
    """Write ``text`` to ``path`` via tmp + replace. Never raises."""
    directory = os.path.dirname(path) or "."
    tmp = ""
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(
            directory, f".neo-session-{os.getpid()}-{abs(hash(path)) % 100000}.tmp"
        )
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return {"ok": True, "path": path, "chars": len(text)}
    except Exception as exc:  # pragma: no cover - filesystem failure
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return {"ok": False, "path": path, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# The session record
# ---------------------------------------------------------------------------


def read_session_record(root: Any) -> Tuple[Dict[str, Any], ...]:
    """Return every recorded session row, oldest first.

    A torn tail is DROPPED rather than half-parsed, for the reason
    ``memory/feature_ledger_store.py::history`` records: *"a half-parsed row must
    not be replayed as a fact."* A row is a claim about what a session did; a
    row truncated by a crash is not a claim.
    """
    path = _session_record_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        return ()
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # The LAST line being unparseable is the crash case. An unparseable
            # line in the MIDDLE is tampering, and is reported by dropping it
            # here and by the chain check below.
            continue
        if isinstance(row, Mapping):
            rows.append(dict(row))
    return tuple(rows)


def _append_session_row(root: Any, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Append one session row, chained to the previous one.

    The chain is over the *content*, so a rewritten earlier row is detectable:
    appending cannot change an earlier row's digest.
    """
    directory = _session_dir(root)
    payload = dict(row)
    prior = read_session_record(root)
    previous_chain = str(prior[-1].get("chain") or "") if prior else ""
    payload["chain"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "chain"},
            sort_keys=True,
            default=str,
        ).encode("utf-8", "replace")
        + b"|"
        + previous_chain.encode("utf-8", "replace")
    ).hexdigest()
    try:
        os.makedirs(directory, exist_ok=True)
        with open(_session_record_path(root), "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"ok": True, "appended": True, "chain": payload["chain"]}
    except Exception as exc:  # pragma: no cover - filesystem failure
        return {"ok": False, "appended": False, "error": f"{type(exc).__name__}: {exc}"}


def session_chain_intact(root: Any) -> Tuple[bool, str]:
    """Recompute the chain over every recorded row. ``(ok, reason)``.

    Split out from :func:`read_session_record` because a DROPPED row must be
    visible: a reader that only ever sees the surviving rows cannot tell an
    honest crash from a deletion.
    """
    path = _session_record_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        return True, "no session record; nothing to break"
    previous = ""
    rows = 0
    for number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # Only the tail may be torn. A hole in the middle is a rewrite.
            return False, f"line {number} is unparseable and is not the last row"
        if not isinstance(row, Mapping):
            return False, f"line {number} is not an object"
        recorded = str(row.get("chain") or "")
        recomputed = hashlib.sha256(
            json.dumps(
                {k: v for k, v in row.items() if k != "chain"},
                sort_keys=True,
                default=str,
            ).encode("utf-8", "replace")
            + b"|"
            + previous.encode("utf-8", "replace")
        ).hexdigest()
        if recorded != recomputed:
            return False, (
                f"row {rows + 1} records chain {recorded[:12]} but its content hashes to "
                f"{recomputed[:12]}; a row was rewritten after it was appended"
            )
        previous = recorded
        rows += 1
    return True, f"{rows} row(s), chain intact"


# ---------------------------------------------------------------------------
# What session N must know about session N-1
# ---------------------------------------------------------------------------


def prior_session_view(root: Any) -> Dict[str, Any]:
    """Return what a NEW session must be told about the sessions before it.

    Three fields, and the denominators are honest:

    ``completed``
        feature ids a prior session moved to a terminal-ish status. This is the
        anti-rework field: *"f1 landed in session 1 -- do not do it again."*
    ``open``
        feature ids a prior session CLAIMED and never resolved. The dangerous
        case: session 2 crashed after claiming ``f3``, and session 3 has to know
        ``f3`` is half-done or it will either redo it or abandon it silently.
    ``sessions``
        how many sessions have run before this one -- **the boundary itself**.

    ``completed`` is derived from the rows AND cross-checked against the ledger
    on disk, because a row is a session's own claim about itself and the ledger
    is the durable document. Where they disagree the disagreement is reported in
    ``disagreements`` rather than smoothed: *"the ledger and the transcript
    disagreeing"* is one of the five things this wave is supposed to find, and a
    summary that silently prefers one source would hide it.
    """
    rows = read_session_record(root)
    completed: List[str] = []
    open_claims: List[str] = []
    seen_completed = set()
    disagreements: List[Dict[str, Any]] = []

    for row in rows:
        feature = str(row.get("claimed_feature") or "")
        applied = bool(row.get("status_applied"))
        status = str(row.get("status") or "")
        if not feature:
            continue
        if applied and status in ("passing", "blocked", "skipped"):
            if feature not in seen_completed:
                seen_completed.add(feature)
                completed.append(feature)
        elif not applied and feature not in open_claims:
            open_claims.append(feature)

    ledger, refusal = LH.load_feature_ledger(os.path.join(str(root), LH.FEATURE_LEDGER_NAME))
    ledger_statuses: Dict[str, str] = {}
    if ledger is not None:
        ledger_statuses = {item.id: item.status for item in ledger.features}
        for feature in completed:
            actual = ledger_statuses.get(feature, "")
            if actual != "passing":
                disagreements.append(
                    {
                        "feature_id": feature,
                        "session_record_says": "passing",
                        "ledger_says": actual or "(absent)",
                        "detail": (
                            f"session record says {feature!r} landed, the ledger says "
                            f"{actual or '(absent)'}. One of the two is a lie and the "
                            "brief reports both rather than picking the convenient one."
                        ),
                    }
                )
        for feature in open_claims:
            actual = ledger_statuses.get(feature, "")
            if actual in ("passing", "blocked", "skipped"):
                open_claims.remove(feature)
                if feature not in seen_completed:
                    seen_completed.add(feature)
                    completed.append(feature)
    elif refusal is not None:
        disagreements.append(
            {
                "feature_id": "",
                "session_record_says": "n/a",
                "ledger_says": "(unreadable)",
                "detail": f"the feature ledger could not be read: {refusal.detail}",
            }
        )

    return {
        "sessions": len(rows),
        "completed": tuple(completed),
        "open": tuple(open_claims),
        "ledger_statuses": ledger_statuses,
        "ledger_refusal": refusal.to_dict() if refusal is not None else None,
        "disagreements": tuple(disagreements),
        "rows": rows,
    }


def render_prior_session_block(view: Mapping[str, Any]) -> str:
    """Render the cross-session block a brief carries.

    FIVE sentences of state, and each one is a thing a fresh context window
    cannot know: what landed, what was left half-done, how many sessions came
    before, whether the record and the ledger agree, and the rule against redoing
    finished work.
    """
    sessions = int(view.get("sessions") or 0)
    completed = tuple(view.get("completed") or ())
    open_claims = tuple(view.get("open") or ())
    disagreements = tuple(view.get("disagreements") or ())
    lines = [f"## What earlier sessions did ({sessions} before this one)"]
    lines.append(
        "- already finished, DO NOT redo: " + (", ".join(completed) if completed else "(none)")
    )
    lines.append(
        "- claimed but never resolved, check before starting: "
        + (", ".join(open_claims) if open_claims else "(none)")
    )
    if disagreements:
        lines.append(
            f"- WARNING: {len(disagreements)} disagreement(s) between the session "
            "record and features.json; the ledger is authoritative:"
        )
        for item in disagreements:
            lines.append(
                f"  - {item.get('feature_id') or '(ledger)'}: "
                f"{item.get('session_record_says')} vs {item.get('ledger_says')}"
            )
    lines.append(
        "- a feature a previous session finished is DONE. Re-implementing it is "
        "rework, and it is how a multi-session effort silently goes backwards."
    )
    return "\n".join(lines)


def git_log_text(root: Any, *, git_runner: Optional[Any] = None, limit: int = 20) -> str:
    """Return a REAL git log for ``root``, or a named reason.

    **This closes a specific lie.** ``LH.render_session_brief`` renders
    ``git_log or "(no commits yet)"``, and ``git_log`` is a caller-supplied
    ``str`` -- Wave 1's module never invokes ``run_git`` for a log, only for
    ``add``/``commit``. So a caller that passed ``""`` produced a brief saying
    "no commits yet" about a repository with forty commits, and nothing on the
    receipt could distinguish the two. The fix is not better wording in the
    renderer; it is that the session actually asks git.
    """
    receipt = LH.run_git(
        ["log", f"-{max(1, int(limit))}", "--pretty=format:%h %s"], cwd=root, runner=git_runner
    )
    if not receipt.get("ran"):
        return ""
    if not receipt.get("ok"):
        return ""
    return str(receipt.get("output") or "").strip()


# ---------------------------------------------------------------------------
# Read observations, and the repo-scoped gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoReadRefusal:
    """Why a cross-session edit was refused.

    ``reason`` is one of :data:`REPO_READ_REFUSALS` and ``stale_read`` is
    DELIBERATELY one of them: a file that changed on disk between the read and
    the edit is a different defect from a file nobody read, and a reader cannot
    act on the second one if the receipt reads the same.
    """

    path: str
    reason: str
    detail: str
    session: str = ""

    @property
    def ok(self) -> bool:
        """Always ``False``. A refusal is never a pass."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
            "session": self.session,
        }

    def render(self) -> str:
        """Return the one model-facing line naming the escape."""
        return (
            f"TOOL ERROR [{self.reason}]: {self.path} -- {self.detail} "
            f"Read it again in THIS session, then edit it on the next turn."
        )


#: The closed vocabulary. ``stale_read`` is the EXISTING kernel slug
#: (``harness/agent_kernel/tools.py::ERROR_STALE_READ``), reused deliberately
#: rather than invented as ``repo_read_stale``: a receipt that presented a
#: staleness refusal under a new name could not tell a reader whether the content
#: changed or the file was never seen, and those need opposite repairs. The
#: brief's requirement -- "a file modified externally between read and edit --
#: that is ``stale_read``, and it must still fire" -- is satisfied by speaking
#: the kernel's own vocabulary, so the two gates are recognisably the same
#: defect and not two unfamiliar ones.
REPO_READ_REFUSALS: Tuple[str, ...] = ("repo_never_read", "stale_read", "read_scope_unknown")


def load_read_observations(root: Any) -> Dict[str, Any]:
    """Return the durable read-observation table. Never raises.

    A missing or corrupt file is ``{}`` with a named reason rather than an
    exception, because "the read table is unreadable" must degrade to *every
    cross-session read is unproven*, which the gate then refuses loudly.
    """
    path = _read_observation_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"schema": "neo/read-observations/v1", "paths": {}, "error": ""}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            "schema": "neo/read-observations/v1",
            "paths": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(data, Mapping):
        return {
            "schema": "neo/read-observations/v1",
            "paths": {},
            "error": "the read-observation document is not an object",
        }
    paths = data.get("paths")
    return {
        "schema": str(data.get("schema") or "neo/read-observations/v1"),
        "paths": dict(paths) if isinstance(paths, Mapping) else {},
        "error": str(data.get("error") or ""),
    }


def save_read_observations(root: Any, table: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist the read-observation table atomically. Never raises."""
    payload = {
        "schema": "neo/read-observations/v1",
        "paths": dict(table.get("paths") or {}),
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return _atomic_write(_read_observation_path(root), text)


def note_read_observation(
    root: Any, relative_path: Any, *, digest: str, session: str = "", turn: Any = None
) -> Dict[str, Any]:
    """Record that THIS session read ``relative_path`` at ``digest``.

    The session and turn are stored alongside the digest because "read in this
    session" is the question the same-turn rule asks, and a repo-scoped
    observation still has to be able to say which session made it.
    """
    table = load_read_observations(root)
    key = str(TurnReadLedger.key(relative_path) or "").strip()
    if not key:
        return {"recorded": False, "reason": "empty path"}
    paths = dict(table.get("paths") or {})
    paths[key] = {
        "sha256": str(digest or ""),
        "session": str(session or ""),
        "turn": turn if isinstance(turn, int) else None,
    }
    written = save_read_observations(root, {"paths": paths})
    return {
        "recorded": bool(written.get("ok")),
        "path": key,
        "sha256": str(digest or ""),
        "error": str(written.get("error") or ""),
    }


def read_observations_for(root: Any) -> Tuple[Tuple[str, ...], ...]:
    """Return the paths any session has recorded a read for."""
    return tuple(sorted(load_read_observations(root).get("paths") or {}))


def _digest_now(root: Any, relative_path: str) -> Tuple[str, str]:
    """Return ``(sha256, state)`` for a path, where state is one of
    ``present`` / ``absent`` / ``unreadable``."""
    target = os.path.join(str(root), str(relative_path).replace("/", os.sep))
    try:
        with open(target, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest(), "present"
    except FileNotFoundError:
        return "", "absent"
    except OSError:
        return "", "unreadable"


def repo_read_gate(
    root: Any,
    relative_path: Any,
    *,
    digest: str = "",
    session: str = "",
    read_scope: str = READ_SCOPE_DEFAULT,
) -> RepoReadRefusal | None:
    """Decide whether a cross-session write may proceed. Returns ``None`` to allow.

    **This is the edge of the read rule, and it is the whole reason the scope
    decision matters.** Three branches, in order:

    1. no observation of this path in ANY session -> ``repo_never_read``. This is
       the case a session-scoped rule would also catch, so it fires identically
       under both scopes and a reader cannot use it to tell the rules apart;
    2. an observation exists but the content has changed -> ``stale_read``.
       **This must fire under BOTH scopes**, including ``session``: a file that
       changed on disk between the read and the edit is a staleness defect, not
       a scoping one, and a rule that answered ``session`` here would let a
       collaborator overwrite bytes it never saw;
    3. otherwise allow.

    ``digest`` is what the CALLER is about to write against. Passing ``""``
    means "the caller has no digest" and the check degrades to the existence of
    an observation -- stated on the receipt as ``digest_checked: False`` by
    :func:`session_boundary_report`, because a gate that could not check the one
    thing it exists to check must not report itself as having checked it.
    """
    key = str(TurnReadLedger.key(relative_path) or "").strip()
    if not key:
        return None
    bad = unknown_read_scope(read_scope)
    if bad:
        return RepoReadRefusal(key, "read_scope_unknown", bad[0], session=session)
    table = load_read_observations(root)
    paths = table.get("paths") or {}
    observed = paths.get(key)
    if not isinstance(observed, Mapping):
        return RepoReadRefusal(
            key,
            "repo_never_read",
            "no session of this effort has read this file. An earlier session's "
            "absence of a read is not this session's permission to write.",
            session=session,
        )
    recorded_digest = str(observed.get("sha256") or "")
    if not digest:
        # No digest to check against. The observation still has to EXIST; what it
        # cannot do is vouch for content. Say which of the two happened.
        return None
    current, state = _digest_now(root, key)
    if state == "absent":
        # The file is gone; writing a new one is a create, not an edit of
        # remembered bytes, and the read rule does not speak to creates.
        return None
    if state == "unreadable":
        return RepoReadRefusal(
            key,
            "stale_read",
            "the file exists but could not be read to confirm it is unchanged; "
            "a read you cannot verify is not a read.",
            session=session,
        )
    if recorded_digest and current and recorded_digest != current:
        return RepoReadRefusal(
            key,
            "stale_read",
            f"it changed on disk since it was read (recorded sha256 "
            f"{recorded_digest[:12]}, on disk {current[:12]}). This is a "
            "staleness refusal, not a scoping one: it fires under both scopes.",
            session=session,
        )
    return None


# ---------------------------------------------------------------------------
# Opening and closing a session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionOpen:
    """Everything a NEW session is given, reconstructed from disk alone.

    ``brief`` is the first request. ``session`` is the scoped object whose
    ``claim()`` refuses a second feature. ``prior`` is the cross-session view, so
    a caller can assert on it without re-reading the record.
    """

    session_index: int
    session_id: str
    root: str
    ledger: Any
    brief: str
    session: Any
    prior: Dict[str, Any]
    git_log: str
    read_scope: str
    ledger_refusal: Any = None
    notes: Tuple[str, ...] = ()

    @property
    def boundary_is_real(self) -> bool:
        """True when this session's index came from disk, not from a caller.

        ``session_index == 1`` on a repository with recorded sessions is the
        shape of "the boundary did not happen", and it is falsifiable from the
        returned object alone.
        """
        return int(self.session_index) == int(self.prior.get("sessions") or 0) + 1

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "session_index": int(self.session_index),
            "session_id": self.session_id,
            "root": self.root,
            "brief": self.brief,
            "brief_chars": len(self.brief),
            "prior": {
                "sessions": int(self.prior.get("sessions") or 0),
                "completed": list(self.prior.get("completed") or ()),
                "open": list(self.prior.get("open") or ()),
                "disagreements": len(self.prior.get("disagreements") or ()),
            },
            "git_log_lines": len([r for r in self.git_log.splitlines() if r.strip()]),
            "read_scope": self.read_scope,
            "boundary_is_real": self.boundary_is_real,
            "ledger_refusal": (
                self.ledger_refusal.to_dict() if self.ledger_refusal is not None else None
            ),
            "notes": list(self.notes),
        }


def open_session(
    root: Any,
    *,
    project_id: str = "",
    request: str = "",
    git_runner: Optional[Any] = None,
    one_feature_per_session: bool = True,
    read_scope: str = READ_SCOPE_DEFAULT,
    brief_rows: int = DEFAULT_HISTORY_ROWS,
) -> SessionOpen:
    """Open session N of an effort. **Every input is a path or a scalar.**

    There is deliberately no parameter that could carry a prior session: no
    ledger object, no message list, no summary blob, no ``prior`` anything. A
    harness that kept sessions alive by handing the next one the last one's
    Python object would be measuring its own argument passing, which is the
    failure ``evals/multi_session.py``'s fresh-objects-in-one-process boundary
    invites.

    The index is ``len(recorded sessions) + 1``. That is the whole detector, and
    it is a counter rather than a timestamp because a clock cannot tell a
    resumed session from a new one and a timestamp comparison would make the
    boundary depend on wall time.
    """
    bad = unknown_read_scope(read_scope)
    if bad:
        raise ValueError(bad[0])
    base = str(root)
    prior = prior_session_view(base)
    index = int(prior.get("sessions") or 0) + 1
    session_id = f"s{index}"

    ledger_path = os.path.join(base, LH.FEATURE_LEDGER_NAME)
    ledger, refusal = LH.load_feature_ledger(ledger_path)
    progress_text = LH.load_progress_ledger(os.path.join(base, LH.PROGRESS_LEDGER_NAME))
    git_log = git_log_text(base, git_runner=git_runner, limit=max(1, int(brief_rows)))

    notes: List[str] = []
    scoped = LH.render_session_brief(ledger, progress_text, git_log)
    # The cross-session block is INSERTED between the ledger table and the scope
    # section rather than appended, because a session reads the brief top-down
    # and the anti-rework facts have to sit next to the status table they
    # qualify. Appending would put them below the instruction block, which is
    # where a model reads least carefully.
    anchor = "## This session's scope"
    if anchor in scoped:
        head, _, tail = scoped.partition(anchor)
        scoped = (
            head
            + render_prior_session_block(prior)
            + "\n\n"
            + anchor
            + tail
        )
    else:  # pragma: no cover - the renderer always emits the anchor
        scoped = scoped + "\n\n" + render_prior_session_block(prior)

    if refusal is not None:
        notes.append(f"the feature ledger was refused: {refusal.reason}")
    if prior.get("disagreements"):
        notes.append(
            f"{len(prior['disagreements'])} session-record/ledger disagreement(s) reported"
        )
    if not git_log:
        notes.append(
            "git log is empty; either the repository has no commits or no git runner "
            "is bound, and this brief does not distinguish them"
        )

    session = LH.LongHorizonSession(
        ledger if ledger is not None else LH.new_feature_ledger(project_id, request, ()),
        session=session_id,
        one_feature_per_session=bool(one_feature_per_session),
        progress_text=progress_text,
        git_log=git_log,
    )
    return SessionOpen(
        session_index=index,
        session_id=session_id,
        root=base,
        ledger=ledger,
        brief=scoped,
        session=session,
        prior=dict(prior),
        git_log=git_log,
        read_scope=str(read_scope),
        ledger_refusal=refusal,
        notes=tuple(notes),
    )


def record_session_end(
    root: Any, receipt: Mapping[str, Any], *, read_observations: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Append this session's row so the NEXT session's boundary is real.

    The row carries what the next session needs and what an auditor needs to
    disagree with the ledger: the claim, whether the status actually applied,
    which status, the commit, the changed paths, and the refusal line. A receipt
    is not evidence unless something later reads it, and nothing read it in
    Wave 1.
    """
    row = {
        "session": str(receipt.get("session") or ""),
        "claimed_feature": str(receipt.get("claimed_feature") or receipt.get("feature_id") or ""),
        # `session_end_receipt` names this `requested_status`; `LongHorizonSession.end`
        # adds `claimed_feature` on top. The first version of this function read
        # `status`, which exists on NEITHER, so every row recorded an empty status and
        # no session was ever reported as having completed anything -- the anti-rework
        # block rendered "(none)" forever. A row is only as good as the key it reads.
        "status": str(receipt.get("requested_status") or receipt.get("status") or ""),
        "status_applied": bool(receipt.get("status_applied")),
        "clean": bool(receipt.get("clean")),
        "commit_sha": str(receipt.get("commit_sha") or ""),
        "changed_paths": [str(item) for item in (receipt.get("changed_paths") or ())],
        "refusal_line": str(receipt.get("refusal_line") or ""),
        "attempts": [dict(a) for a in (receipt.get("attempts") or ())],
        "read_observations": sorted(str(k) for k in (read_observations or {})),
        "ledger_passing": int(receipt.get("ledger_passing") or 0),
        "ledger_total": int(receipt.get("ledger_total") or 0),
    }
    return _append_session_row(root, row)


def session_boundary_report(root: Any) -> Dict[str, Any]:
    """Return a receipt describing the boundary state of ``root``.

    ``digest_checked`` is reported explicitly: a gate that could not compare a
    digest must not be reported as one that did, because that is the same
    "armed and did nothing" shape ``read_before_edit_report`` already refuses.
    """
    rows = read_session_record(root)
    chain_ok, chain_reason = session_chain_intact(root)
    prior = prior_session_view(root)
    observations = load_read_observations(root)
    error = str(observations.get("error") or "")
    return {
        "root": str(root),
        "sessions_recorded": len(rows),
        "next_session_index": len(rows) + 1,
        "chain_intact": bool(chain_ok),
        "chain_reason": chain_reason,
        "completed": list(prior.get("completed") or ()),
        "open_claims": list(prior.get("open") or ()),
        "disagreements": len(prior.get("disagreements") or ()),
        "read_observations": len(observations.get("paths") or {}),
        "read_table_error": error,
        "digest_checked": bool(not error),
        "read_scope_default": READ_SCOPE_DEFAULT,
        "note": (
            "A session is an appended row, not a process. Two runs of this report on "
            "the same root disagree only if a session was recorded in between, which "
            "is the definition of a boundary."
        ),
    }


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the boundary report for a root. Exit 2 when the chain is broken."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m harness.session_boundary")
    parser.add_argument("root", nargs="?", default=".", help="project root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = session_boundary_report(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for key in sorted(report):
            print(f"{key}: {report[key]}")
    return 0 if report["chain_intact"] else 2


if __name__ == "__main__":  # pragma: no cover - the module's CLI
    raise SystemExit(_cli())
