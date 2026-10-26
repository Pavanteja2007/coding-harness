"""File state across a fork, a compaction, and a head move (P3a W1, T2).

T3 is making session forking cheap: a new leaf, no copying. That is a
correctness win for the conversation and a potential correctness disaster for
the filesystem, because **a fork that shares a working tree is two agents
writing one directory.** This module is the layer that owns file state, and it
answers three questions the conversation layer cannot:

1. **What does a session fork do to the working tree?** (:func:`fork_file_state`)
2. **Can two live sessions share a writable tree?** (:func:`claim_tree`) -- and
   the answer is asserted, not documented.
3. **Is a merge-back explicit, and does it respect the concurrent-edit
   refusal?** (:func:`merge_back`)

**The defect this exists to close, measured on this tree.** The multi-instance
guard in :mod:`shared.instance_guard` is a **PROCESS** guard, and its
re-entrancy is per ``(process, thread, lock path)``::

    # shared/instance_guard.py:789-810 -- a second acquire of the SAME path in
    # the SAME process/thread is `held_reentrant` and is ADMITTED.

So two sessions inside one ``neo`` process, both pointed at one working tree,
are **refused by nothing**. That is the exact data-loss shape this phase could
introduce, and it is invisible to the existing guard because the guard was
never asked the session question. :func:`claim_tree` is that second caller: the
same lock directory, the same record discipline, the same liveness authority,
one added dimension.

**One authority per concern, and what is DELEGATED.** Every primitive this
module could have re-implemented is imported from
:mod:`shared.instance_guard` instead: the canonical path key, the lock home and
directory name, the record schema version, process liveness, and the refusal
vocabulary. A second liveness check would be a second answer to "is that
process still running", and this repository has already paid for that class of
bug four times. What is added is *one additive key* (``kind``) and *one
dimension* (``session_id``), both of which are the documented additive-key
shape: consumers treat unknown keys as ignorable.

**The fork policy, decided and justified.** The three candidates, and what each
one costs:

============================  =========  ==================================
policy                        isolation  cost
============================  =========  ==================================
share the tree                **none**   **two agents writing one directory
                                        = silent data loss**; rejected
fork gets its own snapshot    full       storage per fork
fork is read-only until        partial   friction on every first write
adopted
============================  =========  ==================================

:data:`FORK_POLICY_OWN_SNAPSHOT` is the default because it is the only one of
the three that is *correct* rather than merely cheap. The storage cost is
linear and bounded (see :mod:`execution.storage_cost`), and a fork that cannot
write is a fork that cannot be evaluated. :data:`FORK_POLICY_READ_ONLY` is
available for an operator who wants the storage back and will pay the friction.
:data:`FORK_POLICY_SHARE` is *recorded* so a config asking for it is refused by
name rather than silently upgraded -- refusing is the direction
``phases/DOCTRINE.md`` §5 requires, and silently giving a caller a different
policy than it asked for is a receipt that lies.

**What this module does NOT do, and why.** It does not move a git branch, and it
does not know what a "session tree" is -- T3 owns both. It is handed a working
tree path per session and answers file-state questions about it.

**It has no production call site yet.** The wiring is a filed
cross-terminal request (see `execution/AGENTS.md`), and that sentence is the
honest state rather than a caveat about a finished thing. A dead guard is
worse than an absent one, so the state is asserted by a pin rather than left
for a reader to infer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from shared.instance_guard import (
    DEFAULT_STALE_S,
    DEFAULT_WAIT_S,
    INSTANCE_LOCK_SCHEMA,
    INSTANCE_STATES,
    LOCK_DIR_NAME,
    ConcurrentInstanceError,
    InstanceInfo,
    canonical_repository_key,
    describe_instance,
    pid_alive,
)

__all__ = [
    "CLAIM_KIND_SESSION_TREE",
    "CLAIM_STATES",
    "DEFAULT_FORK_POLICY",
    "FORKS_NEED_A_NAMED_TREE",
    "FORK_FILE_POLICIES",
    "FORK_POLICY_KEY",
    "FORK_POLICY_OWN_SNAPSHOT",
    "FORK_POLICY_READ_ONLY",
    "FORK_POLICY_SHARE",
    "ISOLATIONS",
    "MERGE_REFUSALS",
    "RECOVERY_LEDGER_NAME",
    "RECOVERY_REASONS",
    "MergeBackReceipt",
    "SessionTreeConflict",
    "TreeClaim",
    "TreeClaimInfo",
    "claim_tree",
    "concurrent_edit_refusal",
    "describe_claim",
    "fork_file_state",
    "merge_back",
    "probe_tree",
    "read_recovery_ledger",
    "recovery_reason_for",
    "recovery_report",
    "release_tree",
    "resolve_fork_policy",
    "session_tree_lock",
]

# ---------------------------------------------------------------------------
# The fork file-state policy
# ---------------------------------------------------------------------------

#: A fork gets its OWN snapshot. Full isolation; the cost is storage per fork.
#: This is the default because it is the only offered policy that cannot lose
#: another session's writes.
FORK_POLICY_OWN_SNAPSHOT = "own_snapshot"

#: A fork is read-only until it is explicitly adopted. Partial isolation (it can
#: read, plan and diff) at the cost of friction on the first write. Offered for
#: an operator who wants the storage back; the friction is deliberate, because a
#: silent downgrade to read-only would be a policy nobody asked for.
FORK_POLICY_READ_ONLY = "read_only_until_adopted"

#: Share the parent's tree. **REFUSED.** Kept in the vocabulary so a config
#: asking for it is rejected *by name* and recorded, rather than being silently
#: upgraded to a different policy -- a receipt that reports a policy the caller
#: did not get is a lie with a timestamp.
FORK_POLICY_SHARE = "share"

#: Every name this module understands, including the refused one. A consumer
#: can reject an unknown value rather than defaulting to the safe policy, which
#: would turn a typo into a quiet behaviour change.
FORK_POLICY_SHARE_REFUSAL = (
    "a fork may not share its parent's working tree: two live sessions writing "
    "one directory is silent data loss, which is the failure this policy "
    "exists to prevent"
)

#: The enforceable policies, as a CLOSED set. Order is the resolution order for
#: nothing -- it is a vocabulary, and a consumer that wants certainty iterates it.
FORK_FILE_POLICIES: Tuple[str, ...] = (FORK_POLICY_OWN_SNAPSHOT, FORK_POLICY_READ_ONLY)

#: Policies this module will refuse, and why, by name.
REFUSED_FORK_POLICIES: Dict[str, str] = {
    FORK_POLICY_SHARE: FORK_POLICY_SHARE_REFUSAL,
}

#: The default when a caller states no policy. A missing key means unchanged
#: behaviour, so this is the policy that was already true: each run got its own
#: ``pristine``/``work`` pair.
DEFAULT_FORK_POLICY = FORK_POLICY_OWN_SNAPSHOT

#: Config key. Opt-in by PRESENCE, like every other key in this module -- a value
#: in ``harness/config.py::DEFAULTS`` is merged into every task and every eval
#: arm at once, and "absent" has to keep meaning "unchanged".
FORK_POLICY_KEY = "fork_file_policy"

#: The sentence :func:`fork_file_state` records when it cannot name a tree the
#: fork owns. **A writable fork with no tree is the share-the-tree shape wearing
#: a receipt**, and it is the shape this module exists to refuse: ``Path("")``
#: resolves against the PROCESS CWD, so a caller that writes through an empty
#: ``child_tree`` writes into whatever directory the process happens to be
#: standing in -- which is very often the parent's tree.
FORKS_NEED_A_NAMED_TREE = (
    "a writable fork must name a tree of its own: with no child tree the fork "
    "would write through an empty path, which resolves against the process "
    "working directory rather than a tree of its own"
)

#: What isolation each enforceable policy actually delivers. This is the claim
#: the receipt makes, and it is derived from the policy rather than declared by
#: the caller, because a caller that supplied its own isolation figure would be
#: describing the intent rather than reporting the state.
ISOLATIONS: Dict[str, str] = {
    FORK_POLICY_OWN_SNAPSHOT: "full",
    FORK_POLICY_READ_ONLY: "partial",
}

#: What each policy costs. Reported next to the isolation so a reader never sees
#: "full isolation" without also seeing what it was bought with.
ISOLATION_COSTS: Dict[str, str] = {
    FORK_POLICY_OWN_SNAPSHOT: "storage per fork; no copy when the shared store "
    "already holds the content",
    FORK_POLICY_READ_ONLY: "friction on the fork's first write until it is "
    "adopted; no additional storage",
}


def resolve_fork_policy(config: Optional[Mapping[str, Any]] = None) -> str:
    """Resolve the fork file-state policy from a task config.

    **Key presence, and an unknown value RAISES.** ``{}``, ``None`` and a config
    without :data:`FORK_POLICY_KEY` all resolve to
    :data:`DEFAULT_FORK_POLICY`, because an absent key must leave every existing
    run byte-identical. ``None`` and ``False`` under the key are treated as **no
    opinion** and take the default too -- that is the convention every other
    opt-in key in this tree follows, and a ``None``-valued discoverability entry
    has to stay behaviour-neutral.

    A config that NAMES a policy this module does not implement raises
    :class:`FileStatePolicyError` rather than falling back: a typo silently
    becoming "give the fork its own tree" is a behaviour change nobody chose, and
    a typo silently becoming "share the tree" is the data loss this module
    exists to prevent. :data:`FORK_POLICY_SHARE` raises too, carrying the
    refusal sentence.

    **The default itself is validated on every call**, so an edit that changes
    :data:`DEFAULT_FORK_POLICY` to a refused or unknown name fails at the first
    resolution rather than producing a receipt nobody notices is wrong.
    """
    if not config or FORK_POLICY_KEY not in config:
        return _validated_policy(DEFAULT_FORK_POLICY, "the module default")
    raw = config.get(FORK_POLICY_KEY)
    if raw is None or raw is False:
        return _validated_policy(DEFAULT_FORK_POLICY, f"{FORK_POLICY_KEY}=None")
    return _validated_policy(str(raw).strip(), f"{FORK_POLICY_KEY}={raw!r}")


def _validated_policy(name: str, origin: str) -> str:
    """Return ``name`` if it is an enforceable policy, else RAISE naming ``origin``."""
    if name in REFUSED_FORK_POLICIES:
        raise FileStatePolicyError(
            f"{origin} was refused: {REFUSED_FORK_POLICIES[name]}. "
            f"Choose one of {list(FORK_FILE_POLICIES)}."
        )
    if name not in FORK_FILE_POLICIES:
        raise FileStatePolicyError(
            f"{origin} is not a policy this module implements. "
            f"Known policies: {list(FORK_FILE_POLICIES)}; refused by design: "
            f"{sorted(REFUSED_FORK_POLICIES)}. An unrecognised policy is a "
            "configuration bug, and defaulting it would be a behaviour change "
            "nobody asked for."
        )
    return name


# ---------------------------------------------------------------------------
# The session dimension of the instance guard
# ---------------------------------------------------------------------------

#: The one additive key this module writes into a guard record. A record without
#: it is a PROCESS claim (the existing caller); a record carrying it is a SESSION
#: claim. Unknown keys are ignorable per the shared-dict contract, so a
#: :mod:`shared.instance_guard` reader is unaffected by a session record and a
#: session reader is unaffected by a process record.
CLAIM_KIND_SESSION_TREE = "session_tree"


class SessionTreeConflict(ConcurrentInstanceError):
    """Raised when a second LIVE SESSION already holds a writable tree.

    A subclass of the existing :class:`~shared.instance_guard.
    ConcurrentInstanceError` on purpose: a caller that already handles a refused
    second ``neo`` process handles this without a new except clause, and a
    distinct type is still available when the caller needs to tell the two apart
    ("another program is running" versus "another session of yours is writing
    here").

    The distinction is not cosmetic. The process guard's failure is "close the
    other terminal"; this one's is "your own fork is pointed at the parent's
    tree", and the remedy is completely different.
    """


class FileStatePolicyError(ValueError):
    """A declared file-state policy is unusable (unknown, or refused by design)."""


#: The states a session claim can be in. It is a subset of
#: :data:`shared.instance_guard.INSTANCE_STATES` -- the vocabulary is NOT
#: re-declared, because two spellings of "held by a live peer" is two answers to
#: one question. Re-exported so a consumer need not import ``shared`` to branch on
#: it, and asserted equal to the shared tuple by a pin.
CLAIM_STATES: Tuple[str, ...] = INSTANCE_STATES


#: Directory, under the platform's per-user temporary root, that session-tree
#: claims live in when the caller passes no explicit ``home``.
#:
#: **This is deliberately NOT ``<neo_home>/locks/``, and the change is the whole
#: point of the constant.** :func:`shared.instance_guard.instance_home` resolves
#: from ``NEO_HOME``/``HARNESS_HOME``, which means a guard placed there can be
#: RELOCATED by an environment variable -- and the module's own docstring already
#: names that as the hazard ("moving the guard is how two sessions stop seeing
#: each other"), while its implementation permitted it. Measured on this tree
#: before the fix:
#:
#:     sess-A claimed the tree with NEO_HOME=<dir A>   -> held
#:     sess-B claimed the SAME tree, NEO_HOME=<dir B> -> ADMITTED
#:
#: So the guard home is resolved from a location the *ambient environment* cannot
#: reach: the platform temporary directory, which is per-user on every platform
#: this runs on and is identical for every process belonging to that user no
#: matter what ``NEO_HOME`` says. The claim is still keyed by
#: :func:`canonical_repository_key`, so two different repositories never collide.
SESSION_GUARD_DIRNAME = "neo-session-tree-guards"


def _guard_home(home: Any = None) -> Path:
    """Return the directory session-tree claims live under.

    An **explicitly passed** ``home`` is honoured: it is a caller-supplied
    argument, which is knowledge rather than ambient configuration, and the test
    suite and a caller hosting genuinely separate harnesses both need it.

    With no explicit ``home``, the environment is deliberately NOT consulted.
    That is the fix, not an oversight: a guard whose location an environment
    variable can move is not a guard, and the bypass is measurable rather than
    theoretical (see :data:`SESSION_GUARD_DIRNAME`).
    """
    if home is not None:
        return Path(home)
    return Path(tempfile.gettempdir()) / SESSION_GUARD_DIRNAME


def _session_lock_path(tree: Any, *, home: Any = None) -> Path:
    """Return the claim file path for a session's working tree.

    Same ``<prefix>-<digest>.json`` shape and the same ``locks``-style directory
    the process guard uses, so there is ONE place a guard can live and ONE record
    schema to read. Only the prefix differs (``tree-`` rather than ``repo-``),
    because the two claims are keyed by different subjects: a process claims a
    repository, a session claims the working tree it was handed.

    The home comes from :func:`_guard_home`, which **ignores ``NEO_HOME`` and
    ``HARNESS_HOME``**. That is the fix for a measured bypass: under the historical
    ``instance_home()`` resolution, two processes pointed at one repository but
    given different ``NEO_HOME`` values each looked in a different directory, both
    acquired, and both wrote the same tree. The session dimension is the one that
    makes that silently destructive -- a process guard refusal would at least have
    been a visible failure to start.
    """
    key = canonical_repository_key(tree)
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:20]
    return _guard_home(home) / LOCK_DIR_NAME / f"tree-{digest}.json"


def _read_claim(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read a claim record; return ``(record, problem)``.

    ``problem`` is non-empty when the record exists but cannot be understood --
    an unknown ``schema_version``, a corrupt body, a future shape. Every
    unreadable case is **HELD, never free**: stealing a claim we cannot read is
    the data loss this module exists to prevent.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"the claim record could not be read ({type(exc).__name__})"
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None, "the claim record is not valid JSON"
    if not isinstance(record, dict):
        return None, "the claim record is not a mapping"
    version = record.get("schema_version")
    if version != INSTANCE_LOCK_SCHEMA:
        return None, (
            f"the claim record carries schema_version {version!r} rather than "
            f"{INSTANCE_LOCK_SCHEMA!r}, so it cannot be understood"
        )
    return record, ""


def _classify_claim(
    record: Optional[Mapping[str, Any]],
    problem: str,
    *,
    path: Path,
    now: float,
    stale_s: float,
    our_host: str,
    our_pid: int,
    our_session: str,
) -> TreeClaimInfo:
    """Classify one claim record into a :class:`TreeClaimInfo`.

    The rules are the shared guard's, delegated in substance and re-expressed
    only where the session dimension changes the answer:

    * no record -> ``free``;
    * a record this process/thread already holds **for this session** ->
      ``held_reentrant`` (re-entering one session's own claim must not refuse
      itself, or a helper that opens a session inside a session cannot work);
    * a record held by **another session in the same live process** -> ``held``.
      This is the case the process guard lets through, and it is the whole
      reason this module exists;
    * same host, holder process gone -> ``held_dead_owner`` (takeable);
    * cross-host and the heartbeat is older than the TTL -> ``held_expired``
      (takeable);
    * unreadable -> ``held_unreadable`` (NOT takeable until the TTL, and the
      refusal says which case it was).
    """
    if record is None:
        if problem:
            # An UNREADABLE record is held, never free -- but it is still
            # self-healing: past the takeover budget there is nobody left to
            # protect, and refusing to reclaim garbage would wedge a checkout
            # forever. The refusal NAMES which case it was, so a reader can
            # tell "unreadable and young" from "unreadable and abandoned".
            age: Optional[float] = None
            try:
                age = max(0.0, now - float(path.stat().st_mtime))
            except OSError:
                age = None
            if age is None:
                return TreeClaimInfo(
                    state="held_unreadable",
                    tree="",
                    session_id="",
                    lineage_id="",
                    pid=None,
                    host="",
                    started_at=None,
                    age_s=None,
                    detail=problem,
                    owner_session="",
                    stale_after_s=float(stale_s),
                    guard_home=_guard_home_of(path),
                )
            return TreeClaimInfo(
                state="held_unreadable",
                tree="",
                session_id="",
                lineage_id="",
                pid=None,
                host="",
                started_at=None,
                age_s=age,
                detail=(
                    f"{problem}; it is {age:.0f}s old, "
                    + ("past" if stale_s > 0.0 and age > stale_s else "within")
                    + f" the {stale_s:.0f}s takeover budget"
                ),
                owner_session="",
                stale_after_s=float(stale_s),
                guard_home=_guard_home_of(path),
            )
        return TreeClaimInfo(state="free", guard_home=_guard_home_of(path))
    session_id = str(record.get("session_id", ""))
    pid = _as_int(record.get("pid"))
    host = str(record.get("host", ""))
    started = _as_float(record.get("started_at"))
    age = None if started is None else max(0.0, now - started)
    lineage = str(record.get("lineage_id", ""))
    detail = ""
    if pid is not None and host == our_host and pid == our_pid and session_id == our_session:
        state = "held_reentrant"
        detail = "this process already holds this tree for this session"
    else:
        alive = pid_alive(pid) if pid is not None else None
        if alive is True:
            state = "held"
            detail = (
                f"session {session_id or '?'}"
                f"{' in this same process' if pid == our_pid and host == our_host else ''}"
                " holds this working tree"
            )
        elif alive is False:
            state = "held_dead_owner"
            detail = f"session {session_id or '?'} is no longer running"
        elif age is not None and stale_s > 0.0 and age > stale_s:
            state = "held_expired"
            detail = (
                f"the holder is on host {host or '?'}, where liveness cannot be "
                f"measured, and its heartbeat is {age:.0f}s old (past the "
                f"{stale_s:.0f}s takeover budget)"
            )
        else:
            state = "held"
            detail = (
                f"session {session_id or '?'} holds this working tree and liveness "
                "could not be measured; refusing to guess"
            )
    return TreeClaimInfo(
        state=state,
        tree=str(record.get("tree", "")),
        session_id=session_id,
        lineage_id=lineage,
        pid=pid,
        host=host,
        started_at=started,
        age_s=age,
        detail=detail,
        owner_session=session_id,
        stale_after_s=float(stale_s),
        guard_home=_guard_home_of(path),
    )


def _guard_home_of(path: Path) -> str:
    """The guard home a claim file lives in -- its lock directory's parent.

    Derived from the PATH rather than passed in, so every classification path
    publishes it and none can forget it. It is ``<home>/locks/<file>``, so the
    home is two levels up; a claim file that somehow sits elsewhere yields its
    own parent, which is still the truth about where the guard looked.
    """
    return str(path.parent.parent)


def _as_int(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TreeClaimInfo:
    """Who holds a working tree, as a value.

    ``state`` is a member of :data:`CLAIM_STATES`. ``is_free`` and ``takeable``
    are the two questions a caller branches on, and they are PROPERTIES derived
    from the state rather than fields a caller can set -- a caller that supplied
    its own ``takeable`` would be able to author the permission it needs.

    ``guard_home`` is WHERE this claim's lock file lives. It is published rather
    than assumed because :func:`shared.instance_guard.instance_home` reads
    ``NEO_HOME`` then ``HARNESS_HOME``: two processes with different homes do not
    see each other's claims at all. That is a legitimate configuration (one home
    per project), so it is made VISIBLE rather than treated as a bypass -- and a
    user who is "not being refused" can now read which home the guard is in.
    """

    state: str = "free"
    tree: str = ""
    session_id: str = ""
    lineage_id: str = ""
    pid: Optional[int] = None
    host: str = ""
    started_at: Optional[float] = None
    age_s: Optional[float] = None
    detail: str = ""
    owner_session: str = ""
    stale_after_s: float = DEFAULT_STALE_S
    guard_home: str = ""

    @property
    def is_free(self) -> bool:
        """True when nothing holds this tree."""
        return self.state == "free"

    @property
    def takeable(self) -> bool:
        """True when the claim can be taken over without a refusal.

        An UNREADABLE claim becomes takeable only past the takeover budget --
        never before. Refusing is the safe direction while there might still be
        somebody behind it, and reclaiming is the safe direction once there
        provably is not.
        """
        if self.state in ("free", "held_dead_owner", "held_expired"):
            return True
        if self.state == "held_unreadable":
            return bool(self.age_s is not None and self.age_s > self.stale_after_s)
        return False

    @property
    def same_process_other_session(self) -> bool:
        """True when a LIVE PEER SESSION IN THIS PROCESS holds the tree.

        This is the case the process guard admits as re-entrant, and it is the
        specific hazard this module closes. A receipt naming it is the evidence
        that "two sessions in one process" is detected rather than assumed
        impossible.
        """
        return bool(self.state == "held" and self.owner_session and self.detail)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record for a receipt."""
        return {
            "state": self.state,
            "free": self.is_free,
            "takeable": self.takeable,
            "stale_after_s": self.stale_after_s,
            "tree": self.tree,
            "session_id": self.session_id,
            "lineage_id": self.lineage_id,
            "pid": self.pid,
            "host": self.host,
            "started_at": self.started_at,
            "age_s": self.age_s,
            "detail": self.detail,
            "owner_session": self.owner_session,
            "guard_home": self.guard_home,
        }

    def describe(self) -> List[str]:
        """Render PLAIN lines naming WHO holds the tree and the door out."""
        if self.is_free:
            return ["free: no live session holds this working tree"]
        lines = [
            f"another session holds this working tree: session "
            f"{self.owner_session or '?'}"
        ]
        if self.pid:
            lines.append(f"  process: pid {self.pid} on {self.host or 'an unknown host'}")
        if self.lineage_id:
            lines.append(f"  lineage: {self.lineage_id}")
        if self.age_s is not None:
            lines.append(f"  held for: {_describe_duration(self.age_s)}")
        if self.guard_home:
            lines.append(
                f"  guard:    {self.guard_home} "
                "(two processes with different NEO_HOME values cannot see each other)"
            )
        if self.detail:
            lines.append(f"  {self.detail}")
        if self.state == "held_reentrant":
            lines.append("  this session already holds it; re-entering is allowed")
        elif self.state == "held_dead_owner":
            lines.append("  its session is gone, so the claim can be taken over")
        elif self.state == "held_expired":
            lines.append("  its heartbeat is past the takeover budget")
        elif self.state == "held_unreadable":
            lines.append("  refusing to guess: a claim we cannot read is never stolen")
        else:
            lines.append(
                "  give the fork its own tree (fork_file_policy=own_snapshot), "
                "or end the session that holds it"
            )
        return lines


def _describe_duration(seconds: float) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if value < 90:
        return f"{int(value)}s"
    if value < 5400:
        return f"{int(value / 60)}m"
    return f"{value / 3600.0:.1f}h"


def probe_tree(
    tree: Any,
    *,
    session_id: str = "",
    home: Any = None,
    stale_s: float = DEFAULT_STALE_S,
    now: Optional[float] = None,
) -> TreeClaimInfo:
    """Report who holds a working tree WITHOUT taking, creating or deleting.

    This is the read-only half: a surface can render "your fork is pointed at
    the parent's tree" without a lock file appearing as a side effect of asking.
    ``free`` means nothing holds the tree.
    """
    key = canonical_repository_key(tree)
    if not key:
        return TreeClaimInfo(
            state="unusable_home",
            detail="no working tree path was supplied, so there is no tree to guard",
        )
    path = _session_lock_path(tree, home=home)
    record, problem = _read_claim(path)
    return _classify_claim(
        record,
        problem,
        path=path,
        now=time.time() if now is None else float(now),
        stale_s=float(stale_s),
        our_host=_host_name(),
        our_pid=os.getpid(),
        our_session=str(session_id or ""),
    )


def _host_name() -> str:
    """The name both guard records use to identify this machine.

    :func:`shared.instance_guard._host_name` is private, so it is DELEGATED to
    rather than re-implemented and the result is cached here. Two records that
    disagreed about the host name would each conclude the other was "on another
    host", which is the one input that decides whether a claim is takeable --
    so the value has to come from the same place both times.
    """
    global _HOST_NAME
    if _HOST_NAME is None:
        from shared.instance_guard import _host_name as shared_host

        _HOST_NAME = shared_host()
    return _HOST_NAME


#: Process-lifetime cache of :func:`_host_name`.
_HOST_NAME: Optional[str] = None

#: Re-entrancy depth per claim path, per (process, thread). **This is the
#: difference between admitting a re-entry and releasing it correctly.** The
#: shared process guard keeps exactly this map; without it, an inner release
#: deletes the record the outer holder still believes it owns, and the outer
#: release then reports a failure for a claim that is already gone.
_CLAIM_DEPTH = threading.local()


def _depth_map() -> Dict[str, int]:
    depths = getattr(_CLAIM_DEPTH, "paths", None)
    if depths is None:
        depths = {}
        _CLAIM_DEPTH.paths = depths
    return depths


@dataclass
class TreeClaim:
    """A held session claim on a working tree.

    Use it as a context manager. :meth:`release` is idempotent and never raises,
    because a failed cleanup must not turn a finished run into a crash.

    ``recovered`` is the receipt for a **stale-claim takeover**: a claim this one
    took over from a holder that is provably gone. It is empty for a claim that
    was free or re-entered, and non-empty exactly when this claim overwrote
    somebody's -- so a caller can report "this tree was free" and "this tree's
    previous holder was killed and I took it over" as two different facts.
    """

    path: Path
    token: str
    info: TreeClaimInfo
    session_id: str
    tree: str
    depth: int = 1
    released: bool = False
    recovered: Tuple[Dict[str, Any], ...] = ()

    @property
    def lock_path(self) -> Path:
        """The claim file this object owns."""
        return self.path

    @property
    def reentrant(self) -> bool:
        """True when an outer claim on this path in this thread already held it."""
        return self.depth > 1

    @property
    def took_over_a_stale_claim(self) -> bool:
        """True when this claim displaced a holder that is provably gone.

        A PROPERTY, not a field: a caller that supplied its own answer could
        author the "nothing was recovered" report.
        """
        return bool(self.recovered)

    def refresh(self, *, now: Optional[float] = None) -> bool:
        """Rewrite the heartbeat; ``False`` when we no longer own the claim.

        A long cross-host session must call this. ``False`` means the record is
        gone or is somebody else's; the safe direction is to stop, never to
        re-create it -- re-creating a claim another session now owns is exactly
        the two-writers state.
        """
        if self.released:
            return False
        record, problem = _read_claim(self.path)
        if record is None or problem or str(record.get("token", "")) != self.token:
            return False
        record["heartbeat_at"] = time.time() if now is None else float(now)
        try:
            self._write(record)
        except OSError:
            return False
        return True

    def _write(self, record: Dict[str, Any]) -> None:
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    def release(self) -> bool:
        """Release the claim. Idempotent, never raises.

        **Re-entrancy is depth-counted.** An inner release decrements the depth
        and returns ``False`` -- it does not delete the record, because the outer
        holder still believes it owns the tree. Only the outermost release
        performs the compare-and-delete on the token, so a claim can only ever
        remove ITS OWN record and a stale holder releasing late cannot delete the
        claim a newer session now owns.

        Returns True when THIS call removed the record.
        """
        if self.released:
            return False
        self.released = True
        depths = _depth_map()
        key = str(self.path)
        if depths.get(key, 1) > 1:
            depths[key] = depths.pop(key) - 1
            return False
        depths.pop(key, None)
        record, problem = _read_claim(self.path)
        if record is None or problem:
            with contextlib.suppress(OSError):
                self.path.unlink()
            return False
        if str(record.get("token", "")) != self.token:
            return False
        with contextlib.suppress(OSError):
            self.path.unlink()
        return not self.path.exists()

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible record for a receipt."""
        payload = self.info.as_dict()
        payload.update(
            {
                "lock_path": str(self.path),
                "released": self.released,
                "owner_pid": os.getpid(),
                "claimed_session": self.session_id,
                "took_over_a_stale_claim": self.took_over_a_stale_claim,
                "recovered": [dict(item) for item in self.recovered],
            }
        )
        return payload

    def __enter__(self) -> "TreeClaim":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def claim_tree(
    tree: Any,
    *,
    session_id: str,
    lineage_id: str = "",
    owner: str = "",
    command: str = "",
    wait_s: float = DEFAULT_WAIT_S,
    stale_s: float = DEFAULT_STALE_S,
    home: Any = None,
    now: Optional[float] = None,
) -> TreeClaim:
    """Claim the single-writer right to one working tree FOR ONE SESSION.

    This is the **second caller of the one instance guard**. The first caller
    (:func:`shared.instance_guard.acquire_repository_lock`) claims a
    *repository* for a *process*. This one claims a *working tree* for a
    *session*, and it is the same mechanism: the same lock directory, the same
    record schema, the same ``O_EXCL`` create, the same liveness authority
    (delegated to :func:`shared.instance_guard.pid_alive`), the same
    compare-and-delete on release, and the same fail-closed treatment of an
    unreadable record. Only the subject and one additive key differ.

    **What it adds, and why it is needed.** The process guard is re-entrant per
    ``(process, thread, path)``, so two sessions inside one ``neo`` process
    pointed at one tree are refused by nothing. Re-entrancy here is keyed by
    **session** as well, so a session re-entering its OWN claim is admitted (a
    helper that opens a session inside a session must work) while a DIFFERENT
    live session in the SAME process is refused. That specific case is
    :attr:`TreeClaimInfo.same_process_other_session`.

    ``wait_s`` defaults to zero: a second session that quietly waits is a second
    session the user believes is working. Raises
    :class:`SessionTreeConflict` (a :class:`ConcurrentInstanceError`) naming the
    holder. A dead holder's claim IS taken over; an unreadable one is not.

    ``session_id`` is REQUIRED and must be non-empty. A claim with no session
    cannot answer "which session holds this", which is the only question this
    guard exists to answer.
    """
    key = canonical_repository_key(tree)
    if not key:
        raise SessionTreeConflict(
            "no working tree path was supplied, so there is no tree to guard",
            info=InstanceInfo(
                state="unusable_home",
                detail="no working tree path was supplied, so there is no tree to guard",
            ),
        )
    session = str(session_id or "").strip()
    if not session:
        raise SessionTreeConflict(
            "claim_tree requires a session_id: a claim with no session cannot "
            "answer 'which session holds this tree', which is the only question "
            "this guard exists to answer",
            info=InstanceInfo(
                state="unusable_home",
                detail="a session_id is required and none was supplied",
            ),
        )

    path = _session_lock_path(tree, home=home)
    wait = min(max(0.0, float(wait_s)), 30.0)
    deadline = time.monotonic() + wait
    token = uuid.uuid4().hex
    our_host = _host_name()
    our_pid = os.getpid()
    #: Every stale-claim takeover this acquisition performed, oldest first. It is
    #: a list rather than a single value because a claim can be taken over more
    #: than once in a retry loop when the first takeover is itself raced.
    recovered: List[Dict[str, Any]] = []

    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            record, problem = _read_claim(path)
            info = _classify_claim(
                record,
                problem,
                path=path,
                now=time.time() if now is None else float(now),
                stale_s=float(stale_s),
                our_host=our_host,
                our_pid=our_pid,
                our_session=session,
            )
            if info.state == "held_reentrant":
                depths = _depth_map()
                depths[str(path)] = depths.get(str(path), 1) + 1
                record, _ = _read_claim(path)
                return TreeClaim(
                    path=path,
                    token=str((record or {}).get("token", "")),
                    info=info,
                    session_id=session,
                    tree=key,
                    depth=depths[str(path)],
                    recovered=tuple(recovered),
                )
            if info.takeable:
                if _steal(path):
                    # A takeover is REPORTED, not silent. A lock left behind by a
                    # kill -9 used to vanish with no trace, so an operator with a
                    # wedged checkout had nothing to read and hand-deleting the
                    # file -- which disables the guard entirely -- was the only
                    # remedy available. See recovery_report().
                    recovered.append(
                        _record_recovery(path, info, session=session, tree=key, home=home)
                    )
                continue
            if time.monotonic() >= deadline:
                raise SessionTreeConflict(_refusal_line(info), info=info) from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        except OSError as exc:
            info = InstanceInfo(
                state="unusable_home",
                lock_path=str(path),
                detail=(
                    f"the guard location is unusable ({type(exc).__name__}); "
                    "refusing to run unguarded"
                ),
            )
            raise SessionTreeConflict(_refusal_line(info), info=info) from None

        started = time.time() if now is None else float(now)
        record = {
            "schema_version": INSTANCE_LOCK_SCHEMA,
            "kind": CLAIM_KIND_SESSION_TREE,
            "tree": key,
            "session_id": session,
            "lineage_id": str(lineage_id or "")[:120],
            "pid": our_pid,
            "host": our_host,
            "owner": str(owner or "")[:200],
            "command": str(command or "")[:200],
            "started_at": started,
            "heartbeat_at": started,
            "token": token,
        }
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            with contextlib.suppress(OSError):
                path.unlink()
            info = InstanceInfo(
                state="unusable_home",
                lock_path=str(path),
                detail=f"the claim record could not be written ({type(exc).__name__})",
            )
            raise SessionTreeConflict(_refusal_line(info), info=info) from None

        _depth_map()[str(path)] = 1
        return TreeClaim(
            path=path,
            token=token,
            info=TreeClaimInfo(
                state="held",
                tree=key,
                session_id=session,
                lineage_id=str(lineage_id or ""),
                pid=our_pid,
                host=our_host,
                started_at=started,
                age_s=0.0,
                detail="this session holds the tree",
                owner_session=session,
                guard_home=_guard_home_of(path),
            ),
            session_id=session,
            tree=key,
            recovered=tuple(recovered),
        )


def _steal(path: Path) -> bool:
    """Remove a claim whose owner is provably gone, then report success.

    The caller has already established the record is takeable (a dead same-host
    pid, a heartbeat past the TTL, or an unreadable record past the takeover
    budget). Re-reading the token and deleting against it means two sessions
    racing to take over a crashed claim both attempt the same compare-and-delete;
    at most one unlink succeeds and the winner is the one whose ``O_EXCL`` create
    then lands.
    """
    with contextlib.suppress(OSError):
        path.unlink()
    return not path.exists()


# ---------------------------------------------------------------------------
# Stale-lock RECOVERY, and why it is a receipt
# ---------------------------------------------------------------------------

#: The append-only ledger of claim recoveries, beside the claim files themselves
#: (``<neo_home>/locks/``). Beside them rather than in a run directory because a
#: recovery is about the GUARD, not about any one run, and a log root can be
#: pruned.
RECOVERY_LEDGER_NAME = "recoveries.jsonl"

#: The closed set of reasons a claim was taken over. Each is a FACT about the
#: previous holder, and each one is a different operator response, which is why
#: they are named rather than collapsed into "stale":
#:
#: ``dead_owner``      -- the holder's process is gone on this host. The common
#:                        case: a `kill -9`, a crashed worker, a closed laptop.
#: ``expired_heartbeat`` -- the holder is on another host, where liveness cannot
#:                        be measured, and its heartbeat is past the budget.
#: ``unreadable_past_budget`` -- the claim file could not be understood and is
#:                        older than the takeover budget.
RECOVERY_REASONS: Tuple[str, ...] = (
    "dead_owner",
    "expired_heartbeat",
    "unreadable_past_budget",
)

_RECOVERY_BY_STATE: Dict[str, str] = {
    "held_dead_owner": "dead_owner",
    "held_expired": "expired_heartbeat",
    "held_unreadable": "unreadable_past_budget",
}

RECOVERY_SCHEMA = 1


def _recovery_ledger_path(home: Any = None) -> Path:
    """The recovery ledger's path, in the same directory as the claim files."""
    return _guard_home(home) / LOCK_DIR_NAME / RECOVERY_LEDGER_NAME


def recovery_reason_for(info: Any) -> str:
    """The named recovery reason for a claim state, or ``""`` when not a recovery.

    Exported so a surface can render the operator-facing sentence without
    re-deriving the mapping, and so a pin can assert the vocabulary is closed.
    """
    return _RECOVERY_BY_STATE.get(str(getattr(info, "state", "")), "")


def append_recovery_ledger(
    home: Any = None, record: Optional[Mapping[str, Any]] = None
) -> bool:
    """Append one recovery to the ledger. Returns whether it landed.

    **Never raises.** A ledger that cannot be written must not stop a claim being
    taken over: the alternative is a tree wedged by the ledger, which is the same
    manual-cleanup failure this receipt exists to end. The caller learns the write
    failed from the ``False``.
    """
    payload: Dict[str, Any] = {
        "schema_version": RECOVERY_SCHEMA,
        "recovered_at": time.time(),
    }
    payload.update(dict(record or {}))
    try:
        path = _recovery_ledger_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_recovery_ledger(home: Any = None) -> Tuple[Dict[str, Any], ...]:
    """Read the recovery ledger, newest last. Total: never raises.

    Corrupt lines are SKIPPED rather than failing the read: a torn final line is
    the normal shape of a process death mid-append, and a ledger that refuses to
    read because of one is a ledger nobody reads.
    """
    rows: List[Dict[str, Any]] = []
    try:
        raw = _recovery_ledger_path(home).read_text(encoding="utf-8")
    except OSError:
        return ()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get("schema_version") == RECOVERY_SCHEMA:
            rows.append(value)
    return tuple(rows)


def recovery_report(home: Any = None, *, tree: Any = None) -> Dict[str, Any]:
    """Report every stale-lock recovery this guard performed, as one receipt.

    **This is the answer to "users delete lock files by hand".** A lock left by a
    ``kill -9`` used to be a lock with no trace: the next session took it over
    silently, so an operator watching a wedged checkout had no way to learn that
    a recovery had happened -- and hand-deleting the file is exactly how a guard
    gets disabled. Every takeover is therefore recorded with WHO held the tree,
    WHY they were judged gone, and WHICH session took over.

    ``tree`` narrows the receipt to one working tree; omit it for everything this
    guard home has recovered.
    """
    wanted = canonical_repository_key(tree) if tree else ""
    rows = [
        row
        for row in read_recovery_ledger(home)
        if not wanted or str(row.get("tree", "")) == wanted
    ]
    by_reason: Dict[str, int] = {reason: 0 for reason in RECOVERY_REASONS}
    for row in rows:
        reason = str(row.get("reason", "") or "")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    lines: List[str] = []
    if not rows:
        lines.append("no stale-claim recovery has been performed in this guard home")
    else:
        lines.append(f"{len(rows)} stale-claim recovery/recoveries recorded:")
        for row in rows[-10:]:
            lines.append(
                f"  {(row.get('recovered_at') and time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(row['recovered_at']))) or '?'}"
                f"  took over from session {row.get('previous_session') or '?'}"
                f" (pid {row.get('previous_pid')}, host {row.get('previous_host') or '?'})"
                f" as {row.get('taking_session') or '?'}"
                f" because {row.get('reason')}"
            )
        if len(rows) > 10:
            lines.append(f"  ... and {len(rows) - 10} earlier recovery/recoveries")
    return {
        "schema_version": RECOVERY_SCHEMA,
        "ledger": str(_recovery_ledger_path(home)),
        "recoveries": len(rows),
        "by_reason": by_reason,
        "reasons": list(RECOVERY_REASONS),
        "tree": wanted,
        "lines": lines,
        "last": dict(rows[-1]) if rows else {},
    }


def _record_recovery(
    path: Path,
    info: Any,
    *,
    session: str,
    tree: str,
    home: Any,
) -> Dict[str, Any]:
    """Record one takeover and return the record, for the claim to carry."""
    reason = recovery_reason_for(info)
    record: Dict[str, Any] = {
        "schema_version": RECOVERY_SCHEMA,
        "recovered_at": time.time(),
        "reason": reason or "unknown",
        "previous_state": str(getattr(info, "state", "") or ""),
        "previous_session": str(getattr(info, "owner_session", "") or ""),
        "previous_pid": getattr(info, "pid", None),
        "previous_host": str(getattr(info, "host", "") or ""),
        "previous_started_at": getattr(info, "started_at", None),
        "held_for_s": getattr(info, "age_s", None),
        "taking_session": session,
        "taking_pid": os.getpid(),
        "tree": tree,
        "lock_path": str(path),
        "guard_home": str(_guard_home(home)),
    }
    record["ledger_written"] = append_recovery_ledger(home, record)
    return record


def _refusal_line(info: Any) -> str:
    """A single PLAIN line naming the refusal, from either info type."""
    if isinstance(info, TreeClaimInfo):
        lines = info.describe()
        return lines[0] if lines else "another session holds this working tree"
    lines = describe_instance(info)
    return lines[0] if lines else "another instance holds this tree"


def release_tree(claim: TreeClaim) -> bool:
    """Release a session claim. A named form of :meth:`TreeClaim.release`.

    It exists so a caller holding a claim in a data structure can release it
    without reaching for the method, and so the release path is one name a pin
    can assert on.
    """
    if claim is None:
        return False
    return claim.release()


def session_tree_lock(tree: Any, **kwargs: Any):
    """Return a context manager holding the session claim for a block.

    The keywords are exactly :func:`claim_tree`'s. A guard that is acquired and
    forgotten is not a guard, so this is the shape a caller almost always wants.
    """

    class _SessionTreeLock:
        def __init__(self) -> None:
            self._claim: Optional[TreeClaim] = None

        def __enter__(self) -> TreeClaim:
            self._claim = claim_tree(tree, **kwargs)
            return self._claim

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            if self._claim is not None:
                self._claim.release()
            self._claim = None

    return _SessionTreeLock()


def describe_claim(info: TreeClaimInfo) -> List[str]:
    """Render a claim as PLAIN lines. The named form of ``info.describe()``."""
    if info is None:
        return ["free: no live session holds this working tree"]
    return info.describe()


def claim_report(tree: Any, *, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
    """Return the read-only receipt a surface renders to a user.

    ``free`` means nothing holds the tree. Every ``held*`` state means a second
    writer would be refused, and the receipt says WHICH case it was so the user
    is not told "busy" when the truth is "that session is gone".
    """
    info = probe_tree(tree, session_id=session_id, **kwargs)
    payload = info.as_dict()
    payload["refuse"] = bool(
        not info.is_free and not info.takeable and info.state != "held_reentrant"
    )
    payload["lines"] = describe_claim(info)
    payload["schema_version"] = INSTANCE_LOCK_SCHEMA
    payload["kind"] = CLAIM_KIND_SESSION_TREE
    return payload


# ---------------------------------------------------------------------------
# Fork file state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForkFileState:
    """What a fork did to the working tree, as a receipt.

    Every field is DERIVED from the decision plus the filesystem, never supplied
    by the caller:

    * ``policy`` -- the resolved policy;
    * ``isolation`` -- ``full`` / ``partial``, read from :data:`ISOLATIONS`;
    * ``writable`` -- whether the child may write RIGHT NOW, which is ``True``
      only for a full-isolation fork that NAMES a tree of its own which EXISTS
      and is not the parent's;
    * ``writable_reason`` -- WHY, and it is **always** present, because
      ``writable: false`` with no reason is a value nobody can act on;
    * ``child_tree`` / ``snapshot_dir`` / ``snapshot_present``;
    * ``shared_with_parent`` -- always ``False``, and asserted rather than
      computed, because this is the property the whole policy exists to keep;
    * ``display`` -- the lines a surface must show, so "you now have your own
      tree" is not something a caller has to remember to say.

    ``snapshot_present`` is deliberately a boolean WITH a companion reason. A
    boolean alone renders as "no snapshot" and reads as a measurement; see
    :data:`SNAPSHOT_ABSENT_REASON_KEY` and :meth:`to_dict`.
    """

    policy: str
    isolation: str
    writable: bool
    child_tree: str
    parent_tree: str
    snapshot_dir: str = ""
    snapshot_present: bool = False
    snapshot_reason: str = ""
    writable_reason: str = ""
    shared_with_parent: bool = False
    display: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt.

        ``writable`` and ``writable_reason`` are ALWAYS both present, for the
        same reason the snapshot pair is: a consumer that has to handle the
        absence of a reason is a consumer that will eventually treat the absence
        as "there is no reason", which is the confusion the pair exists to
        prevent.
        """
        payload = {
            "policy": self.policy,
            "isolation": self.isolation,
            "writable": self.writable,
            "writable_reason": self.writable_reason,
            "child_tree": self.child_tree,
            "parent_tree": self.parent_tree,
            "shared_with_parent": self.shared_with_parent,
            "notes": list(self.notes),
            "display": list(self.display),
            # The snapshot pair is ALWAYS present. A consumer that has to
            # handle its absence is a consumer that will eventually treat the
            # absence as "there is no snapshot", which is the confusion the
            # available+reason pair exists to prevent.
            "snapshot": {
                "available": bool(self.snapshot_present),
                "reason": self.snapshot_reason,
                "path": self.snapshot_dir,
            },
        }
        return payload


#: The key a consumer reads to distinguish "measured, absent" from "not measured".
SNAPSHOT_ABSENT_REASON_KEY = "reason"


def fork_file_state(
    parent_tree: Any,
    child_tree: Any,
    *,
    config: Optional[Mapping[str, Any]] = None,
    policy: Optional[str] = None,
    make_snapshot: Optional[Any] = None,
) -> ForkFileState:
    """Decide and APPLY what a session fork does to the working tree.

    ``policy`` overrides ``config``; with neither, the default is
    :data:`DEFAULT_FORK_POLICY`. **Both doors validate.** An unknown or refused
    policy RAISES :class:`FileStatePolicyError` whichever door it came through
    -- see :func:`resolve_fork_policy` for why that is a raise and not a default,
    and see the module's ``W2/2026-10-04`` note for what happened when the
    ``policy=`` keyword skipped that call and reached an untyped ``KeyError``
    instead of the promised refusal.

    **The enforcement, which is the point, and it has two halves.**

    1. **The child is never the parent.** Under the default policy a ``child_tree``
       equal to the parent's refuses, whatever the caller passed.
    2. **A WRITABLE fork must NAME a tree of its own that exists.** This half was
       missing and it is the load-bearing one: ``fork_file_state(parent, None)``
       used to return ``writable=True`` with ``isolation='full'``,
       ``shared_with_parent=False`` and an EMPTY ``child_tree`` -- a receipt
       claiming full isolation over no tree at all. ``Path("")`` resolves against
       the PROCESS working directory, so the fork's first write landed in whatever
       directory the process was standing in, which is very often the parent's
       tree. That is the share-the-tree shape wearing a receipt. The child the
       snapshot primitive creates is now PUBLISHED as ``child_tree``, and a fork
       that cannot name an existing tree of its own is ``writable=False`` with
       :data:`FORKS_NEED_A_NAMED_TREE` as the reason.

    ``make_snapshot(tree) -> str`` is the caller's snapshot primitive -- in
    production :func:`execution.snapshot.create_run_snapshot`. It is injected
    rather than imported so this module keeps zero coupling to the copier and a
    caller can hand it a worktree creation instead. **A failure to snapshot is
    recorded as ``available: false`` plus the reason, and the fork is NOT
    writable** -- degrading to a shared tree would be the data loss.
    """
    resolved = (
        _validated_policy(str(policy), "the policy= keyword")
        if policy
        else resolve_fork_policy(config)
    )
    parent_key = canonical_repository_key(parent_tree)
    child_key = canonical_repository_key(child_tree)
    notes: List[str] = []

    if parent_key and child_key and child_key == parent_key:
        if resolved == FORK_POLICY_OWN_SNAPSHOT:
            raise FileStatePolicyError(
                f"refused to give a fork its parent's working tree ({parent_key}): "
                f"{FORK_POLICY_SHARE_REFUSAL}. Under {FORK_POLICY_KEY}="
                f"{FORK_POLICY_OWN_SNAPSHOT!r} the child needs its own tree, so "
                "pass a child path distinct from the parent's (or pass "
                "make_snapshot and let this function choose the location)."
            )
        notes.append(
            "the child is pointed at the parent's tree; it is READ-ONLY until it "
            "is adopted, which is what makes this safe rather than data loss"
        )

    snapshot_dir = ""
    snapshot_present = False
    snapshot_reason = ""
    if resolved == FORK_POLICY_OWN_SNAPSHOT and make_snapshot is not None:
        try:
            produced = make_snapshot(child_tree or None)
        except Exception as exc:  # a snapshot fault must not become a share
            snapshot_reason = (
                f"the fork's own snapshot could not be created "
                f"({type(exc).__name__}: {exc}); the fork is NOT writable rather "
                "than sharing the parent's tree"
            )
            notes.append(snapshot_reason)
        else:
            snapshot_dir = str(produced or "")
            if snapshot_dir:
                snapshot_present = Path(snapshot_dir).is_dir()
                snapshot_reason = (
                    ""
                    if snapshot_present
                    else (
                        f"the snapshot primitive reported {snapshot_dir!r} but that "
                        "path is not a directory on disk"
                    )
                )
                if snapshot_present:
                    # PUBLISH the tree the primitive chose. Leaving `child_tree`
                    # empty while `snapshot_dir` names the fork's real tree is a
                    # receipt that answers the isolation question in the typed
                    # field with a blank.
                    child_key = canonical_repository_key(snapshot_dir)
            else:
                snapshot_reason = "the snapshot primitive returned no path"

    #: Why this fork is (or is not) writable. ALWAYS present, for the same
    #: reason `snapshot.available` is always paired with `reason`: a bare boolean
    #: that reads as "we measured it and the answer is no".
    writable_reason = ""
    writable = resolved == FORK_POLICY_OWN_SNAPSHOT and (
        not child_key or child_key != parent_key
    )
    if resolved == FORK_POLICY_OWN_SNAPSHOT and make_snapshot is not None:
        writable = bool(snapshot_present) and writable
        if not snapshot_present and not snapshot_reason:
            snapshot_reason = "the fork has no snapshot to write into"
    if resolved != FORK_POLICY_OWN_SNAPSHOT:
        writable_reason = (
            f"the resolved policy is {resolved!r}, whose isolation is "
            f"{ISOLATIONS[resolved]}; a fork only writes its own tree under "
            f"{FORK_POLICY_OWN_SNAPSHOT!r} or after an explicit adoption"
        )
    elif not child_key:
        writable = False
        writable_reason = FORKS_NEED_A_NAMED_TREE
        notes.append(writable_reason)
    elif child_key == parent_key:
        writable = False
        writable_reason = (
            f"the fork's tree IS the parent's tree ({parent_key}): "
            f"{FORK_POLICY_SHARE_REFUSAL}"
        )
    elif not Path(child_key).is_dir():
        writable = False
        writable_reason = (
            f"the fork's tree {child_key} does not exist on disk, so there is "
            "nothing to write into; a writable fork needs a real directory of "
            "its own"
        )
    elif make_snapshot is not None and not snapshot_present:
        writable = False
        writable_reason = (
            f"the fork's own snapshot is not available ({snapshot_reason or 'no reason recorded'}); "
            "it is NOT writable rather than sharing the parent's tree"
        )
    elif child_key and parent_key and child_key != parent_key and Path(child_key).is_dir():
        writable_reason = (
            f"the fork owns {child_key} and it exists, distinct from the parent's "
            f"{parent_key}"
        )

    display = (
        f"fork file state: {resolved} (isolation: {ISOLATIONS[resolved]})",
        f"  parent tree: {parent_key or parent_tree}",
        f"  child tree:  {child_key or child_tree or '(to be created)'}",
        f"  snapshot:    {snapshot_dir or 'none'}",
        f"  writable:    {writable}",
        f"  why:         {writable_reason or '(no reason recorded)'}",
    )
    return ForkFileState(
        policy=resolved,
        isolation=ISOLATIONS[resolved],
        writable=writable,
        child_tree=child_key,
        parent_tree=parent_key,
        snapshot_dir=snapshot_dir,
        snapshot_present=snapshot_present,
        snapshot_reason=snapshot_reason,
        writable_reason=writable_reason,
        shared_with_parent=False,
        display=tuple(display),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Merge-back: explicit, and it respects the concurrent-edit refusal
# ---------------------------------------------------------------------------

#: The closed set of reasons a merge-back can be refused. Every one is a
#: refusal that keeps a user's unaccounted content alive; none of them is
#: "probably fine".
MERGE_REFUSALS: Tuple[str, ...] = (
    "not_explicit",  # the caller did not state the merge was intended
    "no_evidence",  # there is nothing to say WHAT is being merged
    "concurrent_edit",  # the target content is not accounted for by any record
    "target_missing",  # the target path does not exist to merge into
    "claim_conflict",  # another live session holds the target tree
)


@dataclass(frozen=True)
class MergeBackReceipt:
    """Whether a fork's work may be merged back, and why not when it may not.

    ``ok`` is a PROPERTY of ``refusal``, not a field, so a caller cannot
    construct a passing receipt: ``MergeBackReceipt(refusal="concurrent_edit")``
    reports ``ok is False``. A receipt whose verdict a caller can supply is a
    receipt that describes the intent rather than the outcome.

    ``unaccounted`` names the PATHS whose current content no record accounts for.
    Those are the user's edits, and they are never overwritten by this module --
    it reports them and the caller decides, which is the same shape
    :func:`cli.review.revert_paths` already uses.
    """

    target_tree: str
    source_tree: str
    explicit: bool
    refusal: str = ""
    reason: str = ""
    paths: Tuple[str, ...] = ()
    accounted: Tuple[str, ...] = ()
    unaccounted: Tuple[str, ...] = ()
    display: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing refused the merge. Derived, never declared."""
        return not self.refusal

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "ok": self.ok,
            "target_tree": self.target_tree,
            "source_tree": self.source_tree,
            "explicit": self.explicit,
            "paths": list(self.paths),
            "accounted": list(self.accounted),
            "unaccounted": list(self.unaccounted),
            "refusal": self.refusal,
            "reason": self.reason,
            "display": list(self.display),
        }


def concurrent_edit_refusal(
    recorded_digests: Iterable[str],
    current_digest: str,
) -> bool:
    """True when ``current_digest`` is accounted for by NO recorded digest.

    The rule is the one :mod:`cli.review` already enforces for a revert, stated
    once here so the merge path cannot answer it differently: **content that no
    run-accounted record explains is the user's, and unaccounted content is
    refused rather than overwritten.**

    ``recorded_digests`` is anything iterable of digest strings; an absent or
    empty CURRENT digest is NOT a refusal, because there is nothing to compare --
    and the caller that has no current digest must not be handed a permission.
    That asymmetry is deliberate and is pinned: a missing measurement is not a
    passing measurement (``phases/DOCTRINE.md`` §1).
    """
    current = str(current_digest or "").strip()
    if not current:
        return False
    return current not in {str(item or "").strip() for item in recorded_digests}


def merge_back(
    target_tree: Any,
    source_tree: Any,
    *,
    explicit: bool,
    paths: Optional[Iterable[str]] = None,
    accounted: Optional[Mapping[str, Iterable[str]]] = None,
    current: Optional[Mapping[str, str]] = None,
    claim: Optional[TreeClaimInfo] = None,
) -> MergeBackReceipt:
    """Decide whether a fork's work may be merged back. It never merges.

    **Explicitness is a parameter, not a default.** ``explicit=False`` refuses
    with ``not_explicit``: a merge-back that a caller did not state the intent of
    is the failure mode this gate exists to catch, and defaulting it to allowed
    would make the gate a decoration.

    The concurrent-edit refusal is applied per path against
    :func:`concurrent_edit_refusal`, so the answer for a merge-back is the SAME
    answer a revert gives. **This function does not write.** It reports; the
    write belongs to whoever owns the target tree, which is the caller's
    decision to make with the ``unaccounted`` list in hand.

    ``claim`` is an optional :class:`TreeClaimInfo` for the TARGET tree. When it
    says a different live session holds the target, the merge refuses with
    ``claim_conflict``: merging into a tree another session is writing is the
    same two-writers hazard as the fork itself.
    """
    target_key = canonical_repository_key(target_tree)
    source_key = canonical_repository_key(source_tree)
    considered = tuple(str(item) for item in (paths or ()))
    display: List[str] = []

    def _refuse(refusal: str, reason: str) -> MergeBackReceipt:
        display.append(f"merge-back REFUSED ({refusal}): {reason}")
        return MergeBackReceipt(
            target_tree=target_key,
            source_tree=source_key,
            explicit=bool(explicit),
            paths=considered,
            refusal=refusal,
            reason=reason,
            display=tuple(display),
        )

    if not explicit:
        return _refuse(
            "not_explicit",
            "a merge-back must be requested explicitly; an implicit merge of a "
            "fork's work into its parent is not a merge-back, it is a surprise",
        )
    if not considered:
        return _refuse(
            "no_evidence",
            "no paths were named, so there is nothing to say what would be merged; "
            "an empty merge is either a no-op or a whole-tree copy and neither "
            "should be inferred",
        )
    if (claim is not None and not claim.is_free and not claim.takeable
            and claim.state != "held_reentrant"):
        return _refuse(
            "claim_conflict",
            f"session {claim.owner_session or '?'} holds the target tree, so "
            "merging into it is the same two-writers hazard as sharing it: "
            + "; ".join(claim.describe()[:1]),
        )

    digests = dict(accounted or {})
    observed = dict(current or {})
    accounted_paths: List[str] = []
    unaccounted: List[str] = []
    for path in considered:
        allowed = digests.get(path, ())
        digest = observed.get(path, "")
        if concurrent_edit_refusal(allowed, digest):
            unaccounted.append(path)
        else:
            accounted_paths.append(path)

    if unaccounted:
        return MergeBackReceipt(
            target_tree=target_key,
            source_tree=source_key,
            explicit=True,
            paths=considered,
            accounted=tuple(accounted_paths),
            unaccounted=tuple(unaccounted),
            refusal="concurrent_edit",
            reason=(
                "the current content of "
                + ", ".join(repr(item) for item in unaccounted)
                + " is not accounted for by any recorded digest, so it is the "
                "user's unrecorded work and is NOT overwritten. Re-merge from a "
                "fresh read, or merge the remaining "
                f"{len(accounted_paths)} path(s) and leave the rest."
            ),
            display=tuple(
                [
                    *display,
                    f"merge-back REFUSED (concurrent_edit): {len(unaccounted)} of "
                    f"{len(considered)} path(s) carry unaccounted content: "
                    f"{', '.join(unaccounted)}",
                ]
            ),
        )

    display.append(
        f"merge-back CLEARED: {len(accounted_paths)} path(s) accounted for; "
        "nothing has been written"
    )
    return MergeBackReceipt(
        target_tree=target_key,
        source_tree=source_key,
        explicit=True,
        paths=considered,
        accounted=tuple(accounted_paths),
        unaccounted=(),
        display=tuple(display),
    )


def adopt_fork(state: ForkFileState, *, merge: MergeBackReceipt) -> ForkFileState:
    """Promote a read-only fork to writable ONLY on an explicitly cleared merge.

    This is the ``read_only_until_adopted`` policy's whole transition, and it is
    a function of the merge receipt rather than a flag a caller sets: an adopted
    fork whose merge was refused stays read-only. The returned receipt says
    which, so a reader never has to infer whether adoption happened.

    **Adoption is the ONE transition that may make a fork writable without a
    snapshot on disk**, because the policy is "read only until someone adopts
    you" and adoption is a person saying so. It is therefore the one path where
    ``writable=True`` does not imply ``FORKS_NEED_A_NAMED_TREE`` is satisfied, so
    the reason says so explicitly rather than leaving the reader to infer it.
    """
    if state.writable:
        return state
    if not merge.ok:
        notes = (
            *state.notes,
            f"adoption refused: the merge-back was refused "
            f"({merge.refusal or 'unknown'}); the fork stays read-only",
        )
        display = (
            *state.display,
            "  adoption:    REFUSED (the merge-back did not clear) - still read-only",
        )
        return ForkFileState(
            policy=state.policy,
            isolation=state.isolation,
            writable=False,
            child_tree=state.child_tree,
            parent_tree=state.parent_tree,
            snapshot_dir=state.snapshot_dir,
            snapshot_present=state.snapshot_present,
            snapshot_reason=state.snapshot_reason,
            writable_reason=state.writable_reason,
            shared_with_parent=state.shared_with_parent,
            display=display,
            notes=notes,
        )
    notes = (
        *state.notes,
        "adopted: the merge-back cleared explicitly, so the fork is writable",
    )
    writable_reason = (
        f"adopted on an explicitly cleared merge-back; the child tree "
        f"{state.child_tree or '(unnamed)'} is the operator's declaration that "
        "this fork may write here"
    )
    display = (
        *state.display,
        "  adoption:    CLEARED by an explicit merge-back",
        f"  why:         {writable_reason}",
    )
    return ForkFileState(
        policy=state.policy,
        isolation=state.isolation,
        writable=True,
        child_tree=state.child_tree,
        parent_tree=state.parent_tree,
        snapshot_dir=state.snapshot_dir,
        snapshot_present=state.snapshot_present,
        snapshot_reason=state.snapshot_reason,
        writable_reason=writable_reason,
        shared_with_parent=state.shared_with_parent,
        display=display,
        notes=notes,
    )
