"""THE context-file authority: one filename set, one precedence, one walker.

**The defect this module closes is a divergence bug, not a missing feature.**
Phase 0's audit found three separate implementations of the same walk, each with
its own filename list:

* ``harness/context_compiler.py`` — ``("AGENTS.md", "CLAUDE.md", "GEMINI.md",
  "QWEN.md")`` plus a ``.neo/`` list;
* ``memory/project_context.py`` — ``("AGENTS.md", "CLAUDE.md")`` plus
  ``("AGENTS.md", "INSTRUCTIONS.md")``;
* ``harness/agent_kernel/context.py`` — a hard-coded pair of ``root / "AGENTS.md"``
  and ``root / ".neo" / "AGENTS.md"``.

Three lists that agree today diverge the first time one of them gains an entry,
and ``phases/DOCTRINE.md`` §8 already records this exact class of failure costing
345x on a retrieval walk. One list with a documented precedence is a feature;
three lists is a bug waiting to happen. This module is the list.

**The filename set is the corpus's, unified.** Hermes walks its own file ->
``AGENTS.md`` -> ``CLAUDE.md`` -> ``.cursorrules``, first-found-wins. OpenCode
reads ``AGENTS.md`` / ``CLAUDE.md`` / ``CONTEXT.md``, first filename wins. We
recognise the union, in one documented precedence, so a repository that ships
any of them is obeyed rather than silently ignored.

**Precedence is documented, not incidental.** Within ONE directory, the first
name in :data:`CONTEXT_FILE_NAMES` that exists WINS — so a repository carrying
both ``AGENTS.md`` and ``.cursorrules`` gets ``AGENTS.md``. Across directories,
Codex's rule holds and is the one that matters: **root-to-cwd, nested content
last.** A deeper ``AGENTS.md`` is a more specific instruction and must be read
after the general one; reversing that is how a directory's own rules get
overridden by the repository root's.

**Trust gating is T5's authority, not a second scanner.** Gemini gates context
discovery by folder trust; Hermes blocks wholesale on an injection match. Both
of those were built in P2 into ``shared.security``, and this module CALLS them
(:func:`shared.security.review_repository_instructions`) instead of writing a
fourth opinion about whether a repository's own markdown may be read. This
module's job is *discovery and ordering*; *trust* is ``shared/security.py``'s,
and the two are kept apart on purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The ONE filename set, in PRECEDENCE order. First match within a directory
#: wins; see the module docstring for why that is stated rather than left to a
#: reader to infer from the tuple's order.
#:
#: The set is the union of what the corpus systems read, plus the two this
#: repository already honoured and one T5's boundary already names:
#:
#: * ``AGENTS.md``    - Codex / OpenCode / Hermes / Gemini, and this repo's own
#: * ``CLAUDE.md``    - Claude Code / Hermes / OpenCode
#: * ``GEMINI.md``    - Gemini CLI
#: * ``QWEN.md``      - Qwen Code
#: * ``NEO.md``       - this product's own, and already in
#:                     ``shared.security.REPOSITORY_INSTRUCTION_FILES``
#: * ``INSTRUCTIONS.md`` - generic, and already in both of the above
#: * ``CONTEXT.md``   - OpenCode's third name
#: * ``.cursorrules`` - Cursor, and Hermes's last name
#: * ``.windsurfrules`` - Windsurf; the same shape as ``.cursorrules`` and
#:                     cheap to support now because the set is DATA
CONTEXT_FILE_NAMES: Tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "QWEN.md",
    "NEO.md",
    "INSTRUCTIONS.md",
    "CONTEXT.md",
    ".cursorrules",
    ".windsurfrules",
)

#: Filenames that live in a ``.neo/`` subdirectory rather than at the directory
#: root. Kept separate because they are a different LOOKUP (one directory deeper),
#: not a different precedence.
NEO_SUBDIRECTORY = ".neo"

#: The bound on how deep a root-to-cwd walk goes. A repository with an
#: instruction file 40 directories down is not describing a subtree an agent is
#: working in; past this depth the walk is reported as truncated rather than
#: silently shortened.
MAX_CONTEXT_DEPTH = 24

#: The byte ceiling for one instruction file, matching what the context compiler
#: already applied. Reused as a constant rather than re-derived so the two
#: cannot drift.
MAX_INSTRUCTION_BYTES = 64_000


def is_context_filename(name: str) -> bool:
    """Whether ``name`` is one of the recognised instruction filenames."""
    return str(name or "") in CONTEXT_FILE_NAMES


def first_context_file(directory: Path) -> Optional[Path]:
    """Return the winning instruction file in ``directory``, or ``None``.

    **First-found-wins across :data:`CONTEXT_FILE_NAMES`**, which is Hermes's
    documented order and OpenCode's "first filename wins". It is a per-directory
    question and the answer is deliberately NOT "every file that exists": two
    instruction files in one directory are two competing authorities, and
    concatenating them is how a repository ends up contradicting itself with no
    way to say which half wins.

    The ``.neo/`` subdirectory is NOT consulted here. Callers that want it ask
    :func:`context_files_in` with ``include_neo=True``; a JIT trigger looking at
    one directory should not silently pick up a second, deeper authority.
    """
    try:
        base = Path(str(directory))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    for name in CONTEXT_FILE_NAMES:
        candidate = base / name
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    return None


def context_files_in(
    directory: Path, *, include_neo: bool = True
) -> Tuple[Path, ...]:
    """Return every instruction file that applies IN ``directory``, in order.

    The order is :data:`CONTEXT_FILE_NAMES` first (root-level names) then the
    ``.neo/`` names. A caller that concatenates all of them is building the
    root-to-cwd concatenation Codex does; a caller that wants only the winner
    uses :func:`first_context_file`.
    """
    found: List[Path] = []
    try:
        base = Path(str(directory))
    except (OSError, RuntimeError, TypeError, ValueError):
        return ()
    winner = first_context_file(base)
    if winner is not None:
        found.append(winner)
    if include_neo:
        neo = base / NEO_SUBDIRECTORY
        try:
            if neo.is_dir() and not neo.is_symlink():
                for name in CONTEXT_FILE_NAMES:
                    candidate = neo / name
                    if candidate.is_file() and not candidate.is_symlink():
                        found.append(candidate)
        except OSError:
            return tuple(found)
    return tuple(found)


def _normalize(path: Any) -> Optional[Path]:
    """Return an absolute, resolved path, or ``None`` when it cannot be one."""
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _is_absolute(raw: str) -> bool:
    """Whether ``raw`` is absolute under EITHER path grammar.

    ``PurePosixPath("/etc/hosts").is_absolute()`` is ``False`` on Windows, and a
    bare ``Path(raw).is_absolute()`` therefore answers the POSIX question while
    the caller is holding a Windows path. Both grammars are asked, which is the
    same rule ``shared.platform.is_absolute_anywhere`` applies and the reason a
    repository-relative check does not mis-classify ``/etc/hosts`` as relative on
    the wrong platform.
    """
    try:
        text = str(raw)
    except (TypeError, ValueError):
        return False
    if not text:
        return False
    if text.startswith(("/", "\\")):
        return True
    try:
        return Path(text).is_absolute()
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class ContextFile:
    """One discovered instruction file and its position in the ordering."""

    path: Path
    relative_path: str
    scope: str
    depth: int

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection. Carries paths, never content."""
        return {
            "path": str(self.path),
            "relative_path": str(self.relative_path),
            "scope": str(self.scope),
            "depth": int(self.depth),
            "name": self.path.name,
        }


@dataclass(frozen=True)
class ContextDiscovery:
    """The root-to-cwd instruction set for one repository and target.

    ``files`` is ordered ROOT FIRST, DEEPEST LAST. That order is Codex's
    ``AGENTS.md`` rule and it is the precedence rule: a directory's own
    instructions are more specific than the repository root's, so they are read
    after. ``truncated`` is reported rather than inferred — a walk that stopped
    at :data:`MAX_CONTEXT_DEPTH` says so, because a shortened list that reads as
    a complete one is exactly the kind of silent gap this module exists to
    remove.
    """

    repo: str = ""
    target: str = ""
    files: Tuple[ContextFile, ...] = ()
    truncated: bool = False
    warnings: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()

    @property
    def scopes(self) -> Tuple[str, ...]:
        """The distinct scopes, in root-to-cwd order."""
        seen: List[str] = []
        for item in self.files:
            if item.scope not in seen:
                seen.append(item.scope)
        return tuple(seen)

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection."""
        return {
            "repo": str(self.repo),
            "target": str(self.target),
            "files": [item.as_dict() for item in self.files],
            "scopes": list(self.scopes),
            "truncated": bool(self.truncated),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "filenames": list(CONTEXT_FILE_NAMES),
        }


def discover_context_files(
    repo_path: Any, target_path: Any = None, *, include_neo: bool = True
) -> ContextDiscovery:
    """Discover the instruction set for ``target_path``, ROOT-TO-CWD ORDERED.

    Total and never raising. An unreadable root yields an empty result with the
    reason in ``warnings`` rather than an exception, so a caller building a
    prompt cannot die on a repository it merely cannot read.

    ``target_path`` is a FILE or a DIRECTORY, absolute or repository-relative.
    A relative target that escapes the repository (``../elsewhere``) resolves to
    the root only, with a warning — widening the walk to a parent because a
    caller passed a traversable path would read instruction files the repository
    does not control.
    """
    root = _normalize(repo_path)
    if root is None or not root.is_dir():
        return ContextDiscovery(
            repo=str(repo_path or ""),
            target=str(target_path or ""),
            warnings=("repository is not a readable directory",),
        )
    warnings: List[str] = []
    target = root
    if target_path:
        raw = str(target_path)
        # Relativeness MUST be decided from the RAW string, before any
        # ``resolve()``. A relative path resolved first lands against the
        # PROCESS's working directory, which is not the repository - so
        # ``discover_context_files(repo, "pkg/sub/mod.py")`` came back as
        # "target is outside the repository" and the walk silently collapsed to
        # the root. That is the worst possible shape for this bug: the root
        # instructions still arrived, so the run looked healthy while the NESTED
        # file the JIT trigger exists to fetch never did. Pinned by
        # ``test_a_relative_target_resolves_against_the_repository_not_the_cwd``.
        joined = raw if _is_absolute(raw) else str(root / raw)
        candidate = _normalize(joined)
        if candidate is None:
            warnings.append("target path could not be interpreted")
        resolved = candidate or root
        try:
            resolved.relative_to(root)
            target = resolved
        except ValueError:
            warnings.append(
                "target is outside the repository; the walk was not widened"
            )

    directory = target if target.is_dir() else target.parent
    try:
        relative = directory.relative_to(root)
        parts = relative.parts
    except (OSError, ValueError):
        parts = ()

    truncated = len(parts) > MAX_CONTEXT_DEPTH
    if truncated:
        warnings.append(
            f"target is {len(parts)} directories deep; the instruction walk "
            f"stopped at MAX_CONTEXT_DEPTH={MAX_CONTEXT_DEPTH}"
        )
    walked = parts[:MAX_CONTEXT_DEPTH]

    files: List[ContextFile] = []
    errors: List[str] = []
    seen: set = set()
    for depth in range(len(walked) + 1):
        directory_path = root.joinpath(*walked[:depth])
        scope = "." if depth == 0 else Path(*walked[:depth]).as_posix()
        for candidate in context_files_in(directory_path, include_neo=include_neo):
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            try:
                rel = candidate.relative_to(root).as_posix()
            except (OSError, ValueError):
                rel = candidate.name
            files.append(
                ContextFile(
                    path=candidate,
                    relative_path=rel,
                    scope=scope,
                    depth=depth,
                )
            )
    # Explicit sort rather than relying on the loop order: the ordering IS the
    # contract, so it is stated once, in one place, where a reader can check it.
    # ROOT FIRST, then depth, then :data:`CONTEXT_FILE_NAMES` precedence — which
    # is why a naive alphabetical sort is wrong here: it puts `.neo/INSTRUCTIONS.md`
    # ahead of `AGENTS.md` at the same depth purely because a dot sorts first,
    # inverting the precedence the whole module documents.
    files.sort(key=_precedence_key)
    return ContextDiscovery(
        repo=str(root),
        target=str(target),
        files=tuple(files),
        truncated=truncated,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def review_context_text(
    text: Any,
    *,
    relative_path: str = "",
    config: Optional[Mapping[str, Any]] = None,
) -> Any:
    """Review one instruction file's text through T5's authority.

    A THIN DELEGATE to ``shared.security.review_repository_instruction_text``.
    It exists so every caller in the harness reaches the trust boundary through
    ONE harness name — a second opinion about whether a repository's own markdown
    may be read would be a fourth policy, and this repository has already paid
    for that class of bug in its redactor, its skip-set, and its tool catalog.

    Returns ``None`` when the boundary is unreachable rather than passing the
    text through unreviewed. An unreviewed instruction file reaching a prompt is
    the failure this round's security half exists to prevent, so "could not
    check" must not read as "fine".
    """
    try:
        from shared.security import review_repository_instruction_text
    except Exception:
        return None
    try:
        return review_repository_instruction_text(
            text, path=str(relative_path or ""), config=config
        )
    except Exception:
        return None


def review_context_set(
    repo_root: Any, *, config: Optional[Mapping[str, Any]] = None, home: Any = None
) -> Any:
    """Review a repository's whole instruction set through T5's authority.

    Delegates to ``shared.security.review_repository_instructions``, which reads
    the files, applies the operator trust list, defangs forged boundaries and
    quarantines on a high-severity finding. Returns ``None`` when the boundary is
    unreachable — never an unreviewed result dressed as a reviewed one.
    """
    try:
        from shared.security import review_repository_instructions
    except Exception:
        return None
    try:
        return review_repository_instructions(repo_root, config=config, home=home)
    except Exception:
        return None


def _precedence_key(item: ContextFile) -> Tuple[int, int, str]:
    """Return the sort key that IS the documented ordering.

    Root first (``depth`` ascending — Codex's rule, nested content last), then
    :data:`CONTEXT_FILE_NAMES` precedence within a depth, then the path so the
    order is total and deterministic for two files of equal precedence.
    """
    try:
        rank = CONTEXT_FILE_NAMES.index(Path(str(item.relative_path)).name)
    except ValueError:
        rank = len(CONTEXT_FILE_NAMES)
    return (int(item.depth), rank, str(item.relative_path))


@dataclass
class PathTrigger:
    """OpenHands' ``PathTrigger``: inject a directory's instructions JIT.

    The corpus names the gap precisely: *"Inject top-level content near the top
    of the system prompt; surface **nested** files just-in-time when tools touch
    their subtree."* Eight of eleven systems do the first half; almost none do
    the second, and that second half is what keeps the main window clean while
    the agent still obeys the directory it is working in.

    **What a trigger does and does not do.** When a tool touches
    ``pkg/sub/mod.py``, the nearest instruction file in scope for that path is
    injected — ONCE per file, because the point is to keep the main window clean
    and re-injecting on every touch is the opposite of that. It is a *view*
    addition to the conversation, journaled like any other turn, so the injected
    content is inspectable after the fact rather than having appeared from
    nowhere.

    **Trust is not this class's job.** ``injected`` carries the file it came
    from; whether that file's TEXT may be interpolated is
    :func:`review_context_text`'s question and is answered by ``shared.security``.
    Keeping the two apart is why a discovery change can never quietly become a
    trust-policy change.
    """

    repo_root: Path
    #: Relative paths whose instruction file has already been injected.
    injected: Dict[str, str] = field(default_factory=dict)
    #: Every trigger firing this run, in order, for the receipt.
    fired: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.repo_root = _normalize(self.repo_root) or Path(".")

    # -- discovery ------------------------------------------------------

    def _resolve_against_root(self, path: Any) -> Optional[Path]:
        """Resolve ``path`` against the REPOSITORY ROOT, not the process cwd.

        A ``PathTrigger`` is always asked about a path RELATIVE to the
        repository — that is how a tool call reports it — so resolving first
        would land against whatever directory the process happens to be in. The
        containment check afterwards then reports "outside the repository" and
        the trigger silently returns nothing, which is the failure this helper
        exists to remove.
        """
        raw = str(path or "")
        if not raw:
            return None
        joined = raw if _is_absolute(raw) else str(self.repo_root / raw)
        return _normalize(joined)

    def in_scope_for(self, path: Any) -> ContextDiscovery:
        """Return the instruction set in scope for ``path``.

        This is the JIT half: the walk is from the REPOSITORY ROOT down to the
        file's own directory, so the returned list is the same root-to-cwd
        concatenation the compile-time discovery produces, narrowed to the file
        that was touched.
        """
        return discover_context_files(self.repo_root, self._resolve_against_root(path))

    def pending_for(self, path: Any) -> Tuple[ContextFile, ...]:
        """Return the instruction files ``path`` brings into scope that are NEW.

        "New" means not yet injected for that scope. A second read of the same
        directory re-injects nothing, which is what keeps a repeated ``read`` of
        the same path from growing the context by a whole instruction file per
        call.
        """
        found: List[ContextFile] = []
        for item in self.in_scope_for(path).files:
            key = os.path.normcase(item.relative_path)
            if key in self.injected:
                continue
            found.append(item)
        return tuple(found)

    # -- firing ---------------------------------------------------------

    def note_touched(self, path: Any, *, tool: str = "") -> "PathTriggerResult":
        """Record a tool touching ``path`` and return what to inject.

        Idempotent per scope, so calling it on every touch of the same file is
        free after the first. A path outside the repository returns an empty
        result with the reason rather than reading a parent directory's
        instructions.
        """
        normalized = self._resolve_against_root(path)
        if normalized is None:
            return PathTriggerResult(pending=(), reason="path could not be interpreted")
        try:
            normalized.relative_to(self.repo_root)
        except ValueError:
            return PathTriggerResult(
                pending=(), reason="path is outside the repository"
            )
        pending = self.pending_for(normalized)
        for item in pending:
            self.injected[os.path.normcase(item.relative_path)] = item.scope
        record = {
            "tool": str(tool or ""),
            "path": str(normalized),
            "pending": [item.relative_path for item in pending],
        }
        self.fired.append(record)
        return PathTriggerResult(pending=pending, reason="")

    # -- reporting ------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """Return what this trigger has injected so far.

        Published so a receipt can say which directories' instructions entered
        the conversation without re-deriving it from the transcript.
        """
        return {
            "repo": str(self.repo_root),
            "injected": dict(self.injected),
            "injected_count": len(self.injected),
            "fired_count": len(self.fired),
            "fired": list(self.fired),
            "filenames": list(CONTEXT_FILE_NAMES),
        }

    def reset(self) -> None:
        """Forget every injection, so a rewind can re-trigger from scratch."""
        self.injected.clear()
        self.fired.clear()


@dataclass(frozen=True)
class PathTriggerResult:
    """What one trigger firing found."""

    pending: Tuple[ContextFile, ...] = ()
    reason: str = ""

    @property
    def empty(self) -> bool:
        """Whether there is nothing to inject."""
        return not self.pending

    def as_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe projection."""
        return {
            "pending": [item.as_dict() for item in self.pending],
            "count": len(self.pending),
            "reason": str(self.reason),
        }


def render_injected_context(
    files: Sequence[Any], *, max_chars: int = 8000, config: Optional[Mapping[str, Any]] = None
) -> str:
    """Render instruction files for JIT injection, through the trust boundary.

    Each block is labelled with the directory it came from, because a directory
    whose instructions arrive twenty turns into a run are indistinguishable from
    the agent's own findings without it. A file whose review BLOCKS is rendered
    as an explicit refusal line rather than omitted: silence would let a reader
    conclude the directory had no instructions.

    Returns ``""`` when nothing survived, which is the honest "no context file is
    in scope" answer rather than an empty shell of a prompt.
    """
    blocks: List[str] = []
    used = 0
    for item in files or ():
        relative = getattr(item, "relative_path", "") or ""
        path = getattr(item, "path", "")
        try:
            raw = Path(str(path)).read_text(encoding="utf-8", errors="replace")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            blocks.append(
                f"[nested context: {relative}] NOT READ ({type(exc).__name__})"
            )
            continue
        if len(raw) > MAX_INSTRUCTION_BYTES:
            raw = raw[:MAX_INSTRUCTION_BYTES]
            note = "[nested context: truncated at MAX_INSTRUCTION_BYTES]"
        else:
            note = ""
        review = review_context_text(raw, relative_path=relative, config=config)
        if review is None:
            blocks.append(
                f"[nested context: {relative}] NOT REVIEWED - the trust boundary "
                "was unreachable, so its text is withheld"
            )
            continue
        if getattr(review, "blocked", False):
            blocks.append(
                f"[nested context: {relative}] BLOCKED by the untrusted-source "
                "boundary; its text is withheld"
            )
            continue
        banner = str(getattr(review, "text", "") or "")
        finding = getattr(review, "severity", "") or ""
        header = f"[nested context: {relative}]"
        if finding:
            header = f"{header} (reviewed: {finding})"
        piece = f"{header}\n{banner}"
        if note:
            piece = f"{piece}\n{note}"
        if used + len(piece) > max_chars:
            blocks.append(
                f"[nested context: {len(blocks)} more file(s) omitted to fit a "
                f"{max_chars}-char budget]"
            )
            break
        blocks.append(piece)
        used += len(piece)
    return "\n\n".join(blocks).strip()


def context_file_report() -> Dict[str, Any]:
    """Return the authority's own shape, for a receipt and for the pins.

    Includes the relationship to ``shared.security.REPOSITORY_INSTRUCTION_FILES``
    as DATA rather than as a test-only assertion, so a caller asking "what can a
    repository put in front of my agent" reads one place and gets both the
    discovery names and whether the trust boundary covers all of them.
    """
    reviewed: Tuple[str, ...] = ()
    covered: Tuple[str, ...] = ()
    try:
        from shared.security import REPOSITORY_INSTRUCTION_FILES as reviewed
    except Exception:
        reviewed = ()
    covered = tuple(name for name in reviewed if name in CONTEXT_FILE_NAMES)
    uncovered = tuple(name for name in reviewed if name not in CONTEXT_FILE_NAMES)
    return {
        "filenames": list(CONTEXT_FILE_NAMES),
        "count": len(CONTEXT_FILE_NAMES),
        "neo_subdirectory": NEO_SUBDIRECTORY,
        "max_depth": MAX_CONTEXT_DEPTH,
        "max_instruction_bytes": MAX_INSTRUCTION_BYTES,
        "boundary_reviewed_names": list(reviewed),
        "boundary_covers": list(covered),
        "boundary_does_not_cover": list(uncovered),
        "boundary_available": bool(reviewed),
    }
