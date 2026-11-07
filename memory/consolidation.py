"""Memory consolidation, phase 2: a BOUNDED SUB-AGENT REVIEWS A DIFF.

WHAT THIS MODULE IS, AND TWO WORDS IN IT THAT NEED CORRECTING
---------------------------------------------------------------
The corpus design is "a **sandboxed** internal sub-agent reviewing a git-style
workspace diff" over "filesystem artifacts under a **git-baselined** memory
root". This implementation is close to that and is NOT those two things. Both
corrections are stated here rather than left for a reader to discover:

**It is BOUNDED, not SANDBOXED.** The reviewer is an in-process callable. There
is no container, no subprocess, no syscall restriction and no filesystem
isolation -- verified by grepping this module for `sandbox`, `Docker`,
`subprocess`, `container`: none appear. What the reviewer DOES get is a
bounded input (a diff, capped at :data:`MAX_DIFF_CHARS`) and a bounded
effect (it returns a verdict and writes nothing; the caller decides). That is a
real containment property and it is weaker than a sandbox. A reviewer that
raises, returns garbage, or returns an unrecognised verdict ABSTAINS, so the
failure direction is safe -- but "safe on the failure path" and "isolated" are
different claims and only the first is true here.

**It is DIGEST-BASELINED, not GIT-BASELINED.** There is no git anywhere in this
module. The baseline is ``baseline.json``: a mapping of artifact name to the
SHA-256 of its content at the last accepted consolidation. That is what makes
the diff a diff rather than a fresh render, and it is content-addressed, which
is the property that matters. It is not a git repository, and calling it one
would send a reader looking for history, branching or a merge that is not
there.

WHY THE REVIEWER IS INJECTED
----------------------------
``reviewer`` is a callable ``(diff_text, context) -> verdict``. Two reasons, and
the second is the important one:

1. it is testable with no provider;
2. the DEFAULT reviewer is a deterministic RULE reviewer that reports
   ``engine="rules", model_calls=0``, so a consolidation run with no model
   configured never claims a sub-agent reviewed anything.

The verdict vocabulary is CLOSED and fail-closed. ``accept`` is the only one
that writes. ``reject`` and ``abstain`` both write nothing, and they are
different answers: ``reject`` is "this diff is wrong", ``abstain`` is "I cannot
tell", and a system that conflates them will eventually overwrite good memory
with a diff nobody understood.

NO CODE PATH FROM CONSOLIDATION TO THE STORE
--------------------------------------------
Like phase 1, consolidation writes FILES and PROPOSALS. It cannot record a
memory. A consolidation that wanted to persist would have to go through
:class:`memory.inbox.MemoryInbox.apply`, which requires a human. That is the
composition the corpus recommends -- phase 1 and the inbox compose, and phase 2
sits on top of the same boundary.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ConsolidationReceipt",
    "ConsolidationVerdict",
    "MemoryConsolidator",
    "ReviewVerdict",
    "rule_reviewer",
    "unified_diff",
]

#: The artifact filenames in the memory root. Named constants because they are
#: a contract with the corpus design (project memory vs user memory) and with
#: any future reader, and a filename that drifts silently makes two memory roots
#: incomparable.
MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"

#: Cap on the diff handed to the reviewer. A reviewer given a 2 MB diff is not
#: reviewing; and the cap firing is reported rather than applied silently.
MAX_DIFF_CHARS = 60_000

#: Cap on a rendered artifact. Mirrors the injection budget's order of
#: magnitude: an artifact nobody can inject is an artifact nobody benefits from.
MAX_ARTIFACT_CHARS = 16_384


class ReviewVerdict(str, Enum):
    """What the sub-agent decided. Only ``ACCEPT`` writes anything."""

    ACCEPT = "accept"
    REJECT = "reject"
    ABSTAIN = "abstain"

    def __str__(self) -> str:
        """Return the serialized verdict."""
        return self.value


@dataclass(frozen=True)
class ConsolidationVerdict:
    """The reviewer's answer, with the reason it gave."""

    verdict: ReviewVerdict
    reason: str
    engine: str
    model_calls: int
    reviewer: str = ""

    @property
    def accepted(self) -> bool:
        """Only ``ACCEPT`` writes. ``reject`` and ``abstain`` both do not."""
        return self.verdict is ReviewVerdict.ACCEPT

    @property
    def wrote_nothing(self) -> bool:
        """Alias for ``not accepted``, spelled for call sites that read it."""
        return not self.accepted

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible projection."""
        return {
            "verdict": self.verdict.value,
            "accepted": self.accepted,
            "reason": self.reason,
            "engine": self.engine,
            "model_calls": self.model_calls,
            "reviewer": self.reviewer,
        }


@dataclass(frozen=True)
class ConsolidationReceipt:
    """What one consolidation pass did.

    ``diff_chars`` is the size of what the reviewer actually SAW, and
    ``diff_truncated`` says whether it saw all of it. A reviewer that saw a
    truncated diff and said "accept" accepted something the operator cannot
    reconstruct, which is why the two are separate fields.
    """

    ok: bool
    engine: str
    model_calls: int
    diff_chars: int
    diff_truncated: bool
    files_written: Tuple[str, ...]
    files_unchanged: Tuple[str, ...]
    baseline_digest: str
    proposed_digest: str
    verdict: Optional[ConsolidationVerdict]
    reason: str
    persisted: int = 0
    at: str = ""

    @property
    def wrote_artifacts(self) -> bool:
        """Whether any file changed on disk."""
        return bool(self.files_written)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible receipt."""
        return {
            "ok": self.ok,
            "engine": self.engine,
            "model_calls": self.model_calls,
            "diff_chars": self.diff_chars,
            "diff_truncated": self.diff_truncated,
            "files_written": list(self.files_written),
            "files_unchanged": list(self.files_unchanged),
            "baseline_digest": self.baseline_digest,
            "proposed_digest": self.proposed_digest,
            "verdict": self.verdict.as_dict() if self.verdict is not None else None,
            "reason": self.reason,
            "wrote_artifacts": self.wrote_artifacts,
            "persisted": self.persisted,
            "at": self.at,
            "phase": 2,
            "reviews_a_diff": True,
            "persists_nothing": self.persisted == 0,
        }

    def render(self) -> str:
        """Render the human-facing consolidation receipt."""
        lines = [
            f"consolidation via {self.engine} "
            f"({'model' if self.model_calls else 'no model call'})",
            f"  diff {self.diff_chars} chars"
            + ("  [TRUNCATED -- the reviewer did not see all of it]" if self.diff_truncated else ""),
            f"  baseline {self.baseline_digest[:16]} -> proposed {self.proposed_digest[:16]}",
        ]
        if self.verdict is not None:
            lines.append(
                f"  verdict {self.verdict.verdict.value}: {self.verdict.reason}"
            )
        if self.files_written:
            lines.append(f"  wrote: {', '.join(self.files_written)}")
        else:
            lines.append("  wrote: NOTHING")
        if self.reason:
            lines.append(f"  note: {self.reason}")
        lines.append("  no memory was recorded; that requires a human applying an inbox item")
        return "\n".join(lines)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def unified_diff(before: str, after: str, *, path: str = "memory") -> str:
    """Render a git-style unified diff between two artifact bodies.

    ``difflib.unified_diff`` output is very nearly git's own format, and it is
    DIFFABLE BY A HUMAN WITH ``git apply`` / ``patch`` -- which is the property
    that makes the reviewer input reviewable rather than merely well-formatted.
    """
    before_lines = str(before or "").splitlines(keepends=True)
    after_lines = str(after or "").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


#: The default, deterministic reviewer.
# ---------------------------------------------------------------------------


def rule_reviewer(diff_text: str, context: Mapping[str, Any]) -> ConsolidationVerdict:
    """A deterministic reviewer: additive diffs are accepted, deletions refused.

    The default policy is deliberately CONSERVATIVE and it is stated as a rule,
    not a vibe:

    * an EMPTY diff is ``abstain`` -- there is nothing to consolidate, and
      reporting "accept" for a no-op makes a reviewer's log unreadable;
    * a diff containing a DELETION is ``reject`` -- consolidation must never
      silently drop memory, and the reviewer cannot tell an intentional removal
      from a bug;
    * anything else is ``accept``.

    ``model_calls`` is 0: no model was consulted. A consolidation receipt that
    said a sub-agent reviewed the diff without one having run would be exactly
    the fabrication this repository exists to prevent.
    """
    body = str(diff_text or "")
    if not body.strip():
        return ConsolidationVerdict(
            verdict=ReviewVerdict.ABSTAIN,
            reason="the diff is empty, so there is nothing to consolidate",
            engine="rules",
            model_calls=0,
            reviewer="rule_reviewer",
        )
    deletions = [
        line
        for line in body.splitlines()
        if line.startswith(("-",)) and not line.startswith("---")
    ]
    if deletions:
        return ConsolidationVerdict(
            verdict=ReviewVerdict.REJECT,
            reason=(
                f"the diff deletes {len(deletions)} line(s); the default policy "
                "refuses to remove memory during consolidation because a review "
                "cannot distinguish an intended removal from a bug"
            ),
            engine="rules",
            model_calls=0,
            reviewer="rule_reviewer",
        )
    additions = [
        line for line in body.splitlines() if line.startswith("+") and not line.startswith("+++")
    ]
    return ConsolidationVerdict(
        verdict=ReviewVerdict.ACCEPT,
        reason=(
            f"the diff is purely additive ({len(additions)} added line(s)) and "
            "contains no deletion"
        ),
        engine="rules",
        model_calls=0,
        reviewer="rule_reviewer",
    )


class MemoryConsolidator:
    """Phase 2: propose artifact files, have a sub-agent review the diff, apply.

    The memory root is a plain directory of Markdown artifacts plus a
    ``baseline.json`` recording the content digest of each file at the last
    accepted consolidation. That baseline is the "git-baselined" part: it is what
    makes the diff a diff rather than a fresh render.

    NOTHING here writes the decision store. ``consolidate`` writes files under
    ``root`` only when the reviewer accepts, and it reports ``persisted: 0``
    unconditionally -- a memory reaches the store only via
    :class:`memory.inbox.MemoryInbox.apply`.
    """

    def __init__(self, root: Any, *, reviewer: Optional[Callable[..., Any]] = None) -> None:
        """Point at a memory root and choose the reviewer.

        ``reviewer`` returns a :class:`ConsolidationVerdict` (or a mapping with
        the same keys). A reviewer that raises is treated as ``abstain``, never
        as accept: a crashed reviewer must not be read as approval.
        """
        self.root = Path(str(root)).expanduser()
        self.reviewer = reviewer or rule_reviewer

    # -- artifacts ------------------------------------------------------

    def _baseline_path(self) -> Path:
        return self.root / "baseline.json"

    def _read(self, name: str) -> str:
        path = self.root / name
        try:
            if path.is_symlink() or not path.is_file():
                return ""
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""

    def _read_baseline(self) -> Dict[str, str]:
        path = self._baseline_path()
        try:
            if path.is_symlink() or not path.is_file():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            return {}
        digests = data.get("digests") if isinstance(data, MappingABC) else None
        return {
            str(key): str(value)
            for key, value in (digests.items() if isinstance(digests, MappingABC) else ())
        }

    @staticmethod
    def _render_artifact(
        title: str,
        rows: Sequence[Any],
        *,
        max_chars: int,
    ) -> Tuple[str, bool]:
        """Render memories into a Markdown artifact, reporting truncation.

        Returns ``(text, truncated)``. Rows are the store's own projections;
        each carries its id so a reader of the FILE can trace a line back to a
        row, which is what makes the artifact auditable rather than decorative.
        """
        lines = [f"# {title}", ""]
        truncated = False
        for row in rows or ():
            if isinstance(row, MappingABC):
                text = str(row.get("text") or "").strip()
                category = str(row.get("category") or "general")
                ident = row.get("id")
            else:
                text = str(getattr(row, "text", "") or "").strip()
                category = str(getattr(row, "category", "general") or "general")
                ident = getattr(row, "id", None)
            if not text:
                continue
            entry = f"- #{ident} ({category}) {text}"
            if len("\n".join(lines)) + len(entry) + 1 > max_chars:
                truncated = True
                lines.append(f"- …[{len(rows)} memory row(s); this artifact is capped at {max_chars} chars]")
                break
            lines.append(entry)
        return "\n".join(lines) + "\n", truncated

    # -- the pass -------------------------------------------------------

    def consolidate(
        self,
        store: Any,
        *,
        repo_path: Any = None,
        limit: int = 200,
        user_limit: int = 40,
        max_chars: int = MAX_ARTIFACT_CHARS,
        at: Optional[float] = None,
    ) -> ConsolidationReceipt:
        """Propose artifacts, review the diff, and write only on acceptance.

        The sequence is fixed and each step is reported:

        1. read the current artifacts (the ``a/`` side of the diff);
        2. render proposed artifacts from the store (the ``b/`` side);
        3. render the unified diff of the two;
        4. hand the diff to the reviewer;
        5. on ``accept``, write the artifacts and update the baseline;
           otherwise write nothing.

        A diff that exceeds :data:`MAX_DIFF_CHARS` is truncated before the
        reviewer sees it and ``diff_truncated`` is set -- so a reviewer that
        accepts a truncated diff is visibly accepting a truncated diff.
        """
        stamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(at if at is not None else time.time())
        )
        baseline = self._read_baseline()

        def refuse(reason: str) -> ConsolidationReceipt:
            return ConsolidationReceipt(
                ok=False,
                engine="",
                model_calls=0,
                diff_chars=0,
                diff_truncated=False,
                files_written=(),
                files_unchanged=(),
                baseline_digest=_digest(json.dumps(baseline, sort_keys=True)),
                proposed_digest="",
                verdict=None,
                reason=reason,
                persisted=0,
                at=stamp,
            )

        if store is None:
            return refuse("no store was supplied, so there is nothing to consolidate")

        try:
            project_rows = store.search("", limit=int(limit), repo_path=repo_path)
        except TypeError:
            project_rows = store.search("", limit=int(limit))
        except Exception:
            project_rows = []

        from memory.snapshot import _is_user_stated

        user_rows = [row for row in project_rows if _is_user_stated(row)][
            : max(0, int(user_limit))
        ]
        project_only = [row for row in project_rows if row not in user_rows]

        proposed: Dict[str, str] = {}
        artifact_truncated: Dict[str, bool] = {}
        proposed[MEMORY_FILE], artifact_truncated[MEMORY_FILE] = (
            self._render_artifact("Project memory", project_only, max_chars=max_chars)
        )
        proposed[USER_FILE], artifact_truncated[USER_FILE] = self._render_artifact(
            "User memory", user_rows, max_chars=max_chars
        )

        diffs: List[str] = []
        unchanged: List[str] = []
        diff_chars = 0
        truncated_diff = False
        for name in (MEMORY_FILE, USER_FILE):
            before = self._read(name)
            after = proposed[name]
            if _digest(before) == _digest(after):
                unchanged.append(name)
                continue
            piece = unified_diff(before, after, path=name)
            if diff_chars + len(piece) > MAX_DIFF_CHARS:
                truncated_diff = True
                remaining = max(0, MAX_DIFF_CHARS - diff_chars)
                diffs.append(piece[:remaining])
                diff_chars += remaining
                break
            diffs.append(piece)
            diff_chars += len(piece)

        diff_text = "".join(diffs)
        proposed_digest = _digest(
            "\x1f".join(f"{name}:{_digest(body)}" for name, body in sorted(proposed.items()))
        )

        verdict: Optional[ConsolidationVerdict] = None
        if not diff_text.strip():
            return ConsolidationReceipt(
                ok=True,
                engine="rules",
                model_calls=0,
                diff_chars=0,
                diff_truncated=False,
                files_written=(),
                files_unchanged=tuple(unchanged),
                baseline_digest=_digest(json.dumps(baseline, sort_keys=True)),
                proposed_digest=proposed_digest,
                verdict=ConsolidationVerdict(
                    verdict=ReviewVerdict.ABSTAIN,
                    reason=(
                        "the artifacts already match what the store would "
                        "produce; there is nothing to consolidate"
                    ),
                    engine="rules",
                    model_calls=0,
                    reviewer="rule_reviewer",
                ),
                reason=(
                    "artifact truncation: "
                    + ", ".join(
                        f"{name}={value}" for name, value in artifact_truncated.items() if value
                    )
                    or "none"
                ),
                persisted=0,
                at=stamp,
            )

        context = {
            "repo_path": str(repo_path or ""),
            "diff_truncated": truncated_diff,
            "files": sorted(changed for changed in proposed if changed not in unchanged),
            "artifact_truncated": artifact_truncated,
            "rows": len(project_rows),
        }
        try:
            raw = self.reviewer(diff_text, context)
        except Exception as exc:
            # A crashed reviewer is ABSTAIN, not accept. This is the single most
            # dangerous default in the module and it is deliberate.
            verdict = ConsolidationVerdict(
                verdict=ReviewVerdict.ABSTAIN,
                reason=(
                    f"the reviewer raised {type(exc).__name__}: {exc}"[:200]
                    + "; a reviewer that cannot answer is never treated as "
                    "approving"
                ),
                engine="rules",
                model_calls=0,
                reviewer=getattr(self.reviewer, "__name__", "reviewer"),
            )
        else:
            verdict = self._coerce_verdict(raw)

        written: Tuple[str, ...] = ()
        write_reason = ""
        if verdict is not None and verdict.accepted:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                for name, body in proposed.items():
                    target = self.root / name
                    if _digest(self._read(name)) == _digest(body):
                        continue
                    if len(body) > max_chars:
                        write_reason = (
                            f"{name} exceeded the {max_chars}-char artifact cap "
                            "and was NOT written; a truncated artifact that reads "
                            "as complete is worse than none"
                        )
                        continue
                    target.write_text(body, encoding="utf-8")
                    written = (*written, name)
                self._write_baseline(proposed)
            except OSError as exc:
                write_reason = f"writing the artifacts failed: {type(exc).__name__}"

        return ConsolidationReceipt(
            ok=verdict is not None and verdict.accepted and not write_reason,
            engine=(verdict.engine if verdict is not None else "rules"),
            model_calls=(verdict.model_calls if verdict is not None else 0),
            diff_chars=diff_chars,
            diff_truncated=truncated_diff,
            files_written=written,
            files_unchanged=tuple(unchanged),
            baseline_digest=_digest(json.dumps(baseline, sort_keys=True)),
            proposed_digest=proposed_digest,
            verdict=verdict,
            reason=write_reason
            or (
                ", ".join(
                    f"{name} truncated at {max_chars} chars"
                    for name, value in artifact_truncated.items()
                    if value
                )
                or ""
            ),
            persisted=0,
            at=stamp,
        )

    def _coerce_verdict(self, raw: Any) -> ConsolidationVerdict:
        """Normalise whatever the reviewer returned into a verdict.

        A reviewer that returns an unrecognised value ABSTAINS. It does not
        accept: "the reviewer said something I do not understand" is not
        approval, and defaulting it to approval would make the reviewer's
        contract decorative.
        """
        if isinstance(raw, ConsolidationVerdict):
            return raw
        if isinstance(raw, MappingABC):
            value = str(raw.get("verdict") or "").strip().casefold()
            try:
                verdict = ReviewVerdict(value)
            except ValueError:
                return ConsolidationVerdict(
                    verdict=ReviewVerdict.ABSTAIN,
                    reason=(
                        f"the reviewer returned an unrecognised verdict {value!r}; "
                        "treating it as abstain rather than as approval"
                    ),
                    engine=str(raw.get("engine") or "unknown"),
                    model_calls=int(raw.get("model_calls") or 0),
                    reviewer=str(raw.get("reviewer") or ""),
                )
            return ConsolidationVerdict(
                verdict=verdict,
                reason=str(raw.get("reason") or ""),
                engine=str(raw.get("engine") or "unknown"),
                model_calls=int(raw.get("model_calls") or 0),
                reviewer=str(raw.get("reviewer") or ""),
            )
        return ConsolidationVerdict(
            verdict=ReviewVerdict.ABSTAIN,
            reason=(
                f"the reviewer returned {type(raw).__name__}, which is not a "
                "verdict; treating it as abstain rather than as approval"
            ),
            engine="unknown",
            model_calls=0,
        )

    def _write_baseline(self, proposed: Mapping[str, str]) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "digests": {name: _digest(body) for name, body in proposed.items()},
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._baseline_path().write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def artifacts(self) -> Dict[str, str]:
        """Read the current artifact bodies, for a status surface."""
        return {name: self._read(name) for name in (MEMORY_FILE, USER_FILE)}
