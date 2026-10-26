"""Session boundaries: briefings, the boundary marker, and the ledger check.

A session in Phase 4 is a real unit: it starts with a fresh context, reads the
ledgers, does one feature, commits, and ends. **A user must be able to see and
control that rhythm**, which means four things this module provides and no
other surface currently does:

1. a **start briefing** - what the previous session did, what is in progress,
   what this session's feature is;
2. an **end briefing** - what was done, what was verified, what is next, and
   the commit hash;
3. a **visible boundary marker**, so a reader scrolling back can tell where one
   session ended and another began, and WHY the context reset;
4. a **ledger/transcript discrepancy check**, so a session that contradicts the
   ledger it was briefed from is surfaced rather than silently resolved in
   favour of one or the other.

The fifth requirement - starting a new session is a deliberate act that says
what is preserved and what is dropped - is :func:`new_session_contract`.

Every line rendered here is plain text through ``cli.ui.sanitize_text``. Feature
names and summaries reach this module from a Markdown file and from a model,
so this is an injection surface and it is treated as one.

    python -m cli.session_briefing --repo DIR [--session SID]
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cli.feature_ledger import FeatureLedger, load_ledger
from cli.ui import sanitize_text

__all__ = [
    "BOUNDARY_RULE",
    "DISCREPANCY_KINDS",
    "DROPPED_ON_RESET",
    "PRESERVED_ON_RESET",
    "SessionBriefing",
    "SessionDiscrepancy",
    "boundary_marker_lines",
    "discrepancy_lines",
    "end_briefing",
    "end_briefing_lines",
    "ledger_transcript_discrepancy",
    "new_session_contract",
    "reset_git_cache",
    "start_briefing",
    "start_briefing_lines",
]

#: The rule printed at every boundary. A boundary without a stated reason reads
#: as data loss, and a reader who thinks their work vanished stops trusting the
#: session.
BOUNDARY_RULE = (
    "context reset: this session starts with a fresh model context; the "
    "conversation, the feature ledger and the committed tree are what carry "
    "across"
)

#: What a new session PRESERVES. Every entry is a durable artifact, not an
#: in-memory value - a preserve-list naming a context window would be a lie,
#: because the context window is exactly what does not survive.
PRESERVED_ON_RESET: Tuple[Tuple[str, str], ...] = (
    ("the feature ledger", "docs/feature-matrix.md plus any run's own receipts"),
    ("every prior session's transcript", "logs/_conversations/<sid>.json"),
    ("the committed tree and its history", "git; the session's commit hash"),
    ("run logs and receipts", "logs/{task_id}/"),
    ("decisions recorded in memory", "the decision store"),
    ("worktrees created by earlier sessions", "logs/worktrees/ worktrees.json"),
)

#: What a new session DROPS. Stated as concretely as the preserve list, because
#: "fresh context" is exactly the phrase a reader cannot act on.
DROPPED_ON_RESET: Tuple[Tuple[str, str], ...] = (
    ("the model's in-context conversation", "it is re-read from the transcript, not resumed"),
    ("any uncommitted scratch state", "nothing is carried implicitly; commit it first"),
    ("the previous session's open questions list", "it is re-briefed, not inherited silently"),
)

#: The closed vocabulary of a ledger/transcript disagreement. Closed because an
#: open-ended reason string is how "they disagree" quietly becomes one shape.
DISCREPANCY_KINDS: Tuple[str, ...] = (
    "ledger_in_progress_transcript_closed",
    "ledger_closed_transcript_in_progress",
    "ledger_feature_absent",
    "ledger_state_unknown",
    "transcript_missing",
)


def _line(value: Any) -> str:
    """Return one display line through the ONE sanitise entry point."""
    return sanitize_text(value)


#: Per-process git answer memo, keyed by (cwd, argv). A briefing asks git four
#: questions and a full eleven-surface render pass renders several briefings, so
#: an unmemoised loop spends its budget re-asking the same question. Reset by
#: :func:`reset_git_cache`; a long-lived surface calls it before re-rendering.
_GIT_CACHE: Dict[Tuple[str, Tuple[str, ...]], str] = {}


def reset_git_cache() -> None:
    """Drop every memoised git answer so the next render sees the tree now."""
    _GIT_CACHE.clear()


def _git(repo: Path, *args: str) -> str:
    """Return one git answer, or ``""`` when git cannot be asked.

    Never raises. A repository without git is an unreadable fact, not a reason
    for a briefing to fail.

    ``.rstrip()``, never ``.strip()``: in ``git status --porcelain`` the X
    column is the FIRST character of the FIRST row, and a leading strip deletes
    it. The first row's status then reads as " M" (worktree-modified) whatever
    it actually was. Measured on this tree: ``.strip()`` reported a staged
    modification as unstaged and ``line[3:]`` then chopped the leading "." off
    the reported path.
    """
    key = (str(repo or "."), args)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo or "."),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _GIT_CACHE[key] = ""
        return ""
    if completed.returncode != 0:
        _GIT_CACHE[key] = ""
        return ""
    answer = (completed.stdout or "").rstrip()
    _GIT_CACHE[key] = answer
    return answer


def _is_top_level_repo(repo: Any) -> bool:
    """Return whether a path is ITSELF a git work tree's top level.

    **This guard is load-bearing and the reason is measured, not theoretical.**
    ``git status --porcelain`` from a directory that merely sits INSIDE some
    larger repository makes git walk that repository. On this host, from a
    pytest temp directory, that walk covered the whole user temp tree and took
    the run past its timeout - and the surface that asked was a *briefing*,
    which has no business walking anything. A subdirectory is not a tree.
    """
    toplevel = _git(Path(repo or "."), "rev-parse", "--show-toplevel")
    if not toplevel:
        return False
    try:
        return Path(repo or ".").resolve() == Path(toplevel.strip()).resolve()
    except OSError:
        return False


def _git_commit(repo: Any) -> str:
    """Return the repository HEAD, or ``""``."""
    if not _is_top_level_repo(repo):
        return ""
    return _git(Path(repo or "."), "rev-parse", "HEAD")


def _git_dirty(repo: Any) -> List[str]:
    """Return the paths with uncommitted work, or ``[]``."""
    if not _is_top_level_repo(repo):
        return []
    raw = _git(Path(repo or "."), "status", "--porcelain", "--untracked-files=all")
    if not raw:
        return []
    files: List[str] = []
    for line in raw.splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry:
            files.append(entry.replace("\\", "/"))
    return files


# ---------------------------------------------------------------------------
# Session summaries, read from the durable transcript
# ---------------------------------------------------------------------------


def _turn_text(turn: Any) -> str:
    """Return one turn's text, tolerating every shape a transcript can hold."""
    if isinstance(turn, Mapping):
        return str(turn.get("text") or turn.get("content") or "")
    return str(turn or "")


def _turn_meta(turn: Any) -> Mapping[str, Any]:
    """Return one turn's metadata mapping, or an empty one."""
    if isinstance(turn, Mapping):
        meta = turn.get("metadata")
        if isinstance(meta, Mapping):
            return meta
    return {}


def _feature_claims(turns: Sequence[Any]) -> Dict[str, Any]:
    """Read a transcript's own feature claims out of its turn metadata.

    A session that declares a feature does so in turn metadata, which is
    durable. Reading it back is what lets the discrepancy check compare the
    ledger against the TRANSCRIPT rather than against a paraphrase of it.
    """
    claimed: str = ""
    closed = False
    for turn in turns or ():
        meta = _turn_meta(turn)
        name = str(meta.get("feature") or meta.get("feature_name") or "")
        if name:
            claimed = name
        if meta.get("feature_closed") or meta.get("session_feature_done"):
            closed = True
    return {"feature": claimed, "feature_closed": closed, "claims_present": bool(claimed)}


def session_claims(session: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the feature claims a session's transcript carries."""
    return _feature_claims(list((session or {}).get("turns") or ()))


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------


@dataclass
class SessionBriefing:
    """One session's start (or end) briefing, with everything it read."""

    session_id: str = ""
    repo: str = ""
    started_at: str = ""
    turn_count: int = 0
    previous_session_id: str = ""
    previous_turn_count: int = 0
    previous_feature: str = ""
    previous_commit: str = ""
    in_progress: Tuple[str, ...] = ()
    feature: str = ""
    feature_state: str = ""
    feature_verified: str = ""
    commit: str = ""
    uncommitted: Tuple[str, ...] = ()
    verified: str = ""
    next_feature: str = ""
    end_reason: str = ""
    ledger: Optional[FeatureLedger] = None
    warnings: Tuple[str, ...] = ()
    #: Where the ``commit`` field came from. ``session`` means the caller
    #: supplied this session's own hash; ``repository_head`` means the value was
    #: read off the repository and therefore says NOTHING about this session.
    #: P4/W2 added this after measuring an end-briefing that reported the
    #: repository's pre-existing HEAD under a heading reading COMMITTED for a
    #: session that committed nothing.
    commit_source: str = ""
    #: Whether the previous session's own claims could be read back at all.
    previous_readable: bool = False
    #: Ledger/transcript disagreements, for the PREVIOUS session. Wave 1 computed
    #: them for the current session only, and never rendered them on the briefing
    #: at all, so the one surface whose job is "describe where we left off"
    #: described a previous session that contradicted its ledger without saying
    #: so.
    previous_discrepancies: Tuple[SessionDiscrepancy, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection (additive keys only)."""
        return {
            "session_id": self.session_id,
            "repo": self.repo,
            "started_at": self.started_at,
            "turn_count": int(self.turn_count),
            "previous_session_id": self.previous_session_id,
            "previous_turn_count": int(self.previous_turn_count),
            "previous_feature": self.previous_feature,
            "previous_commit": self.previous_commit,
            "previous_readable": self.previous_readable,
            "previous_discrepancies": [
                item.to_dict() for item in self.previous_discrepancies
            ],
            "in_progress": list(self.in_progress),
            "feature": self.feature,
            "feature_state": self.feature_state,
            "feature_verified": self.feature_verified,
            "commit": self.commit,
            "commit_source": self.commit_source,
            "uncommitted": list(self.uncommitted),
            "verified": self.verified,
            "next_feature": self.next_feature,
            "end_reason": self.end_reason,
            "warnings": list(self.warnings),
        }


def _in_progress_rows(ledger: FeatureLedger) -> Tuple[str, ...]:
    """Return every row the ledger does NOT declare done.

    "In progress" is defined as the absence of a pass, which is the honest
    reading: a blocked row is outstanding, and an unverified row is not known
    to be done. Anything else would let a blocked row look finished.
    """
    return tuple(
        feature.name for feature in ledger.features if not feature.passing
    )


def _previous_session(log_root: Any, repo: Any, current: str) -> Dict[str, Any]:
    """Return the most recent OTHER session's summary, or ``{}``.

    ``cli.session.list_conversations`` is the authority for what sessions exist;
    this module does not walk the conversation directory itself, because a
    second reader of the same store is the drift class this repository records
    paying for repeatedly.

    **The row it returns carries NO turns and NO commit, and P4/W2 measured
    that.** ``list_conversations`` is documented as listing snapshots "without
    exposing file contents" and publishes exactly ``session_id``, ``repo``,
    ``updated_ts``, ``turn_count``, ``summary``, ``path`` and ``status``. Wave 1
    read ``previous.get("turns")`` and ``previous.get("commit")`` from it, which
    are keys that can never be there, so the briefing's "WHERE THE LAST SESSION
    LEFT OFF" block rendered "feature: none declared" and "NOT RECORDED" for
    every session on every machine, forever - a receipt that looks like a fact
    about the world and is a fact about a missing dictionary key.

    So the row is used for what it is (WHICH session, its ``path``, and the
    ``turn_count`` it publishes), and the previous session's own claims are read
    back through ``cli.session.load_or_create(..., strict=True)`` - the same
    reader ``cli.session.inspect_session`` itself uses.

    ``inspect_session`` was the first choice and it was WRONG, measured: it is a
    health probe ("describe a session's explicit health without changing any
    files"), so it publishes ``turn_count`` and never ``turns``. The three-session
    run against the real writer answered ``status: ok, turns: 0`` for a session
    the index said had one turn - so reading it here rendered "0 turn(s)" and
    "feature: none declared" for a session that had done work, which is the lie
    this function exists to stop.

    The read is guarded by the row's own ``path`` because ``load_or_create``
    CREATES a session that does not exist, and a read that writes is the defect
    ``cli/AGENTS.md`` already records once against this exact module.
    """
    try:
        from cli import session as cli_session

        rows = list(cli_session.list_conversations(log_root, repo) or ())
    except Exception:
        return {}
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("session_id") or "") != str(current)
    ]
    if not candidates:
        return {}

    def _sort_key(row: Mapping[str, Any]) -> float:
        try:
            return float(row.get("updated_ts") or row.get("started_ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    candidates.sort(key=_sort_key, reverse=True)
    chosen = dict(candidates[0])
    chosen["turns_available"] = False
    chosen["turns"] = []
    path = str(chosen.get("path") or "")
    if not path or not Path(path).is_file():
        chosen["unreadable_reason"] = (
            "the index row names no readable snapshot for this session"
        )
        return chosen
    try:
        loaded = cli_session.load_or_create(
            log_root, chosen.get("repo") or repo, str(chosen.get("session_id") or ""),
            strict=True,
        )
    except Exception as exc:
        chosen["unreadable_reason"] = (
            f"the snapshot could not be read ({type(exc).__name__}: "
            f"{str(exc)[:160]}), so its feature and commit are UNKNOWN"
        )
        return chosen
    turns = list((loaded or {}).get("turns") or ())
    if not turns:
        chosen["unreadable_reason"] = (
            "the snapshot carries no turns, so what this session worked on is "
            "UNKNOWN rather than nothing"
        )
        return chosen
    chosen["turns"] = turns
    chosen["turns_available"] = True
    # The conversation snapshot records a commit only when one was made through
    # the CLI. Absent that the briefing says so, rather than substituting the
    # repository's HEAD - which says nothing about this session.
    for key in ("commit", "last_commit"):
        value = loaded.get(key)
        if value:
            chosen["commit"] = str(value)
            break
    return chosen


def start_briefing(
    session: Mapping[str, Any],
    *,
    log_root: Any = None,
    repo: Any = None,
    feature: str = "",
    ledger: Optional[FeatureLedger] = None,
) -> SessionBriefing:
    """Build the session-START briefing.

    Reads the previous session out of the durable transcript, the feature ledger
    out of the ledger of record, and the commit out of git. Every one of those
    three can be missing, and each missing source produces a NAMED warning
    rather than a silently shortened briefing - a briefing that quietly omits
    "there is no previous session" reads as "there is nothing to know".
    """
    data = dict(session or {})
    repo_path = str(repo or data.get("repo") or ".")
    target = (
        feature
        or str(data.get("feature") or "")
        or str(_feature_claims(list(data.get("turns") or ())).get("feature") or "")
    )
    ledger_obj = ledger if ledger is not None else load_ledger(repo_path, current_feature=target)
    warnings: List[str] = []

    previous = _previous_session(log_root, repo_path, str(data.get("session_id") or ""))
    if not previous:
        warnings.append(
            "no previous session was found for this repository, so there is no "
            "prior work to brief from. That is UNKNOWN, not 'nothing to do'."
        )
    prev_claims = _feature_claims(list(previous.get("turns") or ()))
    previous_commit = str(previous.get("commit") or previous.get("last_commit") or "")
    previous_readable = bool(previous.get("turns_available"))

    # The previous session's DISAGREEMENT with the ledger, computed here so the
    # briefing can render it. The comparison target is the same ledger object
    # this briefing reports states from, so the two can never be about different
    # tables. A previous session that closed a feature the ledger does not carry
    # is the single most useful sentence a start briefing can carry, and Wave 1
    # computed it and threw it away.
    previous_discrepancies: Tuple[SessionDiscrepancy, ...] = ()
    if previous_readable and prev_claims.get("claims_present"):
        previous_discrepancies = tuple(
            ledger_transcript_discrepancy(
                {"turns": list(previous.get("turns") or ())}, ledger_obj
            )
        )
        for item in previous_discrepancies:
            warnings.append(
                f"the PREVIOUS session ({previous.get('session_id')}) disagrees "
                f"with the ledger: {item.kind}"
            )
    elif previous_readable:
        warnings.append(
            "the previous session's transcript records no feature claim, so what "
            "it worked on is UNKNOWN rather than nothing"
        )
    elif previous:
        warnings.append(
            "the previous session's transcript could not be read back, so its "
            "feature, its turn count and its commit are UNKNOWN - not zero"
        )

    row = ledger_obj.by_name(target) if target else None
    if target and row is None:
        warnings.append(
            f"this session declares the feature {target!r}, which matches NO row "
            "in the ledger of record, so its status cannot be reported"
        )
    if not target:
        warnings.append(
            "no feature was declared for this session. UNKNOWN is not the same "
            "as 'nothing is in progress'."
        )
    if not ledger_obj.source_available:
        warnings.append(
            "the feature ledger could not be read, so coverage is UNKNOWN"
        )

    return SessionBriefing(
        session_id=str(data.get("session_id") or ""),
        repo=repo_path,
        started_at=str(data.get("started_ts") or ""),
        turn_count=len(list(data.get("turns") or ())),
        previous_session_id=str(previous.get("session_id") or ""),
        previous_turn_count=len(list(previous.get("turns") or ())),
        previous_feature=str(prev_claims.get("feature") or ""),
        previous_commit=previous_commit,
        previous_readable=previous_readable,
        previous_discrepancies=previous_discrepancies,
        in_progress=_in_progress_rows(ledger_obj),
        feature=str(target or ""),
        feature_state=str(row.state) if row is not None else "",
        feature_verified=(
            f"{len(row.evidence)} reference(s), observable here: "
            f"{row.verification_ran}, recorded run: "
            f"{', '.join(row.recorded_outcomes) if row.run_recorded else 'NONE'}"
            if row is not None
            else ""
        ),
        commit=_git_commit(repo_path),
        commit_source="repository_head" if _is_top_level_repo(repo_path) else "",
        uncommitted=tuple(_git_dirty(repo_path)),
        ledger=ledger_obj,
        warnings=tuple(warnings),
    )


def end_briefing(
    session: Mapping[str, Any],
    *,
    repo: Any = None,
    feature: str = "",
    verified: str = "",
    next_feature: str = "",
    commit: str = "",
    end_reason: str = "",
    ledger: Optional[FeatureLedger] = None,
) -> SessionBriefing:
    """Build the session-END briefing, with the commit hash - and its SOURCE.

    The commit hash is the load-bearing field, and P4/W2 found that it was
    carrying a meaning it could not support. Wave 1 fell back to
    ``_git_commit(repo)`` when the caller supplied no hash and rendered the
    result under a heading reading ``COMMITTED``. The three-session run measured
    what that produces: a session that edited one file and committed nothing
    rendered

        COMMITTED
          commit: 42bc538c554fe04161eb6274777605ee985c90a8

    with the true fact - HEAD unchanged, ``app.py`` uncommitted - one warning
    lower down. The heading is what a reader's eye lands on, so a wrong heading
    outranks a right warning.

    A hash read off the repository is now labelled ``repository_head`` and
    rendered as such: it states the tip of a branch, and says nothing about
    whether this session produced it.
    """
    data = dict(session or {})
    repo_path = str(repo or data.get("repo") or ".")
    target = feature or str(data.get("feature") or "")
    ledger_obj = ledger if ledger is not None else load_ledger(repo_path, current_feature=target)
    supplied = str(commit or "")
    resolved_commit = supplied or _git_commit(repo_path)
    commit_source = "session" if supplied else ("repository_head" if resolved_commit else "none")
    warnings: List[str] = []
    if not supplied:
        if resolved_commit:
            warnings.append(
                "this session did NOT commit. The repository HEAD is "
                f"{resolved_commit[:12]}, which predates or is unrelated to this "
                "session - it is the state of the branch, not this session's work"
            )
        else:
            warnings.append(
                "NO COMMIT: this session ended without a commit hash, so the next "
                "session has no committed tree to build on"
            )
    dirty = tuple(_git_dirty(repo_path))
    if dirty:
        warnings.append(
            f"{len(dirty)} path(s) are uncommitted; work in the tree is NOT "
            "preserved across a session reset unless it is committed"
        )
    if not verified:
        warnings.append(
            "no verification result was supplied, so this session's completion "
            "is UNKNOWN - it is not 'verified'"
        )
    return SessionBriefing(
        session_id=str(data.get("session_id") or ""),
        repo=repo_path,
        turn_count=len(list(data.get("turns") or ())),
        feature=str(target or ""),
        feature_state=(
            str(ledger_obj.by_name(target).state)
            if target and ledger_obj.by_name(target) is not None
            else ""
        ),
        commit=resolved_commit,
        commit_source=commit_source,
        uncommitted=dirty,
        verified=str(verified or ""),
        next_feature=str(next_feature or ""),
        end_reason=str(end_reason or ""),
        ledger=ledger_obj,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# The visible boundary, and the deliberate act of starting a new session
# ---------------------------------------------------------------------------


def new_session_contract(*, reason: str = "") -> Dict[str, Any]:
    """State what a new session preserves and what it drops.

    Returns the contract rather than rendering it, so the caller that performs
    the reset can journal the same value it displayed. A contract that is only
    ever printed is a promise; one that is also journalled is a receipt.
    """
    return {
        "preserved": [{"what": what, "where": where} for what, where in PRESERVED_ON_RESET],
        "dropped": [{"what": what, "note": note} for what, note in DROPPED_ON_RESET],
        "reason": str(reason or "the user asked for a new session"),
        "boundary_rule": BOUNDARY_RULE,
    }


def boundary_marker_lines(
    *,
    session_id: str = "",
    direction: str = "start",
    reason: str = "",
    contract: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """Render the marker a reader sees between two sessions.

    Rendered as a rule so it is visible in a scrolled-back transcript without
    being a heading a renderer might swallow, and the reason is always present:
    a boundary whose reason is absent reads as data loss.
    """
    if direction not in ("start", "end"):
        raise ValueError(f"boundary direction must be 'start' or 'end', got {direction!r}")
    data = contract if isinstance(contract, Mapping) else new_session_contract(reason=reason)
    rule = "-" * 68
    out: List[str] = [rule, f"{str(direction).upper()} OF SESSION {session_id}".strip()]
    out.append(f"  why: {_line(data.get('reason') or BOUNDARY_RULE)}")
    out.append(f"  what this means: {_line(data.get('boundary_rule') or BOUNDARY_RULE)}")
    out.append("  PRESERVED across this boundary:")
    for item in data.get("preserved") or ():
        out.append(f"    - {_line(item.get('what'))} ({_line(item.get('where'))})")
    out.append("  DROPPED at this boundary:")
    for item in data.get("dropped") or ():
        out.append(f"    - {_line(item.get('what'))} ({_line(item.get('note'))})")
    out.append(rule)
    return out


# ---------------------------------------------------------------------------
# Ledger / transcript discrepancies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionDiscrepancy:
    """One place where the ledger and the transcript disagree."""

    kind: str
    detail: str
    ledger_says: str = ""
    transcript_says: str = ""

    def line(self) -> str:
        """Return the plain, sanitised display line for this discrepancy."""
        left = f" ledger={_line(self.ledger_says)}" if self.ledger_says else ""
        right = f" transcript={_line(self.transcript_says)}" if self.transcript_says else ""
        return f"  !! {self.kind}: {_line(self.detail)}{left}{right}"

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "ledger_says": self.ledger_says,
            "transcript_says": self.transcript_says,
        }


def ledger_transcript_discrepancy(
    session: Mapping[str, Any],
    ledger: FeatureLedger,
    *,
    transcript: Optional[Sequence[Any]] = None,
) -> List[SessionDiscrepancy]:
    """Return every place this session's transcript contradicts the ledger.

    **Both sources are reported and neither is silently preferred.** A session
    cannot be resumed into a state that contradicts its ledger, and the way to
    guarantee that is to SAY so rather than pick a winner: a reader who is told
    "the ledger says in progress, the transcript says closed" can resolve it;
    a reader who is handed a silently-chosen answer cannot.

    The function returns a list, which may be EMPTY - and an empty list means
    "checked, no disagreement", not "not checked".
    """
    turns = list(transcript if transcript is not None else (session or {}).get("turns") or ())
    found: List[SessionDiscrepancy] = []
    if not turns:
        return [
            SessionDiscrepancy(
                kind="transcript_missing",
                detail=(
                    "this session has no transcript, so its ledger agreement "
                    "could NOT be checked. An unchecked session is not a "
                    "consistent session."
                ),
            )
        ]
    if not ledger.source_available:
        return [
            SessionDiscrepancy(
                kind="ledger_state_unknown",
                detail=(
                    "the feature ledger could not be read, so no agreement "
                    "between ledger and transcript could be established"
                ),
            )
        ]

    claims = _feature_claims(turns)
    claimed = str(claims.get("feature") or "")
    closed = bool(claims.get("feature_closed"))
    if not claimed:
        return [
            SessionDiscrepancy(
                kind="ledger_in_progress_transcript_closed",
                detail=(
                    "the transcript records no feature for this session, so "
                    "the ledger's view of this session could not be confirmed"
                ),
                ledger_says="the ledger tracks features for this repository",
                transcript_says="no feature claim",
            )
        ]

    row = ledger.by_name(claimed)
    if row is None:
        return [
            SessionDiscrepancy(
                kind="ledger_feature_absent",
                detail=(
                    "the transcript works on a feature that is not a row in the "
                    "ledger of record, so its status cannot be reported"
                ),
                ledger_says="no such row",
                transcript_says=claimed,
            )
        ]

    if closed and not row.passing:
        found.append(
            SessionDiscrepancy(
                kind="ledger_in_progress_transcript_closed",
                detail=(
                    "the transcript says this feature is closed, but the ledger "
                    "does not record it as verified. One of the two is wrong "
                    "and this surface does NOT choose between them."
                ),
                ledger_says=f"{row.state} ({row.declared_status})",
                transcript_says="closed",
            )
        )
    if row.passing and not closed:
        found.append(
            SessionDiscrepancy(
                kind="ledger_closed_transcript_in_progress",
                detail=(
                    "the ledger records this feature as verified, but the "
                    "transcript never closed it. The verification may predate "
                    "this session's work."
                ),
                ledger_says=f"{row.state} ({row.declared_status})",
                transcript_says="not closed",
            )
        )
    return found


def discrepancy_lines(discrepancies: Sequence[Any]) -> List[str]:
    """Render discrepancies, including the explicit all-clear.

    The all-clear line matters: a surface that renders nothing when there is
    nothing to report is indistinguishable from one that did not check.
    """
    if not discrepancies:
        return [
            "  ledger / transcript: checked, and they agree on every feature "
            "this session touches"
        ]
    out: List[str] = ["  ledger / transcript DISAGREEMENT (unresolved, not decided):"]
    for item in discrepancies:
        out.append(item.line() if hasattr(item, "line") else f"  !! {_line(item)}")
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def start_briefing_lines(briefing: SessionBriefing) -> List[str]:
    """Render the session-START briefing."""
    out: List[str] = ["=" * 68, f"SESSION BRIEFING (start) - {briefing.session_id}".strip(), "=" * 68]
    # P4/W2: ``to_dict`` publishes ``turn_count`` and the start briefing never
    # rendered it, so a script reading the JSON and a person reading the text
    # were reading two different documents. The parity test found it by
    # asserting the JSON's own value appears in its own text.
    out.append(
        f"  THIS SESSION: {_line(briefing.session_id or 'unnamed')}   "
        f"turns so far: {briefing.turn_count}"
    )
    out.append("  WHERE THE LAST SESSION LEFT OFF")
    if briefing.previous_session_id:
        out.append(
            f"    session: {_line(briefing.previous_session_id)} "
            f"({briefing.previous_turn_count} turn(s))"
        )
        if not briefing.previous_readable:
            out.append(
                "    previous session: its transcript could NOT be read back, so "
                "its feature and its turn count are UNKNOWN - the 0 above is not "
                "a measurement"
            )
        out.append(
            f"    feature: {_line(briefing.previous_feature or 'none declared')}"
        )
        out.append(
            f"    commit: {_line(briefing.previous_commit or 'NOT RECORDED - no hash to build on')}"
        )
    else:
        out.append(
            "    no previous session was found. This is UNKNOWN, not 'nothing "
            "was done before'."
        )
    if briefing.previous_discrepancies:
        out.append("  !! THE PREVIOUS SESSION DISAGREES WITH THE LEDGER")
        out.append(
            "     it is reported, not resolved: one of the two is wrong and this "
            "surface does NOT choose between them"
        )
        for item in briefing.previous_discrepancies:
            out.append(f"     {_line(item.kind)}: {_line(item.detail)}")
    out.append("  WHAT IS IN PROGRESS")
    if briefing.in_progress:
        for name in briefing.in_progress[:12]:
            out.append(f"    - {_line(name)}")
        if len(briefing.in_progress) > 12:
            out.append(
                f"    ... and {len(briefing.in_progress) - 12} more (see the "
                "feature ledger for the full list)"
            )
    else:
        out.append(
            "    nothing outstanding in the ledger. If that is surprising, run "
            "the feature ledger surface: a ledger that could not be read also "
            "reports nothing outstanding."
        )
    out.append("  THIS SESSION'S FEATURE")
    if briefing.feature:
        out.append(
            f"    {_line(briefing.feature)} [ledger state: "
            f"{_line(briefing.feature_state or 'unknown')}]"
        )
        if briefing.feature_verified:
            out.append(f"    evidence: {_line(briefing.feature_verified)}")
    else:
        out.append(
            "    NO FEATURE DECLARED. Unknown is not the same as 'nothing is in "
            "progress'."
        )
    out.append("  TREE")
    out.append(f"    commit: {_line(briefing.commit or 'NOT A GIT REPOSITORY')}")
    if briefing.commit_source == "repository_head":
        out.append(
            "    that hash is the REPOSITORY HEAD, not this session's work: it "
            "states the tip of the branch, not what this session produced"
        )
    if briefing.uncommitted:
        out.append(
            f"    {len(briefing.uncommitted)} uncommitted path(s): "
            f"{_line(', '.join(briefing.uncommitted[:6]))}"
        )
    else:
        out.append("    no uncommitted paths")
    if briefing.warnings:
        out.append("  WARNINGS")
        for warning in briefing.warnings:
            out.append(f"    ! {_line(warning)}")
    return out


def end_briefing_lines(briefing: SessionBriefing) -> List[str]:
    """Render the session-END briefing, with the commit hash."""
    out: List[str] = ["=" * 68, f"SESSION SUMMARY (end) - {briefing.session_id}".strip(), "=" * 68]
    out.append("  DONE")
    out.append(
        f"    feature: {_line(briefing.feature or 'none declared')} "
        f"[ledger state: {_line(briefing.feature_state or 'unknown')}]"
    )
    out.append(f"    turns: {briefing.turn_count}")
    out.append(
        f"    verified: {_line(briefing.verified or 'NOT REPORTED - this is not a pass')}"
    )
    # The heading is chosen by the SOURCE of the hash, not by whether a hash
    # exists. "COMMITTED" is a claim about this session, and the measured
    # three-session run is what made that distinction load-bearing: a session
    # that changed one byte and committed nothing rendered a full hash under
    # this heading, with the true fact in a warning three lines below.
    if briefing.commit_source == "session":
        out.append("  COMMITTED BY THIS SESSION")
        out.append(f"    commit: {_line(briefing.commit)}")
    elif briefing.commit_source == "repository_head":
        out.append("  COMMITTED BY THIS SESSION: NO - this session did not commit")
        out.append(
            f"    repository HEAD (NOT this session's work): {_line(briefing.commit)}"
        )
    else:
        out.append("  COMMITTED BY THIS SESSION: NO")
        out.append(
            "    no commit hash at all: this session produced nothing the next "
            "session can build on"
        )
    if briefing.uncommitted:
        out.append(
            f"    still uncommitted: {len(briefing.uncommitted)} path(s) - "
            f"{_line(', '.join(briefing.uncommitted[:6]))}"
        )
    out.append("  NEXT")
    out.append(f"    next feature: {_line(briefing.next_feature or 'not chosen yet')}")
    if briefing.end_reason:
        out.append(f"    ended because: {_line(briefing.end_reason)}")
    if briefing.warnings:
        out.append("  WARNINGS")
        for warning in briefing.warnings:
            out.append(f"    ! {_line(warning)}")
    return out


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Print the start briefing, the boundary, and the discrepancy check."""
    args = list(sys.argv[1:] if argv is None else argv)
    repo = "."
    feature = ""
    log_root = "logs"
    session_id = ""
    if "--repo" in args:
        index = args.index("--repo")
        if index + 1 < len(args):
            repo = args[index + 1]
    if "--feature" in args:
        index = args.index("--feature")
        if index + 1 < len(args):
            feature = args[index + 1]
    if "--log-root" in args:
        index = args.index("--log-root")
        if index + 1 < len(args):
            log_root = args[index + 1]
    if "--session" in args:
        index = args.index("--session")
        if index + 1 < len(args):
            session_id = args[index + 1]

    session: Dict[str, Any] = {"session_id": session_id, "repo": repo, "turns": []}
    try:
        from cli import session as cli_session

        loaded = cli_session.inspect_session(log_root, session_id, repo)
        if isinstance(loaded, Mapping):
            session = dict(loaded)
    except Exception as exc:  # an unreadable session is a named condition
        session["briefing_read_error"] = str(exc)

    briefing = start_briefing(session, log_root=log_root, repo=repo, feature=feature)
    for line in boundary_marker_lines(session_id=briefing.session_id, direction="start"):
        print(line)
    for line in start_briefing_lines(briefing):
        print(line)
    ledger = briefing.ledger or load_ledger(repo, current_feature=feature)
    print("  LEDGER / TRANSCRIPT AGREEMENT")
    for line in discrepancy_lines(
        ledger_transcript_discrepancy(session, ledger)
    ):
        print(line)
    if "--json" in args:
        payload = dict(briefing.to_dict())
        payload["discrepancies"] = [
            item.to_dict()
            for item in ledger_transcript_discrepancy(session, ledger)
        ]
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator surface
    raise SystemExit(_cli())
